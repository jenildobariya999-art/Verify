#!/usr/bin/env python3
"""BotPanel - multi-bot Telegram manager with a TBC (TPY) compatible runtime.
Run:  pip install flask requests gunicorn  &&  ADMIN_PASSWORD='strong-pass' python botpanel.py
Prod: gunicorn -w 1 --threads 16 -b 127.0.0.1:8000 botpanel:app   (keep 1 worker: bots poll in-process)
Put nginx + HTTPS in front and set COOKIE_SECURE=1."""
import os, io, re, csv, json, time, hmac, secrets, sqlite3, threading, traceback, hashlib
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import requests
from flask import (Flask, request, session, redirect, url_for, render_template_string,
                   abort, Response, flash, get_flashed_messages)
from markupsafe import Markup
from werkzeug.security import generate_password_hash, check_password_hash

BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("DB_PATH", os.path.join(BASE, "botpanel.db"))
KEY_PATH = os.path.join(BASE, ".secret_key")
if not os.path.exists(KEY_PATH):
    with open(KEY_PATH, "w") as f: f.write(secrets.token_hex(32))
    os.chmod(KEY_PATH, 0o600)

app = Flask(__name__)
app.secret_key = open(KEY_PATH).read().strip()
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                  SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE") == "1",
                  MAX_CONTENT_LENGTH=20 * 1024 * 1024, PERMANENT_SESSION_LIFETIME=8 * 3600)
LOCK = threading.RLock()
POOL = ThreadPoolExecutor(max_workers=16)

# ---------------------------------------------------------------- database
def db():
    c = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA busy_timeout=30000"); c.execute("PRAGMA synchronous=NORMAL")
    return c

def q(sql, args=(), one=False, commit=False):
    with LOCK:
        c = db()
        try:
            cur = c.execute(sql, args)
            if commit: c.commit()
            rows = cur.fetchall()
            return (rows[0] if rows else None) if one else rows
        finally:
            c.close()

def init_db():
    with LOCK:
        c = db()
        try: c.execute("PRAGMA journal_mode=WAL")
        except Exception: pass
        c.executescript("""
        CREATE TABLE IF NOT EXISTS settings(k TEXT PRIMARY KEY, v TEXT);
        CREATE TABLE IF NOT EXISTS bots(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, username TEXT,
            token TEXT UNIQUE, running INTEGER DEFAULT 0, created TEXT);
        CREATE TABLE IF NOT EXISTS commands(bot_id INTEGER, name TEXT, code TEXT, updated TEXT,
            PRIMARY KEY(bot_id,name));
        CREATE TABLE IF NOT EXISTS users(bot_id INTEGER, uid INTEGER, first_name TEXT, username TEXT,
            banned INTEGER DEFAULT 0, joined TEXT, last_seen TEXT, PRIMARY KEY(bot_id,uid));
        CREATE TABLE IF NOT EXISTS kv(bot_id INTEGER, uid INTEGER, k TEXT, v TEXT, PRIMARY KEY(bot_id,uid,k));
        CREATE TABLE IF NOT EXISTS waits(bot_id INTEGER, uid INTEGER, cmd TEXT, PRIMARY KEY(bot_id,uid));
        CREATE TABLE IF NOT EXISTS res(bot_id INTEGER, uid INTEGER, name TEXT, val REAL DEFAULT 0,
            PRIMARY KEY(bot_id,uid,name));
        CREATE TABLE IF NOT EXISTS errors(id INTEGER PRIMARY KEY AUTOINCREMENT, bot_id INTEGER, cmd TEXT,
            msg TEXT, ts TEXT);""")
        c.commit(); c.close()

def now(): return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
def log_err(bot_id, cmd, msg):
    q("INSERT INTO errors(bot_id,cmd,msg,ts) VALUES(?,?,?,?)", (bot_id, cmd, str(msg)[-2000:], now()), commit=True)
    q("DELETE FROM errors WHERE id NOT IN (SELECT id FROM errors ORDER BY id DESC LIMIT 500)", commit=True)

# ---------------------------------------------------------------- TPY runtime
class D(dict):
    """dict with attribute access; missing keys -> None (like Telegram objects in TBC)."""
    def __getattr__(self, k):
        if k == "from_user": k = "from"
        v = self.get(k)
        return D(v) if isinstance(v, dict) else ([D(x) if isinstance(x, dict) else x for x in v] if isinstance(v, list) else v)
    def __setattr__(self, k, v): self[k] = v

class ReturnCommand(Exception): pass

TG_ARGS = {  # positional argument names for bot.<method>(...)
    "sendMessage": ("chat_id", "text"), "replyText": ("chat_id", "text"),
    "editMessageText": ("chat_id", "message_id", "text"), "deleteMessage": ("chat_id", "message_id"),
    "copyMessage": ("chat_id", "from_chat_id", "message_id"), "forwardMessage": ("chat_id", "from_chat_id", "message_id"),
    "sendPhoto": ("chat_id", "photo"), "sendVideo": ("chat_id", "video"), "sendAnimation": ("chat_id", "animation"),
    "sendDocument": ("chat_id", "document"), "sendAudio": ("chat_id", "audio"), "sendVoice": ("chat_id", "voice"),
    "sendSticker": ("chat_id", "sticker"), "getChatMember": ("chat_id", "user_id"),
    "answerCallbackQuery": ("callback_query_id", "text"), "exportChatInviteLink": ("chat_id",),
    "getChat": ("chat_id",), "banChatMember": ("chat_id", "user_id"), "unbanChatMember": ("chat_id", "user_id"),
    "pinChatMessage": ("chat_id", "message_id"), "approveChatJoinRequest": ("chat_id", "user_id"),
}
ALIAS = {"replyText": "sendMessage"}
JSON_FIELDS = ("reply_markup", "entities", "link_preview_options", "reply_parameters")

def tg_call(token, method, data):
    r = requests.post(f"https://api.telegram.org/bot{token}/{method}", json=data, timeout=40)
    return r.json()

class BotAPI:
    def __init__(self, ctx): self.ctx = ctx
    def __getattr__(self, name):
        if name.startswith("_"): raise AttributeError(name)
        def call(*a, **kw):
            names = TG_ARGS.get(name, ("chat_id",))
            for n, v in zip(names, a): kw[n] = v
            if name in ("sendMessage", "replyText", "editMessageText"): kw.setdefault("parse_mode", "HTML")
            elif "caption" in kw: kw.setdefault("parse_mode", "HTML")
            for f in JSON_FIELDS:
                if isinstance(kw.get(f), str):
                    try: kw[f] = json.loads(kw[f])
                    except Exception: pass
            res = tg_call(self.ctx.token, ALIAS.get(name, name), kw)
            if not res.get("ok"):
                log_err(self.ctx.bot_id, self.ctx.cmd, f"{name}: {res.get('description')}")
                return None
            r = res.get("result")
            return D(r) if isinstance(r, dict) else r
        return call

def jl(v):
    try: return json.loads(v)
    except Exception: return v

class DataAPI:
    def __init__(self, bot_id, uid=0): self.b, self.u = bot_id, uid
    def getData(self, k, default=None):
        r = q("SELECT v FROM kv WHERE bot_id=? AND uid=? AND k=?", (self.b, self.u, str(k)), one=True)
        return jl(r["v"]) if r else default
    def saveData(self, k, v):
        q("INSERT OR REPLACE INTO kv VALUES(?,?,?,?)", (self.b, self.u, str(k), json.dumps(v, default=str)), commit=True)
    def deleteData(self, k):
        q("DELETE FROM kv WHERE bot_id=? AND uid=? AND k=?", (self.b, self.u, str(k)), commit=True)

class Res:
    def __init__(self, b, u, n): self.b, self.u, self.n = b, u, n
    def value(self):
        r = q("SELECT val FROM res WHERE bot_id=? AND uid=? AND name=?", (self.b, self.u, self.n), one=True)
        return r["val"] if r else 0
    def add(self, x):
        with LOCK: q("INSERT OR IGNORE INTO res VALUES(?,?,?,0)", (self.b, self.u, self.n), commit=True); \
            q("UPDATE res SET val=val+? WHERE bot_id=? AND uid=? AND name=?", (float(x), self.b, self.u, self.n), commit=True)
    def cut(self, x): self.add(-float(x))
    def set(self, x):
        q("INSERT OR REPLACE INTO res VALUES(?,?,?,?)", (self.b, self.u, self.n, float(x)), commit=True)

class Ctx:
    def __init__(self, bot_id, token, uid, message, depth=0):
        self.bot_id, self.token, self.uid, self.message, self.depth, self.cmd = bot_id, token, uid, message, depth, ""

class BotObj(DataAPI):
    def __init__(self, ctx): super().__init__(ctx.bot_id); self.ctx = ctx
    def handleNextCommand(self, cmd, **_):
        q("INSERT OR REPLACE INTO waits VALUES(?,?,?)", (self.ctx.bot_id, self.ctx.uid, cmd), commit=True)
    def runCommand(self, cmd, user_id=None, chat_id=None, message=None, params=None, options=None, **_):
        if self.ctx.depth >= 8: raise RuntimeError("runCommand depth limit")
        run_command(self.ctx.bot_id, cmd, user_id or chat_id or self.ctx.uid, message or self.ctx.message,
                    params or "", options, self.ctx.depth + 1)
    def runCommandAfter(self, delay_seconds, cmd, user_id=None, options=None, **_):
        b, uid, m = self.ctx.bot_id, user_id or self.ctx.uid, self.ctx.message
        t = threading.Timer(float(delay_seconds), lambda: POOL.submit(run_command, b, cmd, uid, m, "", options, 0))
        t.daemon = True; t.start()
    def sendMessage(self, text, **kw): return BotAPI(self.ctx).sendMessage(self.ctx.uid, text, **kw)
    def info(self, *a, **k): 
        r = q("SELECT * FROM bots WHERE id=?", (self.ctx.bot_id,), one=True)
        return D({"id": r["id"], "name": r["name"], "username": r["username"]})
    def broadcast(self, function=None, **_): raise RuntimeError("Bot.broadcast: use the panel Broadcast tool")

class UserObj(DataAPI):
    def __init__(self, ctx): super().__init__(ctx.bot_id, ctx.uid)

class Libs:
    class DateAndTime:
        @staticmethod
        def now(): return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    class Random:
        import random as _r
        randomInt = staticmethod(lambda a, b: Libs.Random._r.randint(a, b))
        @staticmethod
        def randomWeightedChoice(items, weights=None):
            return Libs.Random._r.choices(items, weights=weights)[0]
    def __init__(self, ctx):
        b = ctx.bot_id
        class Resources:
            anotherRes = staticmethod(lambda n, user=None: Res(b, int(user or 0), n))
            userRes = staticmethod(lambda n: Res(b, ctx.uid, n))
            globalRes = staticmethod(lambda n: Res(b, 0, n))
        self.Resources = Resources
        class Webhook:
            getUrlFor = staticmethod(lambda *a, **k: "")
        self.Webhook = Webhook

import builtins as _b
SAFE = {k: getattr(_b, k) for k in (
    "abs all any bool dict dir enumerate filter float format hasattr getattr int isinstance len list map max min "
    "range repr reversed round set sorted str sum tuple zip print Exception ValueError KeyError TypeError "
    "IndexError AttributeError RuntimeError StopIteration ZeroDivisionError True False None slice divmod chr ord "
    "bytes hex bin pow iter next type object callable setattr").split()}
CODE_CACHE = {}

def get_code(bot_id, name):
    r = q("SELECT code FROM commands WHERE bot_id=? AND name=?", (bot_id, name), one=True)
    if not r: return None
    h = hashlib.md5(r["code"].encode()).hexdigest()
    k = (bot_id, name)
    if CODE_CACHE.get(k, (None,))[0] != h:
        CODE_CACHE[k] = (h, compile(r["code"], f"<{name}>", "exec"))
    return CODE_CACHE[k][1]

def check_code(code):
    if re.search(r"^\s*(import|from)\s+\w+", code, re.M): return "import is not allowed in TPY"
    try: compile(code, "<cmd>", "exec")
    except SyntaxError as e: return f"Line {e.lineno}: {e.msg}"
    return None

def run_command(bot_id, name, uid, message, params="", options=None, depth=0):
    bot = q("SELECT * FROM bots WHERE id=?", (bot_id,), one=True)
    if not bot: return
    try:
        code = get_code(bot_id, name)
        if code is None: return
        ctx = Ctx(bot_id, bot["token"], uid, message, depth); ctx.cmd = name
        import json as _json
        env = {"__builtins__": SAFE, "Bot": BotObj(ctx), "bot": BotAPI(ctx), "User": UserObj(ctx),
               "libs": Libs(ctx), "message": message, "u": uid, "user": D({"id": uid}), "params": params,
               "options": options if options is not None else D(), "ReturnCommand": ReturnCommand,
               "json": _json, "time": time, "bunchify": lambda x: D(x) if isinstance(x, dict) else x,
               "request": D(), "chat": message.chat if isinstance(message, dict) else None,
               "inline_message_id": None}
        exec(code, env)
    except ReturnCommand:
        pass
    except Exception:
        log_err(bot_id, name, traceback.format_exc())

# ---------------------------------------------------------------- bot runners
RUNNERS = {}

class Runner(threading.Thread):
    def __init__(self, bot_id):
        super().__init__(daemon=True); self.bot_id = bot_id; self.stop_flag = threading.Event()
    def run(self):
        bot = q("SELECT * FROM bots WHERE id=?", (self.bot_id,), one=True)
        if not bot: return
        token, offset = bot["token"], 0
        try: tg_call(token, "deleteWebhook", {})
        except Exception: pass
        while not self.stop_flag.is_set():
            try:
                res = requests.post(f"https://api.telegram.org/bot{token}/getUpdates", timeout=40,
                                    json={"offset": offset, "timeout": 25,
                                          "allowed_updates": ["message", "callback_query", "chat_join_request", "my_chat_member"]}).json()
                if not res.get("ok"):
                    log_err(self.bot_id, "poll", res.get("description")); self.stop_flag.wait(10); continue
                for up in res["result"]:
                    offset = up["update_id"] + 1
                    POOL.submit(handle_update, self.bot_id, token, up)
            except Exception as e:
                log_err(self.bot_id, "poll", e); self.stop_flag.wait(5)

def start_bot(bot_id):
    with LOCK:
        r = RUNNERS.get(bot_id)
        if r and r.is_alive(): return
        r = Runner(bot_id); RUNNERS[bot_id] = r; r.start()
        q("UPDATE bots SET running=1 WHERE id=?", (bot_id,), commit=True)

def stop_bot(bot_id):
    with LOCK:
        r = RUNNERS.pop(bot_id, None)
        if r: r.stop_flag.set()
        q("UPDATE bots SET running=0 WHERE id=?", (bot_id,), commit=True)

def handle_update(bot_id, token, up):
    try:
        cb = up.get("callback_query"); jr = up.get("chat_join_request")
        if cb:
            frm, msg, text = cb["from"], D(cb.get("message") or {}), cb.get("data", "")
            tg_call(token, "answerCallbackQuery", {"callback_query_id": cb["id"]})
        elif up.get("message"):
            m = up["message"]; frm, msg, text = m["from"], D(m), m.get("text") or m.get("caption") or ""
        elif jr:
            frm, msg, text = jr["from"], D({"chat": jr["chat"], "from": jr["from"]}), ""
            if q("SELECT 1 FROM commands WHERE bot_id=? AND name='/handler_special_updates'", (bot_id,), one=True):
                run_command(bot_id, "/handler_special_updates", frm["id"], msg, "", D(up)); return
            return
        else: return
        uid = frm["id"]
        if frm.get("is_bot"): return
        with LOCK:
            q("INSERT OR IGNORE INTO users(bot_id,uid,first_name,username,joined,last_seen) VALUES(?,?,?,?,?,?)",
              (bot_id, uid, frm.get("first_name", ""), frm.get("username", ""), now(), now()), commit=True)
            q("UPDATE users SET last_seen=?, first_name=?, username=? WHERE bot_id=? AND uid=?",
              (now(), frm.get("first_name", ""), frm.get("username", ""), bot_id, uid), commit=True)
        u = q("SELECT banned FROM users WHERE bot_id=? AND uid=?", (bot_id, uid), one=True)
        if u and u["banned"]: return
        opts = D({"callback_data": text, "callback_query": cb}) if cb else D()
        if not msg.get("chat"): msg["chat"] = {"id": uid}
        with LOCK:
            w = q("SELECT cmd FROM waits WHERE bot_id=? AND uid=?", (bot_id, uid), one=True)
            if w: q("DELETE FROM waits WHERE bot_id=? AND uid=?", (bot_id, uid), commit=True)
        if w and not (text.startswith("/") and not cb and False):
            run_command(bot_id, w["cmd"], uid, msg, text, opts); return
        if text.startswith("/") or (cb and text):
            parts = text.split(None, 1); cmd = parts[0].split("@")[0]; params = parts[1] if len(parts) > 1 else ""
            if q("SELECT 1 FROM commands WHERE bot_id=? AND name=?", (bot_id, cmd), one=True):
                run_command(bot_id, cmd, uid, msg, params, opts); return
        run_command(bot_id, "@", uid, msg, text, opts)
    except Exception:
        log_err(bot_id, "handle_update", traceback.format_exc())

# ---------------------------------------------------------------- auth / security
def get_setting(k):
    r = q("SELECT v FROM settings WHERE k=?", (k,), one=True); return r["v"] if r else None
def set_setting(k, v): q("INSERT OR REPLACE INTO settings VALUES(?,?)", (k, v), commit=True)

def ensure_admin():
    if get_setting("admin_hash"): return
    pw = os.environ.get("ADMIN_PASSWORD")
    if not pw:
        pw = secrets.token_urlsafe(14)
        print(f"\n*** First run. Admin password (shown once): {pw}\n")
    set_setting("admin_hash", generate_password_hash(pw, method="pbkdf2:sha256:200000"))

FAILS = {}
@app.before_request
def guard():
    if request.endpoint == "static": return
    if request.method == "POST":
        tok = request.form.get("_csrf", "")
        if not session.get("csrf") or not hmac.compare_digest(tok, session["csrf"]): abort(400, "Bad CSRF token")
    if request.endpoint not in ("login", "health") and not session.get("auth"):
        return redirect(url_for("login"))

@app.after_request
def headers(r):
    r.headers.update({"X-Frame-Options": "DENY", "X-Content-Type-Options": "nosniff", "Referrer-Policy": "same-origin",
        "Content-Security-Policy": "default-src 'self'; style-src 'unsafe-inline'; img-src 'self' data:; form-action 'self'",
        "Cache-Control": "no-store"})
    return r

@app.context_processor
def inject():
    if "csrf" not in session: session["csrf"] = secrets.token_hex(16)
    return {"csrf": session["csrf"]}

# ---------------------------------------------------------------- UI
ICONS = {"bot": "M12 2v3M5 8h14a2 2 0 0 1 2 2v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-7a2 2 0 0 1 2-2zM9 13h.01M15 13h.01",
 "code": "M16 18l6-6-6-6M8 6l-6 6 6 6", "users": "M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2M9 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8zM23 21v-2a4 4 0 0 0-3-3.9M16 3.1a4 4 0 0 1 0 7.8",
 "plus": "M12 5v14M5 12h14", "trash": "M3 6h18M8 6V4h8v2M19 6l-1 14H6L5 6", "play": "M5 3l14 9-14 9z",
 "stop": "M6 6h12v12H6z", "dl": "M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M7 10l5 5 5-5M12 15V3",
 "ul": "M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M17 8l-5-5-5 5M12 3v12", "alert": "M12 9v4M12 17h.01M10.3 3.9L1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z",
 "home": "M3 12l9-9 9 9M5 10v10h5v-6h4v6h5V10", "out": "M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4M16 17l5-5-5-5M21 12H9",
 "save": "M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2zM17 21v-8H7v8M7 3v5h8",
 "ban": "M12 22a10 10 0 1 0 0-20 10 10 0 0 0 0 20zM4.9 4.9l14.2 14.2", "edit": "M12 20h9M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z"}
def ic(n, cls=""): return Markup(f'<svg class="i {cls}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="{ICONS[n]}"/></svg>')
app.jinja_env.globals["ic"] = ic

LAYOUT = """<!doctype html><html><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>BotPanel</title><style>
:root{--bg:#0f1219;--card:#181d29;--bd:#262d3d;--tx:#e6e9f0;--mu:#8b94a8;--ac:#4f8cff;--rd:#ef5350;--gn:#3ecf8e}
*{box-sizing:border-box}body{margin:0;font:14px system-ui,sans-serif;background:var(--bg);color:var(--tx);display:flex;min-height:100vh}
a{color:inherit;text-decoration:none}.i{width:16px;height:16px;vertical-align:-3px;margin-right:6px}
aside{width:220px;background:var(--card);border-right:1px solid var(--bd);padding:16px;position:sticky;top:0;height:100vh;overflow:auto}
aside h2{font-size:16px;margin:0 0 16px}aside a{display:block;padding:8px 10px;border-radius:8px;color:var(--mu);margin-bottom:2px}
aside a:hover,aside a.on{background:#232a3b;color:var(--tx)}main{flex:1;padding:24px;max-width:1100px;min-width:0}
.card{background:var(--card);border:1px solid var(--bd);border-radius:12px;padding:16px;margin-bottom:16px}
h1{font-size:20px;margin:0 0 16px}table{width:100%;border-collapse:collapse}td,th{padding:8px;border-bottom:1px solid var(--bd);text-align:left;vertical-align:middle}
th{color:var(--mu);font-weight:500}input,textarea,select{width:100%;background:#0f1219;color:var(--tx);border:1px solid var(--bd);border-radius:8px;padding:9px;font:inherit}
textarea{font:13px ui-monospace,monospace;min-height:420px;tab-size:4}
.btn{display:inline-block;background:var(--ac);color:#fff;border:0;border-radius:8px;padding:8px 14px;cursor:pointer;font:inherit}
.btn.g{background:#2a3246}.btn.r{background:var(--rd)}.btn.s{padding:5px 9px;font-size:12px}form.in{display:inline}
.tabs a{display:inline-block;padding:7px 12px;border-radius:8px;color:var(--mu)}.tabs a.on{background:#232a3b;color:var(--tx)}
.tag{padding:2px 8px;border-radius:99px;font-size:12px;background:#232a3b}.ok{color:var(--gn)}.bad{color:var(--rd)}
.flash{background:#1e2a44;border:1px solid var(--ac);padding:10px;border-radius:8px;margin-bottom:12px}.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(250px,1fr));gap:12px}pre{white-space:pre-wrap;font-size:12px;color:var(--mu);margin:0}
@media(max-width:760px){body{display:block}aside{width:auto;height:auto;position:static}}
</style></head><body>
{% if session.auth %}<aside><h2>{{ic('bot')}}BotPanel</h2><a href="/" class="{{'on' if nav=='home'}}">{{ic('home')}}Bots</a>
{% for b in bots %}<a href="/bot/{{b.id}}" class="{{'on' if bid==b.id}}">{{ic('code')}}{{b.name or b.username or b.id}}</a>{% endfor %}
<form method=post action="/logout"><input type=hidden name=_csrf value="{{csrf}}"><button class="btn g s" style="margin-top:14px">{{ic('out')}}Logout</button></form></aside>{% endif %}
<main>{% for m in get_flashed_messages() %}<div class=flash>{{m}}</div>{% endfor %}{{body}}</main></body></html>"""

def page(tpl, nav="", bid=None, **kw):
    body = render_template_string(tpl, **kw)
    return render_template_string(LAYOUT, body=Markup(body), nav=nav, bid=bid,
                                  bots=q("SELECT id,name,username FROM bots ORDER BY id") if session.get("auth") else [])

def bot_or_404(bid):
    b = q("SELECT * FROM bots WHERE id=?", (bid,), one=True)
    if not b: abort(404)
    return b

@app.route("/login", methods=["GET", "POST"])
def login():
    ip = request.remote_addr
    if request.method == "POST":
        n, t = FAILS.get(ip, (0, 0))
        if n >= 5 and time.time() - t < 300: abort(429, "Too many attempts, wait 5 minutes")
        if check_password_hash(get_setting("admin_hash"), request.form.get("password", "")):
            session.clear(); session["auth"] = True; session["csrf"] = secrets.token_hex(16); session.permanent = True
            FAILS.pop(ip, None); return redirect("/")
        FAILS[ip] = (n + 1, time.time()); flash("Wrong password")
    return render_template_string(LAYOUT, body=Markup(render_template_string(
        """<div class=card style="max-width:340px;margin:12vh auto"><h1>{{ic('bot')}}Sign in</h1><form method=post>
        <input type=hidden name=_csrf value="{{csrf}}"><input type=password name=password placeholder=Password autofocus required>
        <p><button class=btn>Login</button></p></form></div>""", csrf=session.setdefault("csrf", secrets.token_hex(16)))),
        nav="", bid=None, bots=[])

@app.post("/logout")
def logout(): session.clear(); return redirect("/login")

@app.route("/")
def home():
    rows = q("""SELECT b.*, (SELECT COUNT(*) FROM commands WHERE bot_id=b.id) cmds,
                (SELECT COUNT(*) FROM users WHERE bot_id=b.id) users FROM bots b ORDER BY id""")
    return page("""<h1>Your bots</h1><div class=grid>{% for b in rows %}<a class=card href="/bot/{{b.id}}"><b>{{ic('bot')}}{{b.name}}</b>
      <p class=tag>@{{b.username}}</p><p>{{b.cmds}} commands · {{b.users}} users</p>
      <span class="{{'ok' if b.running else 'bad'}}">{{'● running' if b.running else '● stopped'}}</span></a>{% endfor %}</div>
      <div class=card><h1>{{ic('plus')}}Add bot</h1><form method=post action="/bots/add"><input type=hidden name=_csrf value="{{csrf}}">
      <div class=row><input name=token placeholder="Bot token from @BotFather" required style="flex:1"><button class=btn>Add</button></div></form></div>""",
        nav="home", rows=rows)

@app.post("/bots/add")
def bot_add():
    token = request.form.get("token", "").strip()
    if not re.fullmatch(r"\d{6,12}:[\w-]{30,50}", token): flash("Invalid token format"); return redirect("/")
    try: info = tg_call(token, "getMe", {})
    except Exception as e: flash(f"Telegram error: {e}"); return redirect("/")
    if not info.get("ok"): flash("Telegram rejected this token"); return redirect("/")
    r = info["result"]
    try: q("INSERT INTO bots(name,username,token,created) VALUES(?,?,?,?)", (r["first_name"], r["username"], token, now()), commit=True)
    except sqlite3.IntegrityError: flash("Bot already added"); return redirect("/")
    seed_builtin(q("SELECT id FROM bots WHERE token=?", (token,), one=True)["id"])
    flash("Bot added with built-in commands loaded"); return redirect("/")

@app.route("/bot/<int:bid>")
def bot_view(bid):
    b = bot_or_404(bid); tab = request.args.get("tab", "commands"); s = request.args.get("s", "").strip()
    d = {"b": b, "tab": tab, "s": s}
    if tab == "commands":
        d["cmds"] = q("SELECT name,length(code) n,updated FROM commands WHERE bot_id=? AND name LIKE ? ORDER BY name", (bid, f"%{s}%"))
    elif tab == "users":
        pg = max(int(request.args.get("p", 1) or 1), 1)
        d["users"] = q("SELECT * FROM users WHERE bot_id=? AND (first_name LIKE ? OR username LIKE ? OR CAST(uid AS TEXT) LIKE ?) ORDER BY joined DESC LIMIT 100 OFFSET ?",
                       (bid, f"%{s}%", f"%{s}%", f"%{s}%", (pg - 1) * 100))
        d["total"] = q("SELECT COUNT(*) c FROM users WHERE bot_id=?", (bid,), one=True)["c"]; d["p"] = pg
    elif tab == "errors":
        d["errs"] = q("SELECT * FROM errors WHERE bot_id=? ORDER BY id DESC LIMIT 100", (bid,))
    return page(BOT_TPL, bid=bid, **d)

BOT_TPL = """<h1>{{ic('bot')}}{{b.name}} <span class=tag>@{{b.username}}</span> <span class="{{'ok' if b.running else 'bad'}}">{{'● running' if b.running else '● stopped'}}</span></h1>
<div class=row style="margin-bottom:14px"><div class=tabs>{% for t,l,i in [('commands','Commands','code'),('users','Users','users'),('errors','Errors','alert'),('settings','Settings','bot')] %}
<a class="{{'on' if tab==t}}" href="/bot/{{b.id}}?tab={{t}}">{{ic(i)}}{{l}}</a>{% endfor %}</div>
<form class=in method=post action="/bot/{{b.id}}/{{'stop' if b.running else 'start'}}"><input type=hidden name=_csrf value="{{csrf}}">
<button class="btn s {{'r' if b.running else ''}}">{{ic('stop' if b.running else 'play')}}{{'Stop' if b.running else 'Start'}}</button></form></div>
{% if tab=='commands' %}<div class=card><div class=row><form method=get style="flex:1"><input type=hidden name=tab value=commands><input name=s value="{{s}}" placeholder="Search commands"></form>
<a class=btn href="/bot/{{b.id}}/cmd">{{ic('plus')}}New command</a><a class="btn g" href="/bot/{{b.id}}/commands.txt">{{ic('dl')}}Export</a>
<form class=in method=post action="/bot/{{b.id}}/commands/builtin" onsubmit="return confirm('Overwrite same-named commands with built-in versions?')"><input type=hidden name=_csrf value="{{csrf}}"><button class="btn g">{{ic('ul')}}Load built-in</button></form></div>
<table><tr><th>Command<th>Size<th>Updated<th></tr>{% for c in cmds %}<tr><td>{{c.name}}<td>{{c.n}}<td>{{c.updated}}
<td><a class="btn g s" href="/bot/{{b.id}}/cmd?name={{c.name|urlencode}}">{{ic('edit')}}Edit</a>
<form class=in method=post action="/bot/{{b.id}}/cmd/delete" onsubmit="return confirm('Delete command?')"><input type=hidden name=_csrf value="{{csrf}}"><input type=hidden name=name value="{{c.name}}"><button class="btn r s">{{ic('trash')}}</button></form></tr>{% endfor %}</table></div>
<div class=card><b>{{ic('ul')}}Import commands</b> <small>(format: <code>=== /start ===</code> then code; updates existing, adds new)</small>
<form method=post action="/bot/{{b.id}}/commands/import" enctype=multipart/form-data><input type=hidden name=_csrf value="{{csrf}}"><p><input type=file name=file accept=".txt" required></p><button class=btn>Import</button></form></div>
{% elif tab=='users' %}<div class=card><div class=row><form method=get style="flex:1"><input type=hidden name=tab value=users><input name=s value="{{s}}" placeholder="Search id / name / username"></form>
<a class="btn g" href="/bot/{{b.id}}/users.csv">{{ic('dl')}}Export CSV</a></div><p>{{total}} users total</p>
<table><tr><th>ID<th>Name<th>Username<th>Last seen<th>Status<th></tr>{% for u in users %}<tr><td>{{u.uid}}<td>{{u.first_name}}<td>{{u.username}}<td>{{u.last_seen}}<td>{{'banned' if u.banned else 'active'}}
<td><form class=in method=post action="/bot/{{b.id}}/user/ban"><input type=hidden name=_csrf value="{{csrf}}"><input type=hidden name=uid value="{{u.uid}}"><button class="btn g s">{{ic('ban')}}{{'Unban' if u.banned else 'Ban'}}</button></form></tr>{% endfor %}</table>
<p class=row>{% if p>1 %}<a class="btn g s" href="?tab=users&p={{p-1}}&s={{s}}">Prev</a>{% endif %}{% if users|length==100 %}<a class="btn g s" href="?tab=users&p={{p+1}}&s={{s}}">Next</a>{% endif %}</p></div>
<div class=card><b>{{ic('ul')}}Import users (CSV: uid,first_name,username)</b><form method=post action="/bot/{{b.id}}/users/import" enctype=multipart/form-data>
<input type=hidden name=_csrf value="{{csrf}}"><p><input type=file name=file accept=".csv" required></p><button class=btn>Import</button></form></div>
<div class=card><b>Broadcast</b><form method=post action="/bot/{{b.id}}/broadcast"><input type=hidden name=_csrf value="{{csrf}}"><textarea name=text style="min-height:90px" placeholder="HTML message" required></textarea><p><button class=btn>Send to all active users</button></p></form></div>
{% elif tab=='errors' %}<div class=card><table><tr><th>Time<th>Command<th>Error</tr>{% for e in errs %}<tr><td>{{e.ts}}<td>{{e.cmd}}<td><pre>{{e.msg}}</pre></tr>{% endfor %}</table></div>
{% else %}<div class=card><b>Update token</b><form method=post action="/bot/{{b.id}}/token"><input type=hidden name=_csrf value="{{csrf}}"><div class=row><input name=token placeholder="New bot token" required style="flex:1"><button class=btn>Save</button></div></form></div>
<div class=card><b class=bad>Danger zone</b><form method=post action="/bot/{{b.id}}/delete" onsubmit="return confirm('Delete bot and ALL its data?')"><input type=hidden name=_csrf value="{{csrf}}"><p><button class="btn r">{{ic('trash')}}Delete bot</button></p></form></div>{% endif %}"""

@app.post("/bot/<int:bid>/<action>")
def bot_action(bid, action):
    bot_or_404(bid)
    if action == "start": start_bot(bid)
    elif action == "stop": stop_bot(bid)
    elif action == "delete":
        stop_bot(bid)
        for t in ("commands", "users", "kv", "waits", "res", "errors"): q(f"DELETE FROM {t} WHERE bot_id=?", (bid,), commit=True)
        q("DELETE FROM bots WHERE id=?", (bid,), commit=True); flash("Bot deleted"); return redirect("/")
    elif action == "token":
        t = request.form.get("token", "").strip(); r = tg_call(t, "getMe", {})
        if not r.get("ok"): flash("Invalid token"); return redirect(f"/bot/{bid}?tab=settings")
        stop_bot(bid); q("UPDATE bots SET token=?,username=?,name=? WHERE id=?", (t, r["result"]["username"], r["result"]["first_name"], bid), commit=True); flash("Token updated; start the bot")
    else: abort(404)
    return redirect(f"/bot/{bid}")

@app.route("/bot/<int:bid>/cmd", methods=["GET", "POST"])
def cmd_edit(bid):
    b = bot_or_404(bid); name = request.values.get("name", "")
    if request.method == "POST":
        newname, code = request.form.get("newname", "").strip(), request.form.get("code", "").replace("\r\n", "\n")
        if not newname or len(newname) > 100: flash("Command name required (max 100 chars)"); return redirect(request.url)
        err = check_code(code)
        if err: flash("Not saved - " + err); return page(CMD_TPL, bid=bid, b=b, name=name, newname=newname, code=code)
        if name and name != newname: q("DELETE FROM commands WHERE bot_id=? AND name=?", (bid, name), commit=True)
        q("INSERT OR REPLACE INTO commands VALUES(?,?,?,?)", (bid, newname, code, now()), commit=True)
        flash("Saved"); return redirect(f"/bot/{bid}/cmd?name={newname}")
    r = q("SELECT code FROM commands WHERE bot_id=? AND name=?", (bid, name), one=True) if name else None
    return page(CMD_TPL, bid=bid, b=b, name=name, newname=name or "/", code=r["code"] if r else "")

CMD_TPL = """<h1>{{ic('code')}}{{'Edit command' if name else 'New command'}}</h1><div class=card><form method=post>
<input type=hidden name=_csrf value="{{csrf}}"><input type=hidden name=name value="{{name}}"><p><input name=newname value="{{newname}}" placeholder="/command" required></p>
<textarea name=code spellcheck=false>{{code}}</textarea><p class=row><button class=btn>{{ic('save')}}Save</button><a class="btn g" href="/bot/{{b.id}}">Back</a></p></form></div>"""

@app.post("/bot/<int:bid>/cmd/delete")
def cmd_delete(bid):
    bot_or_404(bid); q("DELETE FROM commands WHERE bot_id=? AND name=?", (bid, request.form.get("name", "")), commit=True)
    flash("Command deleted"); return redirect(f"/bot/{bid}")

def parse_commands(text):
    parts = re.split(r"^=== (.+?) ===\s*$", text.replace("\r\n", "\n"), flags=re.M)
    return [(parts[i].strip(), parts[i + 1].strip("\n")) for i in range(1, len(parts) - 1, 2)]

@app.post("/bot/<int:bid>/commands/import")
def cmds_import(bid):
    bot_or_404(bid); f = request.files.get("file")
    try: items = parse_commands(f.read().decode("utf-8"))
    except Exception: flash("Could not read file"); return redirect(f"/bot/{bid}")
    bad = [(n, check_code(c)) for n, c in items if check_code(c)]
    for n, c in items:
        q("INSERT OR REPLACE INTO commands VALUES(?,?,?,?)", (bid, n, c, now()), commit=True)
    flash(f"Imported {len(items)} commands" + (f"; {len(bad)} have syntax issues: " + ", ".join(f"{n} ({e})" for n, e in bad[:5]) if bad else ""))
    return redirect(f"/bot/{bid}")

@app.route("/bot/<int:bid>/commands.txt")
def cmds_export(bid):
    bot_or_404(bid)
    txt = "\n\n".join(f"=== {r['name']} ===\n{r['code']}" for r in q("SELECT * FROM commands WHERE bot_id=? ORDER BY name", (bid,)))
    return Response(txt, mimetype="text/plain", headers={"Content-Disposition": f"attachment; filename=bot_{bid}_commands.txt"})

@app.route("/bot/<int:bid>/users.csv")
def users_export(bid):
    bot_or_404(bid); out = io.StringIO(); w = csv.writer(out); w.writerow(["uid", "first_name", "username", "banned", "joined", "last_seen"])
    for r in q("SELECT * FROM users WHERE bot_id=?", (bid,)):
        w.writerow([r["uid"], *[("'" + str(v) if str(v)[:1] in "=+-@" else v) for v in (r["first_name"], r["username"])], r["banned"], r["joined"], r["last_seen"]])
    return Response(out.getvalue(), mimetype="text/csv", headers={"Content-Disposition": f"attachment; filename=bot_{bid}_users.csv"})

@app.post("/bot/<int:bid>/users/import")
def users_import(bid):
    bot_or_404(bid); n = 0
    try:
        for row in csv.reader(io.StringIO(request.files["file"].read().decode("utf-8-sig"))):
            if row and row[0].strip().lstrip("-").isdigit():
                q("INSERT OR IGNORE INTO users(bot_id,uid,first_name,username,joined,last_seen) VALUES(?,?,?,?,?,?)",
                  (bid, int(row[0]), (row[1] if len(row) > 1 else "")[:100], (row[2] if len(row) > 2 else "")[:100], now(), now()), commit=True); n += 1
    except Exception: flash("Bad CSV file"); return redirect(f"/bot/{bid}?tab=users")
    flash(f"Imported {n} users"); return redirect(f"/bot/{bid}?tab=users")

@app.post("/bot/<int:bid>/user/ban")
def user_ban(bid):
    bot_or_404(bid); q("UPDATE users SET banned=1-banned WHERE bot_id=? AND uid=?", (bid, int(request.form.get("uid", 0))), commit=True)
    return redirect(f"/bot/{bid}?tab=users")

@app.post("/bot/<int:bid>/broadcast")
def broadcast(bid):
    b = bot_or_404(bid); text = request.form.get("text", "")
    uids = [r["uid"] for r in q("SELECT uid FROM users WHERE bot_id=? AND banned=0", (bid,))]
    def job():
        for uid in uids:
            try: tg_call(b["token"], "sendMessage", {"chat_id": uid, "text": text, "parse_mode": "HTML"})
            except Exception: pass
            time.sleep(0.05)  # ~20 msg/s, under Telegram's limit
    threading.Thread(target=job, daemon=True).start()
    flash(f"Broadcast started for {len(uids)} users"); return redirect(f"/bot/{bid}?tab=users")

# ===================== BUILT-IN COMMANDS: bot 29194077 (Yono Big Promocodes) =====================
# Plain Python source. The runtime above supplies Bot, bot, User, libs, message, u, params, options.
BUILTIN_COMMANDS = {
    '//0': r'''EMOJI_CUSTOM = "6129840374971112593"

def escape_html(text):
    if not text:
        return ""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
key = Bot.getData("KEY") or "not set"
PaymentCh = Bot.getData("Botpaychannel") or "not set"
Botpayname = Bot.getData("BotPayComm") or "Payment"
GUID = Bot.getData("GUID") or "not set"
MID = Bot.getData("MID") or "not set"
Mkey = Bot.getData("Mkey") or "not set"
Token = Bot.getData("TOKEN")
AllBanUsers = (Bot.getData("AllBanUsers") or "12345").split(",")

if str(message.chat.id) in AllBanUsers:
    bot.replyText(u, f'<tg-emoji emoji-id="{EMOJI_CUSTOM}">🚫</tg-emoji><b><i> You are Banned from using this Bot</i></b>')
    raise ReturnCommand()

AllBanWallets = (Bot.getData("AllBanWallets") or "12345").split(",")
wallet = Bot.getData("UserWallet" + str(u))
if wallet and wallet in AllBanWallets:
    bot.replyText(u, f'<tg-emoji emoji-id="{EMOJI_CUSTOM}">🚫</tg-emoji><b><i> Your Wallet is Banned in this Bot</i></b>')
    raise ReturnCommand()

WitdMode = Bot.getData("WithdrawMode") or "ON"
if WitdMode == "OFF":
    WOT = Bot.getData("WOT") or f'<tg-emoji emoji-id="{EMOJI_CUSTOM}">🚫</tg-emoji><b>Withdrawals are currently turned off.</b>'
    bot.replyText(u, WOT)
    raise ReturnCommand()

Gatewaysett0 = Bot.getData("gatewaynow") or "Not Set"
Gatewaysett011 = Bot.getData("gatewaytype") or "https://RJWallet.in/"
gateway_map = {
    "Rjwallet": "RJ Wallet",
    "full2sms": "F2S Wallet",
    "Tgwallet": "TG Wallet",
    "payzy": "Payzy Wallet",
    "Fxl": "FXL Wallet",
    "lifafawala": "LifafaWala Wallet",
    "vsv": "VSV Wallet"
}
Gatewaysett20 = gateway_map.get(Gatewaysett0, "Not Set")

if Gatewaysett0 == "Not Set":
    bot.replyText(u, f'<tg-emoji emoji-id="{EMOJI_CUSTOM}">🚫</tg-emoji><b>Gateway Not Set</b>')
    raise ReturnCommand()

# Parse options


amount = 1
bal = float(libs.Resources.anotherRes("Balance", user=u).value())
minWith = float(Bot.getData("MinWith") or 5)
tax = float(Bot.getData("Tax") or 0)
WithdrawC = int(Bot.getData("WithdrawC") or 0)
wallet = Bot.getData("UserWallet" + str(u)) or "8796365847"
hide_wallet = f"{wallet[0:3]}xxxxx{wallet[8:10]}"
AmountAftrTax = amount - (amount * tax / 100)

if bal >= amount and amount >= minWith:
    libs.Resources.anotherRes("Balance", user=u).cut(amount)
    libs.Resources.anotherRes("Withdraw", user=u).add(amount)
    libs.Resources.globalRes("BotWithdraws").add(amount)
    Bot.saveData("WithdrawC", WithdrawC + 1)
url = f"http://saathigateway.com/api?token={Token}&key={key}&paytoNumber={wallet}&amount={AmountAftrTax}&comment={Botpayname}"
Bot.sendMessage(url)''',
    '//onogf': r'''# Get Admin list
AllBotAdminss = Bot.getData("AllBotAdminss") or []

# Check Admin
is_Admin = str(u) in [str(userid) for userid in AllBotAdminss]

if not is_Admin:
    bot.replyText(u, "<b><i>🚫 You Are Not Authorized! 👮‍♂️</i></b>")
    raise ReturnCommand()

# Check if user sent text
if not message.text:
    bot.replyText(u, "⚠️ Please send a text message containing a Telegram link.")
    raise ReturnCommand()

# Check if it's a Telegram link
if message.text.startswith("https://t.me/") or message.text.startswith("t.me/"):
    Bot.saveData("SavedLinks", message.text)   # ✅ save directly

    bot.replyText(u, f"✅ Link saved successfully!\n🔗 {message.text}")
else:
    bot.replyText(u, "⚠️ Please send a valid Telegram link (e.g., https://t.me/xyz).")
    ''',
    '/AutoCheckBot': r'''# ==========================================
# 7-DAY AUTO CHECK
#   users >= 50 -> bot added to Multi-Broadcast selection list (bot 72833143)
#   users <  50 -> bot stopped (NOT added to the list)
#
#   Messages:
#     1) This bot's admins  -> sent from this bot
#          added   : "Hey {ownername} ... bot was added in broadcast list ..."
#          stopped : short stop message (no bot id)
#     2) Bot 72833143 (panel) -> ONLY chat id 6925391837, with ALL details
# ==========================================

# Multi-Broadcast panel bot (selection list lives here, in the admin's User data)
PANEL_BOT_ID = "72833143"
PANEL_API_KEY = "mdUuzOnyArsaQ5nhdlJaBU2u9B2SbM_05rnoba21Y-U"
SELECTION_KEY = "selected_broadcast_bots_list"

# Panel admin: selection list updated + full-detail message from 72833143
PANEL_ADMINS = ["6925391837"]
PANEL_NOTIFY_CHAT = "6925391837"

CONTACT = "@Jenish_Dobariya1"
MIN_USERS = 50

users = int(Bot.getData("total_users") or 0)

def esc(x):
    return str(x).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

def get_chat_info(token, uid):
    try:
        r = HTTP.get("https://api.telegram.org/bot" + token + "/getChat?chat_id=" + str(uid)).json()
        return r.get("result", {}) or {}
    except:
        return {}

def get_name(token, uid):
    # first name (+ last name) of a Telegram user; fallback "Owner"
    res = get_chat_info(token, uid)
    nm = str(res.get("first_name") or "").strip()
    ln = str(res.get("last_name") or "").strip()
    full = (nm + " " + ln).strip()
    if full:
        return full
    return "Owner"

def get_username(token, uid):
    un = get_chat_info(token, uid).get("username")
    if un:
        return "@" + str(un)
    return "N/A"

def notify_admins(admins, make_text):
    # every admin of THIS bot, sent from this bot (text can use admin's own name)
    sent = []
    for adm in admins:
        if str(adm) in sent:
            continue
        sent.append(str(adm))
        try:
            bot.sendMessage(chat_id=adm, text=make_text(adm), parse_mode="HTML")
        except:
            pass

def notify_panel(text):
    # sent from bot 72833143, ONLY to PANEL_NOTIFY_CHAT
    try:
        pinfo = Bot.info(bot_id=PANEL_BOT_ID, api_key=PANEL_API_KEY)
        ptoken = str(pinfo.token)
        HTTP.get(
            "https://api.telegram.org/bot" + ptoken + "/sendMessage"
            + "?chat_id=" + str(PANEL_NOTIFY_CHAT)
            + "&parse_mode=HTML"
            + "&text=" + encodeURIComponent(text)
        )
    except:
        pass

def now_text():
    # IST date + 12-hour time with AM/PM, e.g. 17-10-2026 03:45 PM IST
    try:
        t = int(time.time()) + 19800
        days = t // 86400
        rem = t % 86400
        hh = rem // 3600
        mm = (rem % 3600) // 60
        z = days + 719468
        era = z // 146097
        doe = z - era * 146097
        yoe = (doe - doe // 1460 + doe // 36524 - doe // 146096) // 365
        y = yoe + era * 400
        doy = doe - (365 * yoe + yoe // 4 - yoe // 100)
        mp = (5 * doy + 2) // 153
        d = doy - (153 * mp + 2) // 5 + 1
        if mp < 10:
            mo = mp + 3
        else:
            mo = mp - 9
        if mo <= 2:
            y = y + 1
        ap = "AM"
        if hh >= 12:
            ap = "PM"
        h12 = hh % 12
        if h12 == 0:
            h12 = 12
        return "%02d-%02d-%04d %02d:%02d %s IST" % (d, mo, y, h12, mm, ap)
    except:
        return "N/A"

try:
    info = Bot.info()
    bot_id = str(info.bot_id)
    bot_username = str(info.bot_username or "")
    my_token = str(info.token)

    admins = Bot.getData("AllBotAdminss") or []
    owner_id = str(Bot.getData("Owner") or (admins[0] if admins else ""))
    owner_name = esc(get_name(my_token, owner_id)) if owner_id else "Owner"
    owner_un = get_username(my_token, owner_id) if owner_id else "N/A"
    now_txt = now_text()

    if users < MIN_USERS:
        # ---------- LESS THAN 50 USERS: notify + stop ----------
        stop_txt = (
            "<b>🛑 Bot Stopped Automatically\n\n"
            "🤖 Bot: @" + bot_username + "\n"
            "👥 Total Users: " + str(users) + "\n"
            "❌ Required Users: " + str(MIN_USERS) + "\n"
            "📅 7 Days Completed.</b>"
        )
        notify_admins(admins, lambda a: stop_txt)
        notify_panel(
            "<b>🛑 Bot Stopped Automatically\n\n"
            "🤖 Bot: @" + bot_username + "\n"
            "🆔 Bot ID: " + bot_id + "\n"
            "👤 Owner: " + owner_name + "\n"
            "👥 Total Users: " + str(users) + "\n"
            "❌ Required Users: " + str(MIN_USERS) + "\n"
            "📋 Not added to Selection List\n"
            "🕒 " + now_txt + "\n"
            "📅 7 Days Completed.</b>"
        )
        Account.stop_bot(info.bot_id)

    else:
        # ---------- 50 OR MORE USERS: add to selection list of 72833143 ----------
        added = False
        err = ""
        total_sel = 0
        for panel_admin in PANEL_ADMINS:
            try:
                selected = User.getData(
                    SELECTION_KEY,
                    user=str(panel_admin),
                    bot_id=PANEL_BOT_ID,
                    api_key=PANEL_API_KEY
                ) or []
                if not isinstance(selected, list):
                    selected = []

                if bot_id not in [str(x) for x in selected]:
                    selected.append(bot_id)
                    User.saveData(
                        SELECTION_KEY,
                        selected,
                        user=str(panel_admin),
                        bot_id=PANEL_BOT_ID,
                        api_key=PANEL_API_KEY
                    )
                total_sel = len(selected)
                added = True
            except Exception as e2:
                err = str(e2)[:200]

        if added:
            # message to this bot's admins (from this bot)
            def added_text(adm):
                if str(adm) == str(owner_id):
                    nm = owner_name
                else:
                    nm = esc(get_name(my_token, adm))
                return (
                    "<b>👋 Hey " + nm + "\n\n"
                    "✅ Your bot was added in broadcast list.\n"
                    "📢 Now you will see ads or broadcast in this bot.\n"
                    "👨‍💻 That was done by " + CONTACT + ".\n\n"
                    "🛑 If you want to stop it, or if your bot is still not over, "
                    "then contact 👉 " + CONTACT + "</b>"
                )
            notify_admins(admins, added_text)

            # full-detail message to 6925391837 (from bot 72833143)
            try:
                bot_status = str(info.status or "N/A")
            except:
                bot_status = "N/A"
            try:
                bot_fname = esc(info.first_name or "N/A")
            except:
                bot_fname = "N/A"

            notify_panel(
                "✅ <b>Bot Added to Selection List</b>\n"
                "━━━━━━━━━━━━━━━━━━\n\n"
                "🤖 <b>Bot:</b> @" + bot_username + "\n"
                "📛 <b>Bot Name:</b> " + bot_fname + "\n"
                "🆔 <b>Bot ID:</b> <code>" + bot_id + "</code>\n"
                "⚙️ <b>Status:</b> " + esc(bot_status) + "\n\n"
                "👤 <b>Owner Name:</b> " + owner_name + "\n"
                "🔗 <b>Owner Username:</b> " + esc(owner_un) + "\n"
                "🆔 <b>Owner ID:</b> <code>" + owner_id + "</code>\n"
                "👮 <b>Bot Admins:</b> " + str(len(admins)) + "\n\n"
                "👥 <b>Total Users:</b> " + str(users) + "\n"
                "✔️ <b>Required Users:</b> " + str(MIN_USERS) + "\n"
                "📋 <b>Total Selected Bots:</b> " + str(total_sel) + "\n"
                "🕒 <b>Added On:</b> " + now_txt + "\n"
                "📅 7 Days Completed"
            )
        else:
            notify_panel(
                "⚠️ <b>Could not add bot to Selection List</b>\n\n"
                "🤖 Bot: @" + bot_username + "\n"
                "🆔 Bot ID: " + bot_id + "\n"
                "👤 Owner: " + owner_name + "\n"
                "👥 Total Users: " + str(users) + "\n"
                "Reason: " + err
            )

except Exception as e:
    notify_panel("⚠️ <b>AutoCheck Error</b>\n\n" + esc(str(e)[:300]))
    ''',
    '/BROADCAST': r'''# Admin Check
AllBotAdminss = Bot.getData("AllBotAdminss") or []
is_Admin = False
for userid in AllBotAdminss:
    if str(u) == str(userid):
        is_Admin = True
if is_Admin != True:
    bot.sendMessage("<b><i>🚫You Are Not This Bot Admin</i></b>")
    raise ReturnCommand()

# Check if users exist
FulBotUsrs = Bot.getData("FulBotUsrs") or []
user_count = len(FulBotUsrs)

# Build attractive message
text = f"""<b>📢 Broadcast Center</b>

👥 <b>Total Users:</b> <code>{user_count}</code>
<i>Choose your broadcast type below</i>

━━━━━━━━━━━━━━━━━━"""

# Buttons
markup = InlineKeyboardMarkup()
markup.add(
    InlineKeyboardButton("📢 Instant Broadcast", callback_data="/broadcast"))
markup.add(InlineKeyboardButton("📅 Schedule Broadcast", callback_data="/SCHEDULE_BROADCAST"))

markup.add(InlineKeyboardButton("🔙 Back to Admin", callback_data="/admin AP"))

# ✅ EDIT existing message if possible, else send new
try:
    bot.editMessageText(
        chat_id=u,
        message_id=message.message_id,
        text=text,
        reply_markup=markup,
        parse_mode="HTML"
    )
except:
    bot.sendMessage(text, reply_markup=markup, parse_mode="HTML")''',
    '/CHANNEL_BC': r'''# Admin Check
AllBotAdminss = Bot.getData("AllBotAdminss") or []
is_Admin = False
for userid in AllBotAdminss:
    if str(u) == str(userid):
        is_Admin = True
if is_Admin != True:
    bot.sendMessage("<b><i>🚫You Are Not This Bot Admin</i></b>")
    raise ReturnCommand()

# Get channel count
AMCTGID = Bot.getData("AllMainChTGID") or []
channel_count = len(AMCTGID)
BCH = Bot.getData("BC_History") or []

# Build attractive message
text = f"""<b>📢 Channel Broadcast Center</b>

📊 <b>Total Channels:</b> <code>{channel_count}</code>
🗂 <b>Saved Broadcasts:</b> <code>{len(BCH)}</code>
<i>Choose your broadcast type below</i>

━━━━━━━━━━━━━━━━━━"""

markup = InlineKeyboardMarkup()
markup.add(
    InlineKeyboardButton("📢 Instant Broadcast", callback_data="/PIRO_Broadcast"))
markup.add(InlineKeyboardButton("📅 Schedule Broadcast", callback_data="/PIRO_ScheduleBroadcast")
)
markup.add(InlineKeyboardButton("🗑 Delete Broadcast", callback_data="/PIRO_BC_Delete"))
markup.add(InlineKeyboardButton("🔙 Back to Channels", callback_data="/PIRO_Chanel_Pannel"))

# ✅ Edit existing message if possible
try:
    bot.editMessageText(
        chat_id=u,
        message_id=message.message_id,
        text=text,
        reply_markup=markup,
        parse_mode="HTML"
    )
except:
    bot.sendMessage(text, reply_markup=markup, parse_mode="HTML")''',
    '/ClaimVouchers': r'''# /watchad

if not libs.tbcads.reward_ad(
    "🎁 Watch An Ad To Get Your Voucher & Earn Lot Of Money",
    then="/claimvoucher"
):
    bot.sendMessage("❌ No ad is available right now. Please try again later.")''',
    '/EditBtnTxtClear': r'''# ============================================================
# /EditBtnTxtClear  -  clear one game (or "all") with confirmation
#   params:  "<section>"      -> ask for confirmation
#            "<section> go"   -> really clear
# ============================================================

EMOJI_FAILED="6129840374971112593"
EMOJI_SUCCESS="6203820727083208720"
EMOJI_WARN="5447644880824181073"

admin_ids=[str(x) for x in (Bot.getData("AllBotAdminss") or [])]
owner=Bot.getData("Owner")
if owner:
    admin_ids.append(str(owner))
if str(u) not in admin_ids:
    bot.editMessageText(chat_id=u,message_id=call.message.message_id,text=f'<tg-emoji emoji-id="{EMOJI_FAILED}">🚫</tg-emoji> <b>Access Denied</b>',parse_mode="HTML")
    raise ReturnCommand

CFG={"yono":("Yono 777","YONO777_TEXT"),"lucky":("Lucky Gullak","LUCKYGULLAK_TEXT"),"jaiho":("Jaiho 777","JAIHO777_TEXT")}

parts=str(params).strip().lower().split()
target=parts[0] if parts else ""
confirmed=len(parts)>1 and parts[1]=="go"

if target!="all" and target not in CFG:
    Bot.runCommand("/EditButtonTextPanel")
    raise ReturnCommand

if target=="all":
    label="ALL texts (Yono 777, Lucky Gullak, Jaiho 777)"
    back="/EditButtonTextPanel"
else:
    label=CFG[target][0]+" text"
    back="/EditButtonTextPanel "+target

if not confirmed:
    markup={"inline_keyboard":[[
        {"text":"Yes, clear","callback_data":"/EditBtnTxtClear "+target+" go","icon_custom_emoji_id":EMOJI_SUCCESS},
        {"text":"Cancel","callback_data":back,"icon_custom_emoji_id":EMOJI_FAILED}
    ]]}
    msg=f'<tg-emoji emoji-id="{EMOJI_WARN}">⚠️</tg-emoji> <b>Are you sure?</b>\n\nThis will permanently clear <b>{label}</b> (text and media).'
    try:
        bot.editMessageText(chat_id=u,message_id=call.message.message_id,text=msg,parse_mode="HTML",reply_markup=markup)
    except:
        bot.replyText(u,msg,parse_mode="HTML",reply_markup=markup)
    raise ReturnCommand

if target=="all":
    for k in CFG:
        Bot.saveData(CFG[k][1],"")
        Bot.saveData(CFG[k][1]+"_AT","")
        Bot.saveData(CFG[k][1]+"_MEDIA","")
else:
    Bot.saveData(CFG[target][1],"")
    Bot.saveData(CFG[target][1]+"_AT","")
    Bot.saveData(CFG[target][1]+"_MEDIA","")

msg=f'<tg-emoji emoji-id="{EMOJI_SUCCESS}">✅</tg-emoji> <b>{label} cleared</b>'
try:
    bot.editMessageText(chat_id=u,message_id=call.message.message_id,text=msg,parse_mode="HTML",reply_markup={"inline_keyboard":[[{"text":"🔙 Back","callback_data":back}]]})
except:
    bot.replyText(u,msg,parse_mode="HTML",reply_markup={"inline_keyboard":[[{"text":"🔙 Back","callback_data":back}]]})''',
    '/EditBtnTxtPreview': r'''# ============================================================
# /EditBtnTxtPreview  -  ONE message, exactly as users see it
# (supports text, photo, video, GIF, file, audio, voice)
# ============================================================

EMOJI_FAILED = "6129840374971112593"
EMOJI_STAR = "5469741319330996757"
EMOJI_WARN = "5447644880824181073"

admin_ids = [str(x) for x in (Bot.getData("AllBotAdminss") or [])]
admin_ids.append(str(Bot.getData("Owner")))

if str(u) not in admin_ids:
    bot.replyText(u, f'<tg-emoji emoji-id="{EMOJI_FAILED}">🚫</tg-emoji> <b>Access Denied</b>', parse_mode="HTML")
    raise ReturnCommand

CFG = {
    "yono": ("Yono 777", "YONO777_TEXT"),
    "lucky": ("Lucky Gullak", "LUCKYGULLAK_TEXT"),
    "jaiho": ("Jaiho 777", "JAIHO777_TEXT"),
}

target = str(params).strip().lower()

if target not in CFG:
    Bot.runCommand("/EditButtonTextPanel")
    raise ReturnCommand

name, key = CFG[target]
text = Bot.getData(key) or ""
media = Bot.getData(key + "_MEDIA")


def send_media(mt, chat, fid, cap, pm, mk):
    kw = {"chat_id": str(chat)}
    if cap:
        kw["caption"] = cap
    if pm:
        kw["parse_mode"] = pm
    if mk:
        kw["reply_markup"] = mk
    if mt == "photo":
        kw["photo"] = fid
        return bot.sendPhoto(**kw)
    if mt == "video":
        kw["video"] = fid
        return bot.sendVideo(**kw)
    if mt == "animation":
        kw["animation"] = fid
        return bot.sendAnimation(**kw)
    if mt == "document":
        kw["document"] = fid
        return bot.sendDocument(**kw)
    if mt == "audio":
        kw["audio"] = fid
        return bot.sendAudio(**kw)
    if mt == "voice":
        kw["voice"] = fid
        return bot.sendVoice(**kw)
    raise Exception("unknown media type")


def send_content(chat, text, media, mk):
    mt = None
    fid = None
    if isinstance(media, dict):
        mt = media.get("type")
        fid = media.get("file_id")
    for attempt in range(3):
        pm = "HTML"
        m2 = mk
        if attempt == 1:
            m2 = None
        if attempt == 2:
            pm = None
            m2 = None
        try:
            if mt and fid:
                send_media(mt, chat, fid, text, pm, m2)
            elif pm:
                if m2:
                    bot.replyText(chat, text, parse_mode=pm, reply_markup=m2)
                else:
                    bot.replyText(chat, text, parse_mode=pm)
            else:
                bot.replyText(chat, text)
            return True
        except Exception:
            pass
    return False


markup = {
    "inline_keyboard": [
        [
            {"text": "Edit", "callback_data": "/EditBtnTxtStart " + target, "icon_custom_emoji_id": EMOJI_STAR},
            {"text": "Back", "callback_data": "/EditButtonTextPanel " + target}
        ]
    ]
}

has_media = isinstance(media, dict) and media.get("file_id")

if not text.strip() and not has_media:
    bot.replyText(
        u,
        f'<tg-emoji emoji-id="{EMOJI_WARN}">⚠️</tg-emoji> <b>{name}</b> has no text yet.',
        parse_mode="HTML",
        reply_markup=markup
    )
    raise ReturnCommand

send_content(u, text, media, markup)''',
    '/EditBtnTxtSave': r'''EMOJI_FAILED="6129840374971112593"
EMOJI_SUCCESS="6203820727083208720"
EMOJI_WARN="5447644880824181073"

admin_ids=[str(x) for x in (Bot.getData("AllBotAdminss") or [])]
owner_id=Bot.getData("Owner")
if owner_id:
    admin_ids.append(str(owner_id))
if str(u) not in admin_ids:
    bot.replyText(u,f'<tg-emoji emoji-id="{EMOJI_FAILED}">🚫</tg-emoji> <b>Access Denied</b>',parse_mode="HTML")
    raise ReturnCommand

CFG={"yono":("Yono 777","YONO777_TEXT"),"lucky":("Lucky Gullak","LUCKYGULLAK_TEXT"),"jaiho":("Jaiho 777","JAIHO777_TEXT")}
target=str(User.getData("EBT_TARGET"))
if target not in CFG:
    bot.replyText(u,f'<tg-emoji emoji-id="{EMOJI_WARN}">⚠️</tg-emoji> <b>Session expired.</b> Open the panel again.',parse_mode="HTML",reply_markup={"remove_keyboard":True})
    Bot.runCommand("/EditButtonTextPanel")
    raise ReturnCommand

name,key=CFG[target]

incoming=""
ents=None
try:
    if message.caption:
        incoming=message.caption
        ents=message.caption_entities
    elif message.text:
        incoming=message.text
        ents=message.entities
except:
    try:
        incoming=message.text or ""
        ents=message.entities
    except:
        incoming=""
        ents=None

mtype=None
fid=None

try:
    if message.photo:
        mtype="photo"
        fid=message.photo[-1].file_id
except:
    pass
if not mtype:
    try:
        if message.video:
            mtype="video"
            fid=message.video.file_id
    except:
        pass
if not mtype:
    try:
        if message.animation:
            mtype="animation"
            fid=message.animation.file_id
    except:
        pass
if not mtype:
    try:
        if message.document:
            mtype="document"
            fid=message.document.file_id
    except:
        pass
if not mtype:
    try:
        if message.audio:
            mtype="audio"
            fid=message.audio.file_id
    except:
        pass
if not mtype:
    try:
        if message.voice:
            mtype="voice"
            fid=message.voice.file_id
    except:
        pass

if not incoming.strip() and not mtype:
    bot.replyText(u,f'<tg-emoji emoji-id="{EMOJI_WARN}">⚠️</tg-emoji> <b>Send text, photo, video, GIF or file.</b>',parse_mode="HTML")
    Bot.handleNextCommand("/EditBtnTxtSave")
    raise ReturnCommand

low=incoming.strip().lower()

if not mtype and (incoming.strip()=="⛔ Cancel" or low=="/cancel"):
    bot.replyText(u,f'<tg-emoji emoji-id="{EMOJI_FAILED}">⛔</tg-emoji> <b>Cancelled</b>',parse_mode="HTML",reply_markup={"remove_keyboard":True})
    Bot.runCommand("/EditButtonTextPanel")
    raise ReturnCommand

if not mtype and low=="clear":
    Bot.saveData(key,"")
    Bot.saveData(key+"_AT","")
    Bot.saveData(key+"_MEDIA","")
    bot.editMessageText(chat_id=u,message_id=message.message_id,text=f'<tg-emoji emoji-id="{EMOJI_SUCCESS}">✅</tg-emoji> <b>{name} text cleared</b>',parse_mode="HTML",reply_markup={"remove_keyboard":True})
    raise ReturnCommand

limit=1024 if mtype else 4000
if len(incoming)>limit:
    bot.replyText(u,f'<tg-emoji emoji-id="{EMOJI_WARN}">⚠️</tg-emoji> <b>Too long</b> ({len(incoming)} chars). Max is {limit}. Send a shorter text.',parse_mode="HTML")
    Bot.handleNextCommand("/EditBtnTxtSave")
    raise ReturnCommand

def esc(s):
    return s.replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")

def tags_for(e):
    t=str(e.type)
    if t=="bold": return ("<b>","</b>")
    if t=="italic": return ("<i>","</i>")
    if t=="underline": return ("<u>","</u>")
    if t=="strikethrough": return ("<s>","</s>")
    if t=="spoiler": return ("<tg-spoiler>","</tg-spoiler>")
    if t=="code": return ("<code>","</code>")
    if t=="blockquote": return ("<blockquote>","</blockquote>")
    if t=="expandable_blockquote": return ("<blockquote expandable>","</blockquote>")
    if t=="pre":
        lang=None
        try:
            lang=e.language
        except:
            pass
        if lang:
            return ('<pre><code class="language-'+esc(str(lang))+'">',"</code></pre>")
        return ("<pre>","</pre>")
    if t=="text_link":
        url=None
        try:
            url=e.url
        except:
            pass
        if url:
            return ('<a href="'+str(url).replace('"',"&quot;")+'">',"</a>")
        return None
    if t=="text_mention":
        uid=None
        try:
            uid=e.user.id
        except:
            pass
        if uid:
            return ('<a href="tg://user?id='+str(uid)+'">',"</a>")
        return None
    if t=="custom_emoji":
        emoji_id=None
        try:
            emoji_id=e.custom_emoji_id
        except:
            pass
        if emoji_id:
            return ('<tg-emoji emoji-id="'+str(emoji_id)+'">',"</tg-emoji>")
        return None
    return None

ORDER={"blockquote":1,"expandable_blockquote":1,"pre":2,"code":3,"text_link":4,"text_mention":4,"spoiler":5,"bold":6,"italic":7,"underline":8,"strikethrough":9,"custom_emoji":20}

def order_of(e):
    return ORDER.get(str(e.type),50)

def entities_to_html(text,ents):
    chars=list(text)
    pos=[0]
    p=0
    for ch in chars:
        p+=2 if ord(ch)>65535 else 1
        pos.append(p)
    u2c={}
    for i in range(len(pos)):
        u2c[pos[i]]=i
    spans=[]
    for e in ents:
        s=u2c.get(e.offset)
        en=u2c.get(e.offset+e.length)
        if s is None or en is None or en<=s or tags_for(e) is None:
            continue
        spans.append((s,en,e))
    cuts=set([0,len(chars)])
    for s,en,e in spans:
        cuts.add(s)
        cuts.add(en)
    cuts=sorted(cuts)
    out=""
    for i in range(len(cuts)-1):
        a=cuts[i]
        b=cuts[i+1]
        seg=esc("".join(chars[a:b]))
        active=[e for s,en,e in spans if s<=a and en>=b]
        active.sort(key=order_of)
        opens=""
        closes=""
        for e in active:
            pair=tags_for(e)
            if pair:
                o,c=pair
                opens+=o
                closes=c+closes
        out+=opens+seg+closes
    return out

HTML_HINTS=["<b>","<i>","<u>","<s>","<a ","<code>","<pre>","<tg-emoji","<tg-spoiler","<blockquote","<strong>","<em>"]

def looks_like_html(s):
    l=s.lower()
    for h in HTML_HINTS:
        if h in l:
            return True
    return False

if not incoming.strip():
    value=""
elif ents:
    value=entities_to_html(incoming,ents)
elif looks_like_html(incoming):
    value=incoming
else:
    value=esc(incoming)

if value.strip():
    tm=None
    try:
        tm=bot.replyText(u,value,parse_mode="HTML")
    except:
        bot.replyText(u,f'<tg-emoji emoji-id="{EMOJI_FAILED}">❌</tg-emoji> <b>Invalid formatting.</b> Telegram rejected this text. Send it again.',parse_mode="HTML")
        Bot.handleNextCommand("/EditBtnTxtSave")
        raise ReturnCommand
    try:
        bot.deleteMessage(chat_id=u,message_id=tm.message_id)
    except:
        pass

now=libs.DateAndTime.now("Asia/Kolkata")
stamp=now["date"]+" "+now["time"][:5]

Bot.saveData(key,value if value is not None else "")
Bot.saveData(key+"_AT",stamp)

if mtype:
    Bot.saveData(key+"_MEDIA",{"type":mtype,"file_id":fid,"caption":value if value else ""})
else:
    Bot.saveData(key+"_MEDIA","")

bot.replyText(u,f'<tg-emoji emoji-id="{EMOJI_SUCCESS}">✅</tg-emoji> <b>{name} text saved</b>',parse_mode="HTML",reply_markup={"remove_keyboard":True})''',
    '/EditBtnTxtStart': r'''# ============================================================
# /EditBtnTxtStart  -  begin editing one game (yono / lucky / jaiho)
# ============================================================

EMOJI_FAILED = "6129840374971112593"
EMOJI_STAR = "5469741319330996757"
EMOJI_SUCCESS = "6203820727083208720"
EMOJI_WARN = "5447644880824181073"

admin_ids = [str(x) for x in (Bot.getData("AllBotAdminss") or [])]
admin_ids.append(str(Bot.getData("Owner")))

if str(u) not in admin_ids:
    bot.replyText(u, f'<tg-emoji emoji-id="{EMOJI_FAILED}">🚫</tg-emoji> <b>Access Denied</b>', parse_mode="HTML")
    raise ReturnCommand

CFG = {
    "yono": ("Yono 777", "YONO777_TEXT", "5375296873982604963", "🎁"),
    "lucky": ("Lucky Gullak", "LUCKYGULLAK_TEXT", "5021905410089550576", "💰"),
    "jaiho": ("Jaiho 777", "JAIHO777_TEXT", "6305222293601653735", "🎉"),
}

target = str(params).strip().lower()

if target not in CFG:
    bot.replyText(u, f'<tg-emoji emoji-id="{EMOJI_WARN}">⚠️</tg-emoji> <b>Unknown section.</b>', parse_mode="HTML")
    Bot.runCommand("/EditButtonTextPanel")
    raise ReturnCommand

name, key, eid, fb = CFG[target]

User.saveData("EBT_TARGET", target)

keyboard = ReplyKeyboardMarkup(True)
keyboard.row("⛔ Cancel")

bot.replyText(
    u,
    f'<tg-emoji emoji-id="{eid}">{fb}</tg-emoji> <b>Edit {name}</b>\n'
    f'━━━━━━━━━━━━━━━━━━\n\n'
    f'<tg-emoji emoji-id="{EMOJI_STAR}">✏️</tg-emoji> <b>Send the new content now</b>\n\n'
    f'• Text, or Photo / Video / GIF / File with caption\n'
    f'• Bold, italic, links, premium emoji — all saved automatically\n'
    f'• Send <code>Clear</code> to remove everything\n'
    f'• Tap <b>⛔ Cancel</b> to go back',
    parse_mode="HTML",
    reply_markup=keyboard
)

Bot.handleNextCommand("/EditBtnTxtSave")''',
    '/EditButtonTextPanel': r'''# ============================================================
# /EditButtonTextPanel
#   no params      -> main panel (3 game buttons only)
#   params = game  -> that game's full control page
#                     (status, Preview, Edit, Clear, Back)
# ============================================================
EMOJI_MANAGE = "5334673106202010226"
EMOJI_FAILED="6129840374971112593"
EMOJI_GUN="6305222293601653735"
EMOJI_MONEY="5375296873982604963"
EMOJI_FUND="5021905410089550576"
EMOJI_STA = "5372981976804366741"
EMOJI_STAR="5469741319330996757"
EMOJI_SUCCESS="6203820727083208720"
EMOJI_GIFT="5203996991054432397"
EMOJI_WARN="5447644880824181073"

admin_ids=[str(x) for x in (Bot.getData("AllBotAdminss") or [])]
owner=Bot.getData("Owner")
if owner:
    admin_ids.append(str(owner))

if str(u) not in admin_ids:
    bot.replyText(
        u,
        f'<tg-emoji emoji-id="{EMOJI_FAILED}">🚫</tg-emoji> <b>Access Denied</b>\n\n'
        f'Only bot admins can use this panel.',
        parse_mode="HTML"
    )
    raise ReturnCommand

ITEMS={
    "yono":("Yono 777","YONO777_TEXT",EMOJI_MONEY,""),
    "lucky":("Lucky Gullak","LUCKYGULLAK_TEXT",EMOJI_FUND,""),
    "jaiho":("Jaiho 777","JAIHO777_TEXT",EMOJI_GUN,"")
}
ORDER=["yono","lucky","jaiho"]

MEDIA_LABEL={
    "photo":"Photo",
    "video":"Video",
    "animation":"GIF",
    "document":"File",
    "audio":"Audio",
    "voice":"Voice"
}

banner=""
try:
    if options:
        banner=str(options)+"\n\n"
except:
    banner=""

target=""
try:
    target=str(params).strip().lower()
except:
    target=""

if target in ITEMS:
    name,key,eid,fb=ITEMS[target]

    val=Bot.getData(key) or ""
    media=Bot.getData(key+"_MEDIA")
    at=Bot.getData(key+"_AT") or ""

    mtype=""
    try:
        if isinstance(media,dict):
            mtype=media.get("type") or ""
    except:
        mtype=""

    has_text=bool(str(val).strip())
    has_media=bool(mtype)

    if has_text or has_media:
        status=f'<tg-emoji emoji-id="{EMOJI_SUCCESS}">✅</tg-emoji> <b>Active</b>'

        if has_text and has_media:
            content="Text + "+MEDIA_LABEL.get(mtype,"Media")
        elif has_media:
            content=MEDIA_LABEL.get(mtype,"Media")+" Only"
        else:
            content="Text Only"

        info=(
            f'<tg-emoji emoji-id="{EMOJI_STAR}">📄</tg-emoji> '
            f'<b>Content:</b> {content}\n'
        )

        if has_text:
            info+=(
                f'<tg-emoji emoji-id="{EMOJI_SUCCESS}">📝</tg-emoji> '
                f'<b>Text:</b> Added\n'
            )

        if has_media:
            info+=(
                f'<tg-emoji emoji-id="{EMOJI_GIFT}">📎</tg-emoji> '
                f'<b>Media:</b> {MEDIA_LABEL.get(mtype,"Media")}\n'
            )

        if at:
            info+=(
                f'<tg-emoji emoji-id="{EMOJI_STA}">🕒</tg-emoji> '
                f'<b>Updated:</b> {at}\n'
            )
    else:
        status=f'<tg-emoji emoji-id="{EMOJI_FAILED}">❌</tg-emoji> <b>Not Configured</b>'
        info=(
            f'<tg-emoji emoji-id="{EMOJI_WARN}">⚠️</tg-emoji> '
            f'<b>No content has been added yet.</b>\n'
        )

    msg=(
        banner+
        f'<tg-emoji emoji-id="{eid}">{fb}</tg-emoji> '
        f'<b>{name} Button Manager</b>\n'
        f'━━━━━━━━━━━━━━━━━━\n\n'
        f'<b>Status</b>\n'
        f'{status}\n\n'
        f'<b>Details</b>\n'
        f'{info}\n'
        f'━━━━━━━━━━━━━━━━━━\n'
        f'<tg-emoji emoji-id="{EMOJI_MANAGE}">🛠</tg-emoji> '
        f'<b>Manage Content</b>\n'
        f'<i>Choose an action below.</i>'
    )

    markup={
        "inline_keyboard":[
            [
                {
                    "text":"Preview",
                    "callback_data":"/EditBtnTxtPreview "+target,
                    "icon_custom_emoji_id":EMOJI_STAR
                },
                {
                    "text":"Edit",
                    "callback_data":"/EditBtnTxtStart "+target,
                    "icon_custom_emoji_id":eid
                }
            ],
            [
                {
                    "text":"Clear",
                    "callback_data":"/EditBtnTxtClear "+target,
                    "icon_custom_emoji_id":EMOJI_FAILED
                }
            ],
            [
                {
                    "text":"Back",
                    "callback_data":"/EditButtonTextPanel",
                    "icon_custom_emoji_id":5357165441909279397
                }
            ]
        ]
    }

else:
    body=""
    rows=[]
    configured=0

    for k in ORDER:
        name,key,eid,fb=ITEMS[k]

        val=Bot.getData(key) or ""
        media=Bot.getData(key+"_MEDIA")

        mtype=""
        try:
            if isinstance(media,dict):
                mtype=media.get("type") or ""
        except:
            mtype=""

        has_text=bool(str(val).strip())
        has_media=bool(mtype)

        if has_text or has_media:
            configured+=1

            if has_text and has_media:
                content="Text + "+MEDIA_LABEL.get(mtype,"Media")
            elif has_media:
                content=MEDIA_LABEL.get(mtype,"Media")
            else:
                content="Text"

            body+=(
                f'<tg-emoji emoji-id="{EMOJI_SUCCESS}">✅</tg-emoji> '
                f'<b>{name}</b> — <i>{content}</i>\n'
            )
        else:
            body+=(
                f'<tg-emoji emoji-id="{EMOJI_FAILED}">❌</tg-emoji> '
                f'<b>{name}</b> — <i>Not Configured</i>\n'
            )

        rows.append([
            {
                "text":fb+" "+name,
                "callback_data":"/EditButtonTextPanel "+k,
                "icon_custom_emoji_id":eid
            }
        ])

    total=len(ORDER)

    if configured==total:
        overall=f'<tg-emoji emoji-id="{EMOJI_SUCCESS}">🟢</tg-emoji> <b>All Configured</b>'
    elif configured>0:
        overall=f'<tg-emoji emoji-id="{EMOJI_WARN}">🟡</tg-emoji> <b>Partially Configured</b>'
    else:
        overall=f'<tg-emoji emoji-id="{EMOJI_FAILED}">🔴</tg-emoji> <b>Nothing Configured</b>'

    msg=(
        banner+
        f'<tg-emoji emoji-id="{EMOJI_MANAGE}">⚙️</tg-emoji> '
        f'<b>Button Text Manager</b>\n'
        f'<i>Game button content control center</i>\n'
        f'━━━━━━━━━━━━━━━━━━\n\n'
        f'<b>Dashboard</b>\n'
        f'<tg-emoji emoji-id="{EMOJI_GIFT}">🎮</tg-emoji> '
        f'<b>Total Games:</b> {total}\n'
        f'<tg-emoji emoji-id="{EMOJI_SUCCESS}">📊</tg-emoji> '
        f'<b>Configured:</b> {configured}/{total}\n'
        f'<tg-emoji emoji-id="{EMOJI_STAR}">📌</tg-emoji> '
        f'<b>Status:</b> {overall}\n\n'
        f'━━━━━━━━━━━━━━━━━━\n'
        f'<b>Game Details</b>\n\n'
        f'{body}\n'
        f'━━━━━━━━━━━━━━━━━━\n'
        f'<i>Select a game below to manage its content.</i>'
    )

    markup={"inline_keyboard":rows}

is_cb=False

try:
    if call is not None:
        is_cb=True
except:
    is_cb=False

sent=False

if is_cb:
    try:
        bot.editMessageText(
            chat_id=u,
            message_id=call.message.message_id,
            text=msg,
            parse_mode="HTML",
            reply_markup=markup
        )
        sent=True
    except:
        try:
            bot.deleteMessage(
                chat_id=u,
                message_id=call.message.message_id
            )
        except:
            pass
        sent=False

if not sent:
    bot.replyText(
        u,
        msg,
        parse_mode="HTML",
        reply_markup=markup
    )''',
    '/JaiHo777': r'''# ================= /JaiHo777 =================

EMOJI_WARN = "5447644880824181073"
EMOJI_EDIT = "5469741319330996757"
KEY = "JAIHO777_TEXT"
TAG = "jaiho"

admin_ids = [str(x) for x in (Bot.getData("AllBotAdminss") or [])]
admin_ids.append(str(Bot.getData("Owner")))
is_admin = str(u) in admin_ids

text = Bot.getData(KEY) or ""
media = Bot.getData(KEY + "_MEDIA")


def send_media(mt, chat, fid, cap, pm, mk):
    kw = {"chat_id": str(chat)}
    if cap:
        kw["caption"] = cap
    if pm:
        kw["parse_mode"] = pm
    if mk:
        kw["reply_markup"] = mk
    if mt == "photo":
        kw["photo"] = fid
        return bot.sendPhoto(**kw)
    if mt == "video":
        kw["video"] = fid
        return bot.sendVideo(**kw)
    if mt == "animation":
        kw["animation"] = fid
        return bot.sendAnimation(**kw)
    if mt == "document":
        kw["document"] = fid
        return bot.sendDocument(**kw)
    if mt == "audio":
        kw["audio"] = fid
        return bot.sendAudio(**kw)
    if mt == "voice":
        kw["voice"] = fid
        return bot.sendVoice(**kw)
    raise Exception("unknown media type")


def send_content(chat, text, media, mk):
    mt = None
    fid = None
    if isinstance(media, dict):
        mt = media.get("type")
        fid = media.get("file_id")
    for attempt in range(3):
        pm = "HTML"
        m2 = mk
        if attempt == 1:
            m2 = None
        if attempt == 2:
            pm = None
            m2 = None
        try:
            if mt and fid:
                send_media(mt, chat, fid, text, pm, m2)
            elif pm:
                if m2:
                    bot.replyText(chat, text, parse_mode=pm, reply_markup=m2)
                else:
                    bot.replyText(chat, text, parse_mode=pm)
            else:
                bot.replyText(chat, text)
            return True
        except Exception:
            pass
    return False


admin_markup = {
    "inline_keyboard": [
        [
            {"text": "Edit This Text", "callback_data": "/EditBtnTxtStart " + TAG, "icon_custom_emoji_id": EMOJI_EDIT}
        ]
    ]
}

has_media = isinstance(media, dict) and media.get("file_id")

if not text.strip() and not has_media:
    if is_admin:
        send_content(u, f'<tg-emoji emoji-id="{EMOJI_WARN}">⚠️</tg-emoji> <b>No text added yet.</b>\nTap below to add it.', None, admin_markup)
    else:
        send_content(u, f'<tg-emoji emoji-id="{EMOJI_WARN}">⚠️</tg-emoji> <b>This section is being updated. Please check back soon.</b>', None, None)
    raise ReturnCommand

if is_admin:
    send_content(u, text, media, admin_markup)
else:
    send_content(u, text, media, None)''',
    '/JoinPiro': r'''AMC = Bot.getData("AllMainCh") or []
AMCL=Bot.getData("AllMainChlink") or []
AMCTGID = Bot.getData("AllMainChTGID") or []

PIRO_NJCL=User.getData("PIRO_NJCL") or []

if not AMCL:
    bot.sendMessage("📌 No channels found!")
else:
    JoinBtnText = Bot.getData("PIRO_JoinBtnText") or "Join"
    ClaimBtnText = Bot.getData("PIRO_ClaimBtnText") or "🔒 Claim"
    JoinIcon = Bot.getData("PIRO_JoinBtnEmojiId")
    ClaimIcon = Bot.getData("PIRO_ClaimBtnEmojiId")

    keyboard = []
    row = []

    IND=0
    for LINK in AMCL:
        if IND < len(AMCTGID) and str(AMCTGID[IND]) in PIRO_NJCL:
            btn = {"text": JoinBtnText, "url": LINK, "style": "primary"}
            if JoinIcon:
                btn["icon_custom_emoji_id"] = JoinIcon
            row.append(btn)

            if len(row) == 2:
                keyboard.append(row)
                row = []
        IND=IND+1

    if row:
        keyboard.append(row)

    cbtn = {"text": ClaimBtnText, "callback_data": "🟢 Joined","style":"success"}
    if ClaimIcon:
        cbtn["icon_custom_emoji_id"] = ClaimIcon
    keyboard.append([cbtn])

    usr = f"""<a href="tg://user?id={str(u)}">{message.from_user.first_name}</a>"""

    JoinMsgChatId = Bot.getData("PIRO_JoinMsgChatId")
    JoinMsgId = Bot.getData("PIRO_JoinMsgId")

    SentOk = False
    if JoinMsgChatId and JoinMsgId:
        try:
            bot.copyMessage(chat_id=u, from_chat_id=JoinMsgChatId, message_id=JoinMsgId, reply_markup={"inline_keyboard": keyboard})
            SentOk = True
        except:
            SentOk = False

    if not SentOk:
        bot.sendMessage(f"""<b>👋 Hey There {usr} Welcome To Wallet Bot !

🛑 Must Join Total Channel To Use Our Bot

💣 After Joining Click Claim</b>""", reply_markup={"inline_keyboard": keyboard})''',
    '/JoinPiro1': r'''AMC = Bot.getData("AllMainCh") or []
AMCL=Bot.getData("AllMainChlink") or []

text ="<b>🚫You Didn't Join These Channels 👇\n</b>"

if not AMCL:
    bot.sendMessage("📌 No channels Links found!")
else:
    JoinBtnText = Bot.getData("PIRO_JoinBtnText") or "Join"
    ClaimBtnText = Bot.getData("PIRO_ClaimBtnText") or "🔒 Claim"
    JoinIcon = Bot.getData("PIRO_JoinBtnEmojiId")
    ClaimIcon = Bot.getData("PIRO_ClaimBtnEmojiId")

    keyboard = []
    row = []

    for LINK in AMCL:
        btn = {"text": JoinBtnText, "url": LINK, "style": "primary"}
        if JoinIcon:
            btn["icon_custom_emoji_id"] = JoinIcon
        row.append(btn)

        if len(row) == 2:
            keyboard.append(row)
            row = []

    if row:
        keyboard.append(row)

    cbtn = {"text": ClaimBtnText, "callback_data": "🟢 Joined","style":"success"}
    if ClaimIcon:
        cbtn["icon_custom_emoji_id"] = ClaimIcon
    keyboard.append([cbtn])

    usr = f"""<a href="tg://user?id={str(u)}">{message.from_user.first_name}</a>"""

    JoinMsgChatId = Bot.getData("PIRO_JoinMsgChatId")
    JoinMsgId = Bot.getData("PIRO_JoinMsgId")

    SentOk = False
    if JoinMsgChatId and JoinMsgId:
        try:
            bot.copyMessage(chat_id=u, from_chat_id=JoinMsgChatId, message_id=JoinMsgId, reply_markup={"inline_keyboard": keyboard})
            SentOk = True
        except:
            SentOk = False

    if not SentOk:
        bot.sendMessage(f"""<b>👋 Hey There {usr} Welcome To Wallet Bot !

🛑 Must Join Total Channel To Use Our Bot

💣 After Joining Click Claim</b>""", reply_markup={"inline_keyboard": keyboard})

    if options:
        NJLST=User.getData("NJLST") or ""
        text+=NJLST
        bot.replyText(chat_id=u,text=text,parse_mode="html",link_preview_options=LinkPreviewOptions(is_disabled=True))''',
    '/JoinReqSt': r'''# 19'4'25   00'11'10  v:1'0'0 [√]
# CODED BY: @Owner_Here 
# [ DM TO BUY ANY BOTS & CODES ]


P=params.split(" ")

DC=Bot.getData(str(P[0])+"isDef") or "N"

if DC=="Y":
    Bot.saveData(str(P[0])+"JoinReq",P[1]) 
    if str(P[1])=="Y":
        bot.replyText(u,"🟢Added To Check")
    if str(P[1])=="N":
        bot.replyText(u,"🔴Removed From Check")
else:
    bot.replyText(u,"🤯This is Not Default Channel")
    
  
   
   ''',
    '/LuckyGullak': r'''# ================= /LuckyGullak =================

EMOJI_WARN = "5447644880824181073"
EMOJI_EDIT = "5469741319330996757"
KEY = "LUCKYGULLAK_TEXT"
TAG = "lucky"

admin_ids = [str(x) for x in (Bot.getData("AllBotAdminss") or [])]
admin_ids.append(str(Bot.getData("Owner")))
is_admin = str(u) in admin_ids

text = Bot.getData(KEY) or ""
media = Bot.getData(KEY + "_MEDIA")


def send_media(mt, chat, fid, cap, pm, mk):
    kw = {"chat_id": str(chat)}
    if cap:
        kw["caption"] = cap
    if pm:
        kw["parse_mode"] = pm
    if mk:
        kw["reply_markup"] = mk
    if mt == "photo":
        kw["photo"] = fid
        return bot.sendPhoto(**kw)
    if mt == "video":
        kw["video"] = fid
        return bot.sendVideo(**kw)
    if mt == "animation":
        kw["animation"] = fid
        return bot.sendAnimation(**kw)
    if mt == "document":
        kw["document"] = fid
        return bot.sendDocument(**kw)
    if mt == "audio":
        kw["audio"] = fid
        return bot.sendAudio(**kw)
    if mt == "voice":
        kw["voice"] = fid
        return bot.sendVoice(**kw)
    raise Exception("unknown media type")


def send_content(chat, text, media, mk):
    mt = None
    fid = None
    if isinstance(media, dict):
        mt = media.get("type")
        fid = media.get("file_id")
    for attempt in range(3):
        pm = "HTML"
        m2 = mk
        if attempt == 1:
            m2 = None
        if attempt == 2:
            pm = None
            m2 = None
        try:
            if mt and fid:
                send_media(mt, chat, fid, text, pm, m2)
            elif pm:
                if m2:
                    bot.replyText(chat, text, parse_mode=pm, reply_markup=m2)
                else:
                    bot.replyText(chat, text, parse_mode=pm)
            else:
                bot.replyText(chat, text)
            return True
        except Exception:
            pass
    return False


admin_markup = {
    "inline_keyboard": [
        [
            {"text": "Edit This Text", "callback_data": "/EditBtnTxtStart " + TAG, "icon_custom_emoji_id": EMOJI_EDIT}
        ]
    ]
}

has_media = isinstance(media, dict) and media.get("file_id")

if not text.strip() and not has_media:
    if is_admin:
        send_content(u, f'<tg-emoji emoji-id="{EMOJI_WARN}">⚠️</tg-emoji> <b>No text added yet.</b>\nTap below to add it.', None, admin_markup)
    else:
        send_content(u, f'<tg-emoji emoji-id="{EMOJI_WARN}">⚠️</tg-emoji> <b>This section is being updated. Please check back soon.</b>', None, None)
    raise ReturnCommand

if is_admin:
    send_content(u, text, media, admin_markup)
else:
    send_content(u, text, media, None)''',
    '/ManageText': r'''# 19'4'25   00'11'10  v:1'0'0 [√]
# CODED BY: @Owner_Here 
# [ DM TO BUY ANY BOTS & CODES ]

AllBotAdminss = Bot.getData("AllBotAdminss") or []

is_Admin = False

for userid in AllBotAdminss:
    if str(u) == str(userid):
        is_Admin = True

if is_Admin != True:
    bot.replyText(
        u,
        "<b><i>🚫You Are Not This Bot Admin</i></b>"
    )
    raise ReturnCommand()


markup = InlineKeyboardMarkup()

# Row 1
markup.row(InlineKeyboardButton(text='Aᴅᴅ Bᴀʟᴀɴᴄᴇ Tᴇxᴛ',callback_data='/AddBalTxt'))
markup.row(InlineKeyboardButton(
            text="Cʟɪᴄᴋ Hᴇʀᴇ",
            callback_data="/onogf"
        )
    )

# Row 2
markup.row(InlineKeyboardButton(
            text="Lᴇᴀᴅᴇʀʙᴏᴀʀᴅ",
            callback_data="/PIRO_LeadSet"
        ))
markup.row(InlineKeyboardButton(
            text="Wɪᴛʜᴅʀᴀᴡ Oғғ Tᴇxᴛ",
            callback_data="/textoff"
        )
    )
# Row 3
markup.row(
    InlineKeyboardButton(
        text="⬅️ Bᴀᴄᴋ",
        callback_data="/admin AP"
    )
)


bot.editMessageText(
    chat_id=u,
    message_id=call.message.message_id,
    text=f"""<b><i>📝 Mᴇssᴀɢᴇ Mᴀɴᴀɢᴇᴍᴇɴᴛ</i>
    
Hᴇʀᴇ, ʏᴏᴜ ᴄᴀɴ ᴍᴀɴᴀɢᴇ ᴀɴᴅ ᴇᴅɪᴛ ʏᴏᴜʀ ʙᴏᴛ ᴍᴇssᴀɢᴇs ᴀs ʏᴏᴜ ᴡɪsʜ. ✨</b>""",
    parse_mode="HTML",
    reply_markup=markup
)''',
    '/PIRO_AddAdmin': r'''# 19'4'25   00'11'10  v:1'0'0 [√]
# CODED BY: @Owner_Here 
# [ DM TO BUY ANY BOTS & CODES ]

AllBotAdminss = Bot.getData("AllBotAdminss") or []

is_Admin = False
for userid in AllBotAdminss:
    if str(u) == str(userid):
        is_Admin = True

if is_Admin != True:
    bot.replyText(u, "<b><i>🚫You Are Not This Bot Admin</i></b>")
    raise ReturnCommand()


bot.replyText(chat_id=message.chat.id,text=f"""<b>Send UserID of Admin You Want To Add</b>""",parse_mode="html")

User.saveData("EDMsgID",message.message_id) 

Bot.handleNextCommand("/PIRO_AddAdmin1")''',
    '/PIRO_AddAdmin1': r'''# 19'4'25   00'11'10  v:1'0'0 [√]
# CODED BY: @Owner_Here 
# [ DM TO BUY ANY BOTS & CODES ]


AllBotAdminss = Bot.getData("AllBotAdminss") or []

is_Admin = False
for userid in AllBotAdminss:
    if str(u) == str(userid):
        is_Admin = True

if is_Admin != True:
    bot.replyText(u, "<b><i>🚫You Are Not This Bot Admin</i></b>")
    raise ReturnCommand()


check_user = AllBotAdminss.count(message.text)
if check_user > 0:
    """user_exist"""
    T="Admin Already Exists"
else:
    AllBotAdminss.append(message.text)
    Bot.saveData("AllBotAdminss", AllBotAdminss)
    T="Admin Added Successfully"


Bot.runCommand("/PIRO_Admins",options=f"<b>{T}</b>")











now = libs.DateAndTime.now("Asia/Kolkata")
date = now["date"]  
time = now["time"][:5] 
year, month, day = date.split("-")
hour, minute = time.split(":")
hour = int(hour)
ampm = "am"
if hour >= 12:
    ampm = "pm"
if hour > 12:
    hour -= 12
if hour == 0:
    hour = 12

MONTHS = {"01": "Jan", "02": "Feb", "03": "Mar", "04": "Apr","05": "May", "06": "Jun", "07": "Jul", "08": "Aug","09": "Sep", "10": "Oct", "11": "Nov", "12": "Dec"}

EasyTime = f"{int(day)} {MONTHS[month]}, {hour:02}:{minute} {ampm}"


AdmAC=Bot.getData("AdmAC") or []

act=f"Added {message.text} as Bot Admin"
AdmAC.append(f"<b>📆 Time:</b> {EasyTime}\n👥 <b>By {message.from_user.first_name}</b> [ID: <code>{u}</code>]\n🔍<b> Action: </b> {act}")

Bot.saveData("AdmAC",AdmAC)









Bot.sendMessage(f"<b>{T}</b>")''',
    '/PIRO_AddCh': r'''AllBotAdminss = Bot.getData("AllBotAdminss") or []

is_Admin = False
for userid in AllBotAdminss:
    if str(u) == str(userid):
        is_Admin = True

if is_Admin != True:
    bot.replyText(u, "<b><i>🚫You Are Not This Bot Admin</i></b>")
    raise ReturnCommand()


TargetPage = ""
if str(params) != "None":
    PP = str(params).strip()
    if PP.isdigit():
        TargetPage = PP

if not TargetPage:
    TargetPage = str(User.getData("PIRO_AdmViewPage") or 1)


bot.replyText(u,f"""<b>💡 Send Username/Chat ID Of Channel (Username with @) ,Or Forward A message from channel</b>""",parse_mode = "html")

Bot.handleNextCommand("/PIRO_AddCh1", options=TargetPage)''',
    '/PIRO_AddCh1': r'''AllBotAdminss = Bot.getData("AllBotAdminss") or []
is_Admin = str(u) in map(str, AllBotAdminss)

if not is_Admin:
    bot.replyText(u, "<b><i>🚫 You Are Not This Bot Admin</i></b>")
    raise ReturnCommand()

AllMainCh = Bot.getData("AllMainCh") or []
AMCL = Bot.getData("AllMainChlink") or []
AMCTGID = Bot.getData("AllMainChTGID") or []
AMCU = Bot.getData("AllMainChUsernm") or []
AMCP = Bot.getData("AllMainChPage") or []
for _pad in range(len(AMCTGID) - len(AMCP)):
    AMCP.append(1)

TargetPage = User.getData("PIRO_AdminCurPage")
TargetPage = int(TargetPage) if TargetPage else 1

# Detect forwarded channel or direct text input
CHL = []
if message.forward_from_chat:
    fwd_chat = message.forward_from_chat
    TGID = str(fwd_chat.id)
    TITLE = fwd_chat.title
    USRNAM = fwd_chat.username or "None"
    USRNAM_DISPLAY = "@" + USRNAM if USRNAM != "None" else str(TGID)
    CLink = f"https://t.me/{USRNAM}" if USRNAM != "None" else f"https://t.me/c/{str(TGID).replace('-100', '')}"

    try:
        api_url = f"https://api.telegram.org/bot{Bot.info().token}/createChatInviteLink?chat_id={TGID}"
        response = HTTP.get(api_url).json()
        CLink = bunchify(response)["result"]["invite_link"]
    except:
        pass

    CHL = [TGID]
else:
    CH = message.text
    CHL = CH.split("\n") if "\n" in CH else [CH]

NewlyAddedTGIDs = []

# Loop through channels (forwarded or text)
for i in CHL:
    Txt = ""
    BIA = True
    CLink = "None"
    TGID = str(i)
    TITLE = str(i)
    USRNAM = "None"

    if "@" not in i and "-" not in i and not i.isdigit():
        bot.sendMessage(f"{i} is Not Any Channel Username Or Telegram ID")
        continue

    try:
        a = f"https://api.telegram.org/bot{Bot.info().token}/getChat?chat_id={i}"
        b = HTTP.get(a).json()
        data = bunchify(b)['result']
        CLink = data.get('invite_link', "None")
        TGID = data['id']
        TITLE = data['title']
        USRNAM = data.get('username', "None")
    except Exception as e:
        BIA = False

    try:
        UC = bot.getChatMember(i, u)
        UC = UC.status
    except:
        BIA = False
        UC = "error"

    USRNAM_DISPLAY = "@" + USRNAM if USRNAM != "None" else str(TGID)
    lnkAdd = False
    if CLink == "None":
        BIA = False
        CLink = f"https://t.me/{USRNAM_DISPLAY}" if USRNAM != "None" else f"https://t.me/c/{str(TGID).replace('-100', '')}"
        lnkAdd = True

    if BIA:
        Txt += f"<b>🎉 Bot Is Admin In {i} 🎉</b>"
    else:
        Txt += f"<b>🚨 Bot Not Admin In {i} 🚨 </b>"

    Txt += "\n\n<b>🔗 Invite Link "
    Txt += "Successfully Added Automatically! 😊</b>" if not lnkAdd else "Not Added. Please Add Manually If Needed! 😅</b>"

    Txt += f"\n\n<b>📄 Added To Page {TargetPage}</b>"

    if str(TGID) not in map(str, AMCTGID):
        AllMainCh.append(TITLE)
        Bot.saveData("AllMainCh", AllMainCh)
        AMCL.append(CLink)
        Bot.saveData("AllMainChlink", AMCL)
        AMCTGID.append(TGID)
        Bot.saveData("AllMainChTGID", AMCTGID)
        AMCU.append(str(USRNAM_DISPLAY))
        Bot.saveData("AllMainChUsernm", AMCU)
        AMCP.append(TargetPage)
        Bot.saveData("AllMainChPage", AMCP)
        NewlyAddedTGIDs.append(str(TGID))

        Txt += "\n\n<b>✅ Added Successfully</b>"
        bot.replyText(u, Txt, link_preview_options=LinkPreviewOptions(is_disabled=True)) if BIA else Bot.sendMessage(Txt)
    else:
        bot.sendMessage("⚠️ Channel Already Available")

if NewlyAddedTGIDs:
    Bot.runCommand("/PIRO_Chanel_Pannel")''',
    '/PIRO_AddCh2': r'''AllBotAdminss = Bot.getData("AllBotAdminss") or []
is_Admin = str(u) in map(str, AllBotAdminss)

if not is_Admin:
    bot.replyText(u, "<b><i>🚫 You Are Not This Bot Admin</i></b>")
    raise ReturnCommand()

if str(message.text) == "/cancel":
    bot.sendMessage("<b>Kept on Page 1 (default).</b>")
    raise ReturnCommand()

PageTxt = str(message.text).strip()
if not PageTxt.isdigit() or int(PageTxt) < 1:
    bot.sendMessage("<b>⚠️ Please send a valid page number (whole number, 1 or more). Try again:</b>")
    Bot.handleNextCommand("/PIRO_AddCh2", options=options)
    raise ReturnCommand()

PageNum = int(PageTxt)

AMCTGID = Bot.getData("AllMainChTGID") or []
AMCP = Bot.getData("AllMainChPage") or []
for _pad in range(len(AMCTGID) - len(AMCP)):
    AMCP.append(1)

TargetTGIDs = str(options).split(",") if options else []

Updated = 0
for tgid in TargetTGIDs:
    try:
        IND = AMCTGID.index(int(tgid))
    except:
        try:
            IND = AMCTGID.index(str(tgid))
        except:
            continue
    AMCP[IND] = PageNum
    Updated += 1

Bot.saveData("AllMainChPage", AMCP)

bot.sendMessage(f"<b>✅ {Updated} channel(s) set to Page {PageNum}</b>")

User.saveData("PIRO_AdmViewPage", PageNum)
Bot.runCommand("/PIRO_Chanel_Pannel")''',
    '/PIRO_AdminAction': r'''AdmAC=Bot.getData("AdmAC") or []


x=int(len(AdmAC)) 
latest_10 = AdmAC[-10:][::-1]

# Join and print them
bot.sendMessage("\n\n".join(latest_10))

#bot.sendMessage(AdmAC)
    
   ''',
    '/PIRO_Admins': r'''# 19'4'25   00'11'10  v:1'0'0 [√]
# CODED BY: @Owner_Here 
# [ DM TO BUY ANY BOTS & CODES ]

AllBotAdminss = Bot.getData("AllBotAdminss") or []

is_Admin = False
for userid in AllBotAdminss:
    if str(u) == str(userid):
        is_Admin = True

if is_Admin != True:
    bot.replyText(u, "<b><i>🚫You Are Not This Bot Admin</i></b>")
    raise ReturnCommand()





now = libs.DateAndTime.now("Asia/Kolkata")
date = now["date"]  
time = now["time"][:5] 
year, month, day = date.split("-")
hour, minute = time.split(":")
hour = int(hour)
ampm = "am"
if hour >= 12:
    ampm = "pm"
if hour > 12:
    hour -= 12
if hour == 0:
    hour = 12

MONTHS = {"01": "Jan", "02": "Feb", "03": "Mar", "04": "Apr","05": "May", "06": "Jun", "07": "Jul", "08": "Aug","09": "Sep", "10": "Oct", "11": "Nov", "12": "Dec"}

EasyTime = f"{int(day)} {MONTHS[month]}, {hour:02}:{minute} {ampm}"



AdmAC=Bot.getData("AdmAC") or []










if str(params) != "None":

    Owner = Bot.getData("Owner")

    # Owner cannot remove themself
    if str(params) == str(u) and str(u) == str(Owner):
        bot.answerCallbackQuery(
            call.id,
            "🚫 Owner cannot remove themself from Admins.",
            show_alert=True
        )
        raise ReturnCommand()

    # No admin can remove Owner
    if str(params) == str(Owner):
        bot.answerCallbackQuery(
            call.id,
            "🚫 Owner cannot be removed from Admins.",
            show_alert=True
        )
        raise ReturnCommand()

    # Remove selected admin
    if str(params) in [str(x) for x in AllBotAdminss]:
        AllBotAdminss.remove(params)

        act = f"Removed {params} from Bot Admin"

        AdmAC.append(
            f"<b>📆 Time:</b> {EasyTime}\n"
            f"👥 <b>By {message.from_user.first_name}</b> "
            f"[ID: <code>{u}</code>]\n"
            f"🔍<b> Action: </b> {act}"
        )

        Bot.saveData("AdmAC", AdmAC)


markup = InlineKeyboardMarkup()

for admin in AllBotAdminss:
    markup.add(InlineKeyboardButton(text=admin,callback_data="/PIRO_Admins "+str(admin) ),InlineKeyboardButton(text="❌",callback_data="/PIRO_Admins "+str(admin)))

markup.add(InlineKeyboardButton(text='➕Add Admin',callback_data='/PIRO_AddAdmin'))


markup.add(InlineKeyboardButton(text='🔙Back',callback_data='/admin AP'))

Bot.saveData("AllBotAdminss",AllBotAdminss)


e="y"
T="<b>Here You Can Manage Your Admins</b>"
if options:
    T=options

MD=User.getData("EDMsgID") 

try:
    bot.editMessageText(chat_id=u,message_id=message.message_id,text=T,reply_markup=markup,parse_mode="Html")
    if options:
        e="n"
except:
    pass
 
 
try:
    bot.editMessageText(chat_id=u,message_id=MD,text=T)
    if options:
        e="n"
except:
    pass



if e=="n":
    Bot.runCommand ("/admin")''',
    '/PIRO_BC_Cancel': r'''# Admin Check
AllBotAdminss = Bot.getData("AllBotAdminss") or []
is_Admin = False
for userid in AllBotAdminss:
    if str(u) == str(userid):
        is_Admin = True
if is_Admin != True:
    bot.replyText(u, "<b><i>🚫You Are Not This Bot Admin</i></b>")
    raise ReturnCommand()

msg_id = Bot.getData("BC_MsgID")
if msg_id:
    try:
        bot.editMessageText(
            chat_id=u,
            message_id=msg_id,
            text="<b>❌ Broadcast/Schedule Cancelled</b>"
        )
    except:
        pass

Bot.saveData("BC_Data", None)
Bot.saveData("BC_MsgID", None)
Bot.saveData("BC_Schedule_Date", None)
Bot.saveData("BC_Schedule_Time", None)
Bot.saveData("BC_Scheduled_Data", None)
Bot.saveData("BC_Scheduled_Date", None)
Bot.saveData("BC_Scheduled_Time", None)
Bot.saveData("BC_Scheduled_User", None)

# Fix: Answer callback query properly
try:
    bot.answerCallbackQuery("✅ Cancelled!")
except:
    pass''',
    '/PIRO_BC_Confirm': r'''# Admin Check
AllBotAdminss = Bot.getData("AllBotAdminss") or []
is_Admin = False
for userid in AllBotAdminss:
    if str(u) == str(userid):
        is_Admin = True
if is_Admin != True:
    bot.replyText(u, "<b><i>🚫You Are Not This Bot Admin</i></b>")
    raise ReturnCommand()

# Get broadcast data
bc_data = Bot.getData("BC_Data") or {}
if not bc_data:
    bot.replyText(u, "<b>❌ No broadcast data found!</b>")
    raise ReturnCommand()

channels = bc_data.get("channels", [])
if not channels:
    bot.replyText(u, "<b>❌ No channels found!</b>")
    raise ReturnCommand()

# Get message data
txt = bc_data.get("text", "")
file_id = bc_data.get("file_id")
msg_type = bc_data.get("type", "text")
src_chat = bc_data.get("src_chat")
src_msg = bc_data.get("src_msg")

# Update status - starting broadcast
msg_id = Bot.getData("BC_MsgID")
if msg_id:
    bot.editMessageText(
        chat_id=u,
        message_id=msg_id,
        text=f"<b>📢 Broadcasting...</b>\n\n✅ Started: {len(channels)} channels\n⏳ Progress: 0/{len(channels)}"
    )

# Get channel names for failed channels display
AMCU = Bot.getData("AllMainChUsernm") or []
AMCTGID = Bot.getData("AllMainChTGID") or []
AMCL = Bot.getData("AllMainChlink") or []

# Send to each channel
success = 0
failed = 0
failed_list = []
sent_items = []   # [chat_id, message_id] - used for Delete Broadcast

for channel_id in channels:
    try:
        sent = None
        if src_chat and src_msg:
            # Exact copy of the admin's message (keeps bold/italic/premium emoji)
            sent = bot.copyMessage(chat_id=str(channel_id), from_chat_id=src_chat, message_id=src_msg)
        elif msg_type == "text":
            sent = bot.sendMessage(
                chat_id=str(channel_id),
                text=txt,
                parse_mode="html",
                disable_web_page_preview=True
            )
        elif msg_type == "photo":
            sent = bot.sendPhoto(
                chat_id=str(channel_id),
                photo=file_id,
                caption=txt if txt else "",
                parse_mode="html"
            )
        elif msg_type == "video":
            sent = bot.sendVideo(
                chat_id=str(channel_id),
                video=file_id,
                caption=txt if txt else "",
                parse_mode="html"
            )
        elif msg_type == "document":
            sent = bot.sendDocument(
                chat_id=str(channel_id),
                document=file_id,
                caption=txt if txt else "",
                parse_mode="html"
            )
        elif msg_type == "audio":
            sent = bot.sendAudio(
                chat_id=str(channel_id),
                audio=file_id,
                caption=txt if txt else "",
                parse_mode="html"
            )
        elif msg_type == "sticker":
            sent = bot.sendSticker(
                chat_id=str(channel_id),
                sticker=file_id
            )
        elif msg_type == "animation":
            sent = bot.sendAnimation(
                chat_id=str(channel_id),
                animation=file_id,
                caption=txt if txt else "",
                parse_mode="html"
            )
        # remember sent message id so it can be deleted later
        mid = None
        try:
            mid = sent.message_id
        except:
            try:
                mid = sent["message_id"]
            except:
                try:
                    mid = int(sent)
                except:
                    mid = None
        if mid:
            sent_items.append([str(channel_id), mid])
        success += 1
    except:
        failed += 1
        # Store channel info for failed
        ch_name = "Unknown"
        ch_link = "Unknown"
        for i, ch_id in enumerate(AMCTGID):
            if str(ch_id) == str(channel_id):
                if i < len(AMCU):
                    ch_name = AMCU[i]
                if i < len(AMCL):
                    ch_link = AMCL[i]
                break
        
        failed_list.append({
            "id": str(channel_id),
            "name": ch_name,
            "link": ch_link
        })
    
    # Update progress every 2 channels
    if (success + failed) % 2 == 0 or (success + failed) == len(channels):
        if msg_id:
            bot.editMessageText(
                chat_id=u,
                message_id=msg_id,
                text=f"""<b>📢 Broadcasting...</b>

📤 <b>Progress:</b> {success + failed}/{len(channels)}
✅ <b>Success:</b> {success}
❌ <b>Failed:</b> {failed}"""
            )

# Generate EasyTime for logging
now = libs.DateAndTime.now("Asia/Kolkata")
date = now["date"]  
time = now["time"][:5] 
year, month, day = date.split("-")
hour, minute = time.split(":")
hour = int(hour)
ampm = "am"
if hour >= 12:
    ampm = "pm"
if hour > 12:
    hour -= 12
if hour == 0:
    hour = 12

MONTHS = {"01": "Jan", "02": "Feb", "03": "Mar", "04": "Apr","05": "May", "06": "Jun", "07": "Jul", "08": "Aug","09": "Sep", "10": "Oct", "11": "Nov", "12": "Dec"}
EasyTime = f"{int(day)} {MONTHS[month]}, {hour:02}:{minute} {ampm}"

# Plain text of the broadcast (tags stripped) - full text for Delete Broadcast screen + short snippet for list
snip = ""
inside = False
for c in (txt or ""):
    if c == "<":
        inside = True
    elif c == ">":
        inside = False
    elif not inside:
        snip += c
full_text = snip.replace("&", "and")[:700]
snip = full_text.replace("\n", " ")[:18]

# Save broadcast history (last 100, shown 10 per page) for Delete Broadcast
if sent_items:
    BCH = Bot.getData("BC_History") or []
    BCH.append({"time": EasyTime, "type": msg_type, "snip": snip, "full": full_text, "src_chat": src_chat, "src_msg": src_msg, "items": sent_items})
    BCH = BCH[-100:]
    Bot.saveData("BC_History", BCH)

# Final report with links
report = f"""<b>📢 Broadcast Complete</b>

✅ <b>Success:</b> {success}
❌ <b>Failed:</b> {failed}
📊 <b>Total:</b> {len(channels)}"""

if failed_list:
    report += "\n\n<b>⚠️ Failed Channels:</b>\n"
    for i, ch in enumerate(failed_list[:15]):
        name = ch.get('name', 'Unknown')
        link = ch.get('link', '')
        if link and link != "None" and link != "https://t.me/None":
            report += f"{i+1}. <a href='{link}'>{name}</a>\n"
        else:
            report += f"{i+1}. {name} (ID: {ch.get('id', 'Unknown')})\n"
    if len(failed_list) > 15:
        report += f"\n<i>+{len(failed_list)-15} more failures</i>"

# Update final message
if msg_id:
    bot.editMessageText(
        chat_id=u,
        message_id=msg_id,
        text=report,
        disable_web_page_preview=True
    )

# Log the broadcast
AdmAC = Bot.getData("AdmAC") or []
action = f"📢 Broadcast sent to {success} channels ({failed} failed)"
AdmAC.append(f"<b>📆 Time:</b> {EasyTime}\n👥 <b>By {message.from_user.first_name}</b> [ID: <code>{u}</code>]\n🔍<b> Action: </b> {action}")
Bot.saveData("AdmAC", AdmAC)

# Clean up
Bot.saveData("BC_Data", None)
Bot.saveData("BC_MsgID", None)''',
    '/PIRO_BC_Delete': r'''# Admin Check
AllBotAdminss = Bot.getData("AllBotAdminss") or []
is_Admin = False
for userid in AllBotAdminss:
    if str(u) == str(userid):
        is_Admin = True
if is_Admin != True:
    bot.replyText(u, "<b><i>🚫You Are Not This Bot Admin</i></b>")
    raise ReturnCommand()

PER_PAGE = 10
BCH = Bot.getData("BC_History") or []

# Remove any old message-preview that was shown earlier
old_prev = User.getData("BCD_Prev")
if old_prev:
    try:
        bot.deleteMessage(chat_id=u, message_id=int(old_prev))
    except:
        pass
    User.saveData("BCD_Prev", None)

P = []
if str(params) != "None":
    P = str(params).split(" ")

act = "list"
if len(P) > 0:
    act = P[0]

# idx (absolute index in history) and page
idx = None
page = 1
if act == "list":
    if len(P) > 1:
        try:
            page = int(P[1])
        except:
            page = 1
elif act in ("cfm", "do"):
    if len(P) > 1:
        try:
            idx = int(P[1])
        except:
            idx = None
    if len(P) > 2:
        try:
            page = int(P[2])
        except:
            page = 1
elif act == "cfmall":
    if len(P) > 1:
        try:
            page = int(P[1])
        except:
            page = 1

notice = ""

if act in ("cfm", "do"):
    if idx is None or idx < 0 or idx >= len(BCH):
        act = "list"
        notice = "⚠️ <b>Broadcast not found (maybe already deleted)</b>\n\n"

#DELETE ONE
if act == "do":
    batch = BCH[idx]
    ok = 0
    fail = 0
    for it in batch.get("items", []):
        try:
            r = bot.deleteMessage(chat_id=str(it[0]), message_id=int(it[1]))
            if r is False:
                fail += 1
            else:
                ok += 1
        except:
            fail += 1
    BCH.pop(idx)
    Bot.saveData("BC_History", BCH)
    notice = f"<b>🗑 Broadcast Deleted</b>\n✅ Deleted: {ok}\n❌ Failed: {fail}\n\n"
    act = "list"

#DELETE ALL
if act == "doall":
    ok = 0
    fail = 0
    for batch in BCH:
        for it in batch.get("items", []):
            try:
                r = bot.deleteMessage(chat_id=str(it[0]), message_id=int(it[1]))
                if r is False:
                    fail += 1
                else:
                    ok += 1
            except:
                fail += 1
    Bot.saveData("BC_History", [])
    BCH = []
    notice = f"<b>🗑 All Broadcasts Deleted</b>\n✅ Deleted: {ok}\n❌ Failed: {fail}\n\n"
    act = "list"
    page = 1

markup = InlineKeyboardMarkup()

#CONFIRM ONE (shows the broadcast text + actual message
if act == "cfm":
    batch = BCH[idx]
    sc = batch.get("src_chat")
    sm = batch.get("src_msg")
    shown = False
    if sc and sm:
        try:
            r = bot.copyMessage(chat_id=u, from_chat_id=sc, message_id=int(sm))
            pid = None
            try:
                pid = r.message_id
            except:
                try:
                    pid = r["message_id"]
                except:
                    try:
                        pid = int(r)
                    except:
                        pid = None
            if pid:
                User.saveData("BCD_Prev", pid)
            shown = True
        except:
            shown = False
    
    # Broadcast text shown inside this message
    if "full" in batch:
        full = batch.get("full", "")
        if full:
            text_part = f"📝 <b>Broadcast Text:</b>\n{full}\n\n"
        else:
            text_part = "📝 <b>Broadcast Text:</b>\n<i>Text nahi hai (sirf media)</i>\n\n"
    else:
        text_part = "📝 <b>Broadcast Text:</b>\n<i>Ye purana broadcast hai, text save nahi hua tha</i>\n\n"
    
    TXT = f"""<b>⚠️ Delete This Broadcast?</b>

{text_part}📆 <b>Sent:</b> {batch.get('time', '')}
📝 <b>Type:</b> {str(batch.get('type', '')).upper()}
📊 <b>Messages:</b> {len(batch.get('items', []))} channels

<i>Ye message sabhi channels se delete ho jayega.</i>"""
    markup.add(
        InlineKeyboardButton("✅ Yes, Delete", callback_data=f"/PIRO_BC_Delete do {idx} {page}"),
        InlineKeyboardButton("❌ No", callback_data=f"/PIRO_BC_Delete list {page}")
    )
    # list message hata ke confirm message preview ke neeche bhejo
    try:
        bot.deleteMessage(chat_id=u, message_id=message.message_id)
    except:
        pass
    try:
        bot.sendMessage(TXT, reply_markup=markup, parse_mode="HTML")
    except:
        # fallback: text ke bina bhejo
        TXT2 = f"""<b>⚠️ Delete This Broadcast?</b>

📆 <b>Sent:</b> {batch.get('time', '')}
📝 <b>Type:</b> {str(batch.get('type', '')).upper()}
📊 <b>Messages:</b> {len(batch.get('items', []))} channels

<i>Ye message sabhi channels se delete ho jayega.</i>"""
        bot.sendMessage(TXT2, reply_markup=markup, parse_mode="HTML")
    raise ReturnCommand()

#CONFIRM ALL
elif act == "cfmall":
    total = 0
    for batch in BCH:
        total += len(batch.get("items", []))
    TXT = f"""<b>⚠️ Delete ALL Saved Broadcasts?</b>

📦 <b>Broadcasts:</b> {len(BCH)}
📊 <b>Total Messages:</b> {total}

<i>Sabhi channels se ye saare broadcast messages delete ho jayenge.</i>"""
    markup.add(
        InlineKeyboardButton("✅ Yes, Delete ALL", callback_data="/PIRO_BC_Delete doall"),
        InlineKeyboardButton("❌ No", callback_data=f"/PIRO_BC_Delete list {page}")
    )

#LIST (10 per page
else:
    total_pages = (len(BCH) + PER_PAGE - 1) // PER_PAGE
    if total_pages < 1:
        total_pages = 1
    if page < 1:
        page = 1
    if page > total_pages:
        page = total_pages
    
    if not BCH:
        TXT = notice + "<b>🗑 Delete Broadcast</b>\n\n<i>Koi saved broadcast nahi hai.</i>"
    else:
        TXT = notice + f"""<b>🗑 Delete Broadcast</b>

📦 <b>Saved Broadcasts:</b> {len(BCH)}
📄 <b>Page:</b> {page}/{total_pages}
<i>Jo broadcast delete karna hai uspe click karo (message preview dikhega).</i>

<i>Note: Telegram 48 ghante se purane channel messages delete nahi karne deta.</i>"""
        # newest first (no while loop allowed in TPY)
        start = (page - 1) * PER_PAGE
        end = start + PER_PAGE
        if end > len(BCH):
            end = len(BCH)
        for j in range(start, end):
            i = len(BCH) - 1 - j
            b = BCH[i]
            snip = b.get("snip", "")
            label = f"🗑 {b.get('time', '')} • {str(b.get('type', '')).upper()}"
            if snip:
                label += f" • {snip}"
            label += f" • {len(b.get('items', []))}ch"
            markup.add(InlineKeyboardButton(label, callback_data=f"/PIRO_BC_Delete cfm {i} {page}"))
        
        # Page navigation
        if total_pages > 1:
            nav = []
            if page > 1:
                nav.append(InlineKeyboardButton("◀ Prev", callback_data=f"/PIRO_BC_Delete list {page - 1}"))
            nav.append(InlineKeyboardButton(f"📄 {page}/{total_pages}", callback_data=f"/PIRO_BC_Delete list {page}"))
            if page < total_pages:
                nav.append(InlineKeyboardButton("Next ▶", callback_data=f"/PIRO_BC_Delete list {page + 1}"))
            markup.add(*nav)
        
        markup.add(InlineKeyboardButton("🗑 Delete ALL Broadcasts", callback_data=f"/PIRO_BC_Delete cfmall {page}"))
    markup.add(InlineKeyboardButton("🔙 Back", callback_data="/CHANNEL_BC"))

try:
    bot.editMessageText(
        chat_id=u,
        message_id=message.message_id,
        text=TXT,
        reply_markup=markup,
        parse_mode="HTML"
    )
except:
    bot.sendMessage(TXT, reply_markup=markup, parse_mode="HTML")''',
    '/PIRO_BC_Process': r'''# Admin Check
AllBotAdminss = Bot.getData("AllBotAdminss") or []
is_Admin = False
for userid in AllBotAdminss:
    if str(u) == str(userid):
        is_Admin = True
if is_Admin != True:
    bot.replyText(u, "<b><i>🚫You Are Not This Bot Admin</i></b>")
    raise ReturnCommand()

if options == None:
    AMCTGID = Bot.getData("AllMainChTGID") or []
    msg = bot.replyText(u, f"""<b>📢 Broadcast to {len(AMCTGID)} Channels</b>

<i>Send any message (text/photo/video/document)</i>
<b>Send /cancel to cancel</b>""")
    Bot.saveData("BC_MsgID", msg.message_id)
    Bot.handleNextCommand("/PIRO_BC_Process", options=True)
    raise ReturnCommand()
else:
    if message.text == "/cancel":
        msg_id = Bot.getData("BC_MsgID")
        if msg_id:
            bot.editMessageText(chat_id=u, message_id=msg_id, text="<b>❌ Broadcast Cancelled</b>")
        Bot.saveData("BC_MsgID", None)
        raise ReturnCommand()
    
    # Detect content type
    txt = message.caption if message.caption else message.text
    entities = message.caption_entities if message.caption else message.entities
    if entities:
        txt = apply_html_entities(txt, entities, {})
    
    # Get message type and file_id
    msg_type = "text"
    file_id = None
    
    if message.photo:
        msg_type = "photo"
        file_id = message.photo[-1].file_id
    elif message.video:
        msg_type = "video"
        file_id = message.video.file_id
    elif message.document:
        msg_type = "document"
        file_id = message.document.file_id
    elif message.audio:
        msg_type = "audio"
        file_id = message.audio.file_id
    elif message.sticker:
        msg_type = "sticker"
        file_id = message.sticker.file_id
    elif message.animation:
        msg_type = "animation"
        file_id = message.animation.file_id
    elif message.text:
        msg_type = "text"
    else:
        msg_id = Bot.getData("BC_MsgID")
        if msg_id:
            bot.editMessageText(chat_id=u, message_id=msg_id, text="<b>❌ Unsupported File Format!</b>")
        Bot.saveData("BC_MsgID", None)
        raise ReturnCommand()
    
    # Save broadcast data (src_chat/src_msg = original message, used for exact copy incl. premium emoji)
    broadcast_data = {
        "type": msg_type,
        "text": txt,
        "file_id": file_id,
        "src_chat": u,
        "src_msg": message.message_id,
        "channels": Bot.getData("AllMainChTGID") or []
    }
    Bot.saveData("BC_Data", broadcast_data)
    
    # Exact preview = copy of the original message (bold/italic/premium emoji stay as they are)
    shown = False
    try:
        bot.copyMessage(chat_id=u, from_chat_id=u, message_id=message.message_id)
        shown = True
    except:
        shown = False
    
    # Fallback plain text content (only if exact preview failed)
    plain = ""
    inside = False
    for c in (txt or ""):
        if c == "<":
            inside = True
        elif c == ">":
            inside = False
        elif not inside:
            plain += c
    plain = plain.replace("&", "and")
    
    if shown:
        content_part = "👆 <i>Upar wala message aise hi channels me jayega</i>"
    else:
        content_part = f"🔄 <b>Content:</b>\n{plain[:300] if plain else 'No text'}{'...' if plain and len(plain) > 300 else ''}"
    
    preview = f"""<b>📢 Broadcast Preview</b>

📊 <b>Target:</b> {len(broadcast_data['channels'])} channels
📝 <b>Type:</b> {msg_type.upper()}

{content_part}

<i>✅ Click Confirm to broadcast</i>
<i>❌ Click Cancel to abort</i>"""
    
    markup = InlineKeyboardMarkup()
    markup.add(
        InlineKeyboardButton("✅ Confirm", callback_data="/PIRO_BC_Confirm"),
        InlineKeyboardButton("❌ Cancel", callback_data="/PIRO_BC_Cancel")
    )
    
    try:
        new_msg = bot.replyText(u, preview, reply_markup=markup)
        Bot.saveData("BC_MsgID", new_msg.message_id)
    except:
        short = f"""<b>📢 Broadcast Preview</b>

📊 <b>Target:</b> {len(broadcast_data['channels'])} channels
📝 <b>Type:</b> {msg_type.upper()}

<i>✅ Click Confirm to broadcast</i>
<i>❌ Click Cancel to abort</i>"""
        new_msg = bot.replyText(u, short, reply_markup=markup)
        Bot.saveData("BC_MsgID", new_msg.message_id)''',
    '/PIRO_BC_Schedule_Confirm': r'''# Admin Check
AllBotAdminss = Bot.getData("AllBotAdminss") or []
is_Admin = False
for userid in AllBotAdminss:
    if str(u) == str(userid):
        is_Admin = True
if is_Admin != True:
    bot.replyText(u, "<b><i>🚫You Are Not This Bot Admin</i></b>")
    raise ReturnCommand()

bc_data = Bot.getData("BC_Data") or {}
if not bc_data:
    bot.replyText(u, "<b>❌ No broadcast data found!</b>")
    raise ReturnCommand()

date = Bot.getData("BC_Schedule_Date")
time_display = Bot.getData("BC_Schedule_Time_12hr")
hour_24 = Bot.getData("BC_Schedule_Hour")
minute = Bot.getData("BC_Schedule_Minute")

if not date or hour_24 is None or minute is None:
    bot.replyText(u, "<b>❌ Schedule date/time missing! Please schedule again.</b>")
    raise ReturnCommand()

# Get current time (India)
now = libs.DateAndTime.now("Asia/Kolkata")
current_date = now["date"]
current_time = now["time"][:5]

# Parse scheduled date (DD-MM-YYYY)
date_parts = date.split("-")
sched_day = int(date_parts[0])
sched_month = int(date_parts[1])
sched_year = int(date_parts[2])

# Parse current date (YYYY-MM-DD)
current_parts = current_date.split("-")
curr_year = int(current_parts[0])
curr_month = int(current_parts[1])
curr_day = int(current_parts[2])

# Parse current time
curr_time_parts = current_time.split(":")
curr_hour = int(curr_time_parts[0])
curr_min = int(curr_time_parts[1])

# Exact delay (no imports allowed, so day-number formula is used)
y = sched_year
m = sched_month
if m <= 2:
    y -= 1
    m += 12
sched_days = 365 * y + y // 4 - y // 100 + y // 400 + (153 * (m - 3) + 2) // 5 + sched_day

y = curr_year
m = curr_month
if m <= 2:
    y -= 1
    m += 12
curr_days = 365 * y + y // 4 - y // 100 + y // 400 + (153 * (m - 3) + 2) // 5 + curr_day

delay_seconds = (sched_days - curr_days) * 86400 + (int(hour_24) - curr_hour) * 3600 + (int(minute) - curr_min) * 60

if delay_seconds < 0:
    current_date_display = f"{curr_day:02d}-{curr_month:02d}-{curr_year}"
    bot.replyText(u, f"""<b>❌ Scheduled time is in the past!</b>

📆 <b>Current:</b> {current_date_display} at {current_time}
📆 <b>Scheduled:</b> {date} at {time_display}

<i>Please select a future date and time</i>""")
    raise ReturnCommand()

if delay_seconds < 5:
    delay_seconds = 5

# Create a unique task ID
task_counter = Bot.getData("BC_Task_Counter") or 0
task_counter += 1
Bot.saveData("BC_Task_Counter", task_counter)
task_id = f"sch_bc_{u}_{task_counter}"

# Save the task info
Bot.saveData(f"BC_Task_{task_id}", {
    "user": u,
    "data": bc_data,
    "date": date,
    "time": time_display
})

# Save all broadcast data (kept for compatibility)
Bot.saveData("BC_Scheduled_Data", bc_data)
Bot.saveData("BC_Scheduled_Date_Display", date)
Bot.saveData("BC_Scheduled_Time_Display", time_display)
Bot.saveData("BC_Scheduled_User", u)

# *** THE ACTUAL TIMER (this was missing) ***
try:
    job = Bot.runCommandAfter(delay_seconds, "/PIRO_BC_Schedule_Execute", options=task_id)
except Exception as e:
    bot.replyText(u, f"<b>❌ Could not schedule broadcast:</b>\n<code>{str(e)[:300]}</code>")
    raise ReturnCommand()

# Show confirmation by editing the message
msg_id = Bot.getData("BC_MsgID")
if msg_id:
    try:
        bot.editMessageText(
            chat_id=u,
            message_id=msg_id,
            text=f"""<b>📅 Broadcast Scheduled!</b>

📆 <b>Date:</b> {date}
⏰ <b>Time:</b> {time_display}
📊 <b>Target:</b> {len(bc_data.get('channels', []))} channels

<i>✅ Broadcast will start automatically at scheduled time</i>"""
        )
    except:
        pass

# Generate EasyTime for logging
MONTHS = {"01": "Jan", "02": "Feb", "03": "Mar", "04": "Apr","05": "May", "06": "Jun", "07": "Jul", "08": "Aug","09": "Sep", "10": "Oct", "11": "Nov", "12": "Dec"}

sched_hour_display = int(hour_24)
sched_ampm = "am"
if sched_hour_display >= 12:
    sched_ampm = "pm"
if sched_hour_display > 12:
    sched_hour_display -= 12
if sched_hour_display == 0:
    sched_hour_display = 12

EasyTime = f"{sched_day:02d} {MONTHS[date_parts[1]]}, {sched_hour_display:02d}:{int(minute):02d} {sched_ampm}"

# Log the schedule
AdmAC = Bot.getData("AdmAC") or []
action = f"📅 Broadcast scheduled for {EasyTime} ({len(bc_data.get('channels', []))} channels)"
AdmAC.append(f"<b>📆 Time:</b> {EasyTime}\n👥 <b>By {message.from_user.first_name}</b> [ID: <code>{u}</code>]\n🔍<b> Action: </b> {action}")
Bot.saveData("AdmAC", AdmAC)''',
    '/PIRO_BC_Schedule_Date': r'''# Admin Check
AllBotAdminss = Bot.getData("AllBotAdminss") or []
is_Admin = False
for userid in AllBotAdminss:
    if str(u) == str(userid):
        is_Admin = True
if is_Admin != True:
    bot.replyText(u, "<b><i>🚫You Are Not This Bot Admin</i></b>")
    raise ReturnCommand()

if options == None:
    AMCTGID = Bot.getData("AllMainChTGID") or []
    msg = bot.replyText(u, f"""<b>📅 Schedule Broadcast to {len(AMCTGID)} Channels</b>

<i>Send the date for broadcast</i>
<b>Format:</b> DD-MM-YYYY
<b>Example:</b> 25-12-2025

<i>Send /cancel to cancel</i>""")
    Bot.saveData("BC_MsgID", msg.message_id)
    Bot.handleNextCommand("/PIRO_BC_Schedule_Date", options=True)
    raise ReturnCommand()
else:
    if message.text == "/cancel":
        msg_id = Bot.getData("BC_MsgID")
        if msg_id:
            try:
                bot.editMessageText(chat_id=u, message_id=msg_id, text="<b>❌ Schedule Cancelled</b>")
            except:
                pass
        Bot.saveData("BC_MsgID", None)
        raise ReturnCommand()
    
    # Validate date format
    try:
        date_parts = message.text.split("-")
        if len(date_parts) != 3:
            raise ValueError()
        day = int(date_parts[0])
        month = int(date_parts[1])
        year = int(date_parts[2])
        
        # Check if date is valid
        if day < 1 or day > 31 or month < 1 or month > 12 or year < 2024:
            raise ValueError()
        
        # Get current date from TBC (format: YYYY-MM-DD)
        now = libs.DateAndTime.now("Asia/Kolkata")
        current_date = now["date"]
        current_parts = current_date.split("-")
        current_year = int(current_parts[0])
        current_month = int(current_parts[1])
        current_day = int(current_parts[2])
        
        # Compare dates - Allow today's date
        is_valid = False
        if year > current_year:
            is_valid = True
        elif year == current_year and month > current_month:
            is_valid = True
        elif year == current_year and month == current_month and day >= current_day:
            is_valid = True
        
        if not is_valid:
            current_date_display = f"{current_day:02d}-{current_month:02d}-{current_year}"
            bot.replyText(u, f"""<b>❌ Please select today or a future date!</b>

📆 <b>Current Date:</b> {current_date_display}
📆 <b>You entered:</b> {message.text}

<i>Please enter today's date or a future date</i>""")
            raise ReturnCommand()
        
        Bot.saveData("BC_Schedule_Date", message.text)
        
        # Ask for time with AM/PM format
        msg = bot.replyText(u, f"""<b>📅 Schedule Broadcast</b>

📆 <b>Date:</b> {message.text}

<i>Send the time for broadcast</i>
<b>Format:</b> HH:MM AM/PM
<b>Examples:</b> 
• 02:30 PM (for 2:30 PM)
• 09:00 AM (for 9:00 AM)
• 12:00 PM (for 12:00 PM)

<i>Send /cancel to cancel</i>""")
        
        Bot.saveData("BC_MsgID", msg.message_id)
        Bot.handleNextCommand("/PIRO_BC_Schedule_Time", options=True)
        
    except:
        bot.replyText(u, """<b>❌ Invalid date format!</b>

<b>Use:</b> DD-MM-YYYY
<b>Example:</b> 25-12-2025

<i>Please send date in correct format</i>""")
        raise ReturnCommand()''',
    '/PIRO_BC_Schedule_Execute': r'''# No admin check - runs automatically via Bot.runCommandAfter()

# Get task ID from options
task_id = options
task_data = Bot.getData(f"BC_Task_{task_id}") or {}

if not task_data:
    raise ReturnCommand()

# Get data from task
u = task_data.get("user")
bc_data = task_data.get("data", {})
date = task_data.get("date", "Unknown")
time = task_data.get("time", "Unknown")

channels = bc_data.get("channels", [])
txt = bc_data.get("text", "")
file_id = bc_data.get("file_id")
msg_type = bc_data.get("type", "text")
src_chat = bc_data.get("src_chat")
src_msg = bc_data.get("src_msg")

if not channels:
    bot.replyText(u, "<b>❌ No channels found!</b>")
    raise ReturnCommand()

# Send notification
bot.replyText(u, f"""<b>📢 Scheduled Broadcast Started!</b>

📆 <b>Scheduled:</b> {date} at {time}
📊 <b>Target:</b> {len(channels)} channels
📝 <b>Type:</b> {msg_type.upper()}

<i>⏳ Broadcasting in progress...</i>""")

# Get channel names for failed display
AMCU = Bot.getData("AllMainChUsernm") or []
AMCTGID = Bot.getData("AllMainChTGID") or []
AMCL = Bot.getData("AllMainChlink") or []

# Send to channels
success = 0
failed = 0
failed_list = []
sent_items = []   # [chat_id, message_id] - used for Delete Broadcast

for channel_id in channels:
    try:
        sent = None
        if src_chat and src_msg:
            sent = bot.copyMessage(chat_id=str(channel_id), from_chat_id=src_chat, message_id=src_msg)
        elif msg_type == "text":
            sent = bot.sendMessage(chat_id=str(channel_id), text=txt, parse_mode="html", disable_web_page_preview=True)
        elif msg_type == "photo":
            sent = bot.sendPhoto(chat_id=str(channel_id), photo=file_id, caption=txt if txt else "", parse_mode="html")
        elif msg_type == "video":
            sent = bot.sendVideo(chat_id=str(channel_id), video=file_id, caption=txt if txt else "", parse_mode="html")
        elif msg_type == "document":
            sent = bot.sendDocument(chat_id=str(channel_id), document=file_id, caption=txt if txt else "", parse_mode="html")
        elif msg_type == "audio":
            sent = bot.sendAudio(chat_id=str(channel_id), audio=file_id, caption=txt if txt else "", parse_mode="html")
        elif msg_type == "sticker":
            sent = bot.sendSticker(chat_id=str(channel_id), sticker=file_id)
        elif msg_type == "animation":
            sent = bot.sendAnimation(chat_id=str(channel_id), animation=file_id, caption=txt if txt else "", parse_mode="html")
        mid = None
        try:
            mid = sent.message_id
        except:
            try:
                mid = sent["message_id"]
            except:
                try:
                    mid = int(sent)
                except:
                    mid = None
        if mid:
            sent_items.append([str(channel_id), mid])
        success += 1
    except:
        failed += 1
        # Get channel name
        ch_name = str(channel_id)
        ch_link = ""
        for i, ch_id in enumerate(AMCTGID):
            if str(ch_id) == str(channel_id):
                if i < len(AMCU):
                    ch_name = AMCU[i]
                if i < len(AMCL):
                    ch_link = AMCL[i]
                break
        failed_list.append({
            "name": ch_name,
            "link": ch_link
        })

# Generate EasyTime
now = libs.DateAndTime.now("Asia/Kolkata")
date_now = now["date"]  
time_now = now["time"][:5] 
year, month, day = date_now.split("-")
hour, minute = time_now.split(":")
hour = int(hour)
ampm = "am"
if hour >= 12:
    ampm = "pm"
if hour > 12:
    hour -= 12
if hour == 0:
    hour = 12

MONTHS = {"01": "Jan", "02": "Feb", "03": "Mar", "04": "Apr","05": "May", "06": "Jun", "07": "Jul", "08": "Aug","09": "Sep", "10": "Oct", "11": "Nov", "12": "Dec"}
EasyTime = f"{int(day)} {MONTHS[month]}, {hour:02}:{minute} {ampm}"

# Plain text of the broadcast (tags stripped) - full text for Delete Broadcast screen + short snippet for list
snip = ""
inside = False
for c in (txt or ""):
    if c == "<":
        inside = True
    elif c == ">":
        inside = False
    elif not inside:
        snip += c
full_text = snip.replace("&", "and")[:700]
snip = full_text.replace("\n", " ")[:18]

# Save broadcast history (last 100, shown 10 per page) for Delete Broadcast
if sent_items:
    BCH = Bot.getData("BC_History") or []
    BCH.append({"time": EasyTime, "type": msg_type, "snip": snip, "full": full_text, "src_chat": src_chat, "src_msg": src_msg, "items": sent_items})
    BCH = BCH[-100:]
    Bot.saveData("BC_History", BCH)

# Result report to admin
bot.sendMessage(chat_id=str(u), text=f"""<b>📢 Scheduled Broadcast Complete</b>

✅ <b>Success:</b> {success}
❌ <b>Failed:</b> {failed}
📊 <b>Total:</b> {len(channels)}""", parse_mode="HTML")''',
    '/PIRO_BC_Schedule_Message': r'''# Admin Check
AllBotAdminss = Bot.getData("AllBotAdminss") or []
is_Admin = False
for userid in AllBotAdminss:
    if str(u) == str(userid):
        is_Admin = True
if is_Admin != True:
    bot.replyText(u, "<b><i>🚫You Are Not This Bot Admin</i></b>")
    raise ReturnCommand()

if options == None:
    date = Bot.getData("BC_Schedule_Date") or "DD-MM-YYYY"
    time_display = Bot.getData("BC_Schedule_Time_12hr") or "HH:MM AM/PM"
    msg = bot.replyText(u, f"""<b>📅 Schedule Broadcast</b>

📆 <b>Date:</b> {date}
⏰ <b>Time:</b> {time_display}

<i>Now send your message (text/photo/video/document)</i>
<b>Send /cancel to cancel</b>""")
    Bot.saveData("BC_MsgID", msg.message_id)
    Bot.handleNextCommand("/PIRO_BC_Schedule_Message", options=True)
    raise ReturnCommand()
else:
    if message.text == "/cancel":
        msg_id = Bot.getData("BC_MsgID")
        if msg_id:
            try:
                bot.editMessageText(chat_id=u, message_id=msg_id, text="<b>❌ Schedule Cancelled</b>")
            except:
                pass
        Bot.saveData("BC_MsgID", None)
        Bot.saveData("BC_Schedule_Date", None)
        Bot.saveData("BC_Schedule_Time_12hr", None)
        Bot.saveData("BC_Schedule_Time_24hr", None)
        Bot.saveData("BC_Schedule_Hour", None)
        Bot.saveData("BC_Schedule_Minute", None)
        raise ReturnCommand()
    
    # Detect content type
    txt = message.caption if message.caption else message.text
    entities = message.caption_entities if message.caption else message.entities
    if entities:
        txt = apply_html_entities(txt, entities, {})
    
    # Get message type and file_id
    msg_type = "text"
    file_id = None
    
    if message.photo:
        msg_type = "photo"
        file_id = message.photo[-1].file_id
    elif message.video:
        msg_type = "video"
        file_id = message.video.file_id
    elif message.document:
        msg_type = "document"
        file_id = message.document.file_id
    elif message.audio:
        msg_type = "audio"
        file_id = message.audio.file_id
    elif message.sticker:
        msg_type = "sticker"
        file_id = message.sticker.file_id
    elif message.animation:
        msg_type = "animation"
        file_id = message.animation.file_id
    elif message.text:
        msg_type = "text"
    else:
        msg_id = Bot.getData("BC_MsgID")
        if msg_id:
            try:
                bot.editMessageText(chat_id=u, message_id=msg_id, text="<b>❌ Unsupported File Format!</b>")
            except:
                pass
        Bot.saveData("BC_MsgID", None)
        Bot.saveData("BC_Schedule_Date", None)
        Bot.saveData("BC_Schedule_Time_12hr", None)
        Bot.saveData("BC_Schedule_Time_24hr", None)
        Bot.saveData("BC_Schedule_Hour", None)
        Bot.saveData("BC_Schedule_Minute", None)
        raise ReturnCommand()
    
    # Save scheduled broadcast data
    broadcast_data = {
        "type": msg_type,
        "text": txt,
        "file_id": file_id,
        "src_chat": u,
        "src_msg": message.message_id,
        "channels": Bot.getData("AllMainChTGID") or []
    }
    Bot.saveData("BC_Data", broadcast_data)
    
    date = Bot.getData("BC_Schedule_Date")
    time_display = Bot.getData("BC_Schedule_Time_12hr")
    
    # Exact preview = copy of the original message
    shown = False
    try:
        bot.copyMessage(chat_id=u, from_chat_id=u, message_id=message.message_id)
        shown = True
    except:
        shown = False
    
    plain = ""
    inside = False
    for c in (txt or ""):
        if c == "<":
            inside = True
        elif c == ">":
            inside = False
        elif not inside:
            plain += c
    plain = plain.replace("&", "and")
    
    if shown:
        content_part = "👆 <i>Upar wala message aise hi channels me jayega</i>"
    else:
        content_part = f"🔄 <b>Content:</b>\n{plain[:300] if plain else 'No text'}{'...' if plain and len(plain) > 300 else ''}"
    
    preview = f"""<b>📅 Schedule Broadcast Confirmation</b>

📆 <b>Date:</b> {date}
⏰ <b>Time:</b> {time_display}
📊 <b>Target:</b> {len(broadcast_data['channels'])} channels
📝 <b>Type:</b> {msg_type.upper()}

{content_part}

<i>✅ Click Confirm to schedule</i>
<i>❌ Click Cancel to abort</i>"""
    
    markup = InlineKeyboardMarkup()
    markup.add(
        InlineKeyboardButton("✅ Schedule", callback_data="/PIRO_BC_Schedule_Confirm"),
        InlineKeyboardButton("❌ Cancel", callback_data="/PIRO_BC_Cancel")
    )
    
    try:
        new_msg = bot.replyText(u, preview, reply_markup=markup)
        Bot.saveData("BC_MsgID", new_msg.message_id)
    except:
        short = f"""<b>📅 Schedule Broadcast Confirmation</b>

📆 <b>Date:</b> {date}
⏰ <b>Time:</b> {time_display}
📊 <b>Target:</b> {len(broadcast_data['channels'])} channels

<i>✅ Click Confirm to schedule</i>
<i>❌ Click Cancel to abort</i>"""
        new_msg = bot.replyText(u, short, reply_markup=markup)
        Bot.saveData("BC_MsgID", new_msg.message_id)''',
    '/PIRO_BC_Schedule_Time': r'''# Admin Check
AllBotAdminss = Bot.getData("AllBotAdminss") or []
is_Admin = False
for userid in AllBotAdminss:
    if str(u) == str(userid):
        is_Admin = True
if is_Admin != True:
    bot.replyText(u, "<b><i>🚫You Are Not This Bot Admin</i></b>")
    raise ReturnCommand()

if options == None:
    date = Bot.getData("BC_Schedule_Date") or "DD-MM-YYYY"
    msg = bot.replyText(u, f"""<b>📅 Schedule Broadcast</b>

📆 <b>Date:</b> {date}

<i>Send the time for broadcast</i>
<b>Format:</b> HH:MM AM/PM
<b>Examples:</b> 
• 02:30 PM (for 2:30 PM)
• 09:00 AM (for 9:00 AM)
• 12:00 PM (for 12:00 PM)
• 8:34 PM (for 8:34 PM)

<i>Send /cancel to cancel</i>""")
    Bot.saveData("BC_MsgID", msg.message_id)
    Bot.handleNextCommand("/PIRO_BC_Schedule_Time", options=True)
    raise ReturnCommand()
else:
    # This runs when user sends the time
    if message.text == "/cancel":
        msg_id = Bot.getData("BC_MsgID")
        if msg_id:
            try:
                bot.editMessageText(chat_id=u, message_id=msg_id, text="<b>❌ Schedule Cancelled</b>")
            except:
                pass
        Bot.saveData("BC_MsgID", None)
        Bot.saveData("BC_Schedule_Date", None)
        raise ReturnCommand()
    
    # Validate time format (HH:MM AM/PM or H:MM AM/PM)
    try:
        user_input = message.text
        
        # Check if message has AM/PM
        if "AM" not in user_input.upper() and "PM" not in user_input.upper():
            bot.replyText(u, """<b>❌ Invalid time format!</b>

<b>Use:</b> HH:MM AM/PM
<b>Examples:</b> 
• 02:30 PM (for 2:30 PM)
• 09:00 AM (for 9:00 AM)
• 12:00 PM (for 12:00 PM)
• 8:34 PM (for 8:34 PM)

<i>Please send time in correct format</i>""")
            raise ReturnCommand()
        
        # Split by space to get time and AM/PM
        parts = user_input.upper().split(" ")
        if len(parts) != 2:
            bot.replyText(u, """<b>❌ Invalid format!</b>
Please use format: HH:MM AM/PM
Example: 02:30 PM""")
            raise ReturnCommand()
        
        time_str = parts[0].strip()
        ampm_str = parts[1].strip()
        
        if ampm_str not in ["AM", "PM"]:
            bot.replyText(u, "<b>❌ Invalid AM/PM! Use AM or PM</b>")
            raise ReturnCommand()
        
        # Split time by colon
        time_components = time_str.split(":")
        if len(time_components) != 2:
            bot.replyText(u, "<b>❌ Invalid time format! Use HH:MM</b>")
            raise ReturnCommand()
        
        hour = int(time_components[0].strip())
        minute = int(time_components[1].strip())
        
        if hour < 1 or hour > 12:
            bot.replyText(u, "<b>❌ Hour must be between 1-12</b>")
            raise ReturnCommand()
        if minute < 0 or minute > 59:
            bot.replyText(u, "<b>❌ Minute must be between 0-59</b>")
            raise ReturnCommand()
        
        # Convert to 24-hour format for storage
        if ampm_str == "PM" and hour != 12:
            hour_24 = hour + 12
        elif ampm_str == "AM" and hour == 12:
            hour_24 = 0
        else:
            hour_24 = hour
        
        # Format the display time with leading zero if needed
        display_time = f"{hour:02d}:{minute:02d} {ampm_str}"
        
        # Store both formats
        Bot.saveData("BC_Schedule_Time_12hr", display_time)
        Bot.saveData("BC_Schedule_Time_24hr", f"{hour_24:02d}:{minute:02d}")
        Bot.saveData("BC_Schedule_Hour", hour_24)
        Bot.saveData("BC_Schedule_Minute", minute)
        
        # Check if time is in future (if date is today)
        date = Bot.getData("BC_Schedule_Date")
        now = libs.DateAndTime.now("Asia/Kolkata")
        current_date = now["date"]
        current_time = now["time"][:5]
        
        # Parse stored date (DD-MM-YYYY)
        date_parts = date.split("-")
        sched_day = int(date_parts[0])
        sched_month = int(date_parts[1])
        sched_year = int(date_parts[2])
        
        # Parse current date (YYYY-MM-DD)
        curr_parts = current_date.split("-")
        curr_year = int(curr_parts[0])
        curr_month = int(curr_parts[1])
        curr_day = int(curr_parts[2])
        
        # Check if scheduled date is today
        is_today = (sched_year == curr_year and sched_month == curr_month and sched_day == curr_day)
        
        if is_today:
            curr_hour = int(current_time.split(":")[0])
            curr_min = int(current_time.split(":")[1])
            
            if hour_24 < curr_hour or (hour_24 == curr_hour and minute <= curr_min):
                bot.replyText(u, f"""<b>❌ Please select a future time!</b>

⏰ <b>Current Time:</b> {current_time}
⏰ <b>You entered:</b> {display_time}

<i>Please enter a time after current time</i>""")
                raise ReturnCommand()
        
        # Ask for message
        date = Bot.getData("BC_Schedule_Date")
        time_display = Bot.getData("BC_Schedule_Time_12hr")
        
        msg = bot.replyText(u, f"""<b>📅 Schedule Broadcast</b>

📆 <b>Date:</b> {date}
⏰ <b>Time:</b> {time_display}

<i>Now send your message (text/photo/video/document)</i>
<b>Send /cancel to cancel</b>""")
        
        Bot.saveData("BC_MsgID", msg.message_id)
        Bot.handleNextCommand("/PIRO_BC_Schedule_Message", options=True)
        
    except ValueError:
        bot.replyText(u, """<b>❌ Invalid time format!</b>

<b>Use:</b> HH:MM AM/PM
<b>Examples:</b> 
• 02:30 PM (for 2:30 PM)
• 09:00 AM (for 9:00 AM)
• 12:00 PM (for 12:00 PM)
• 8:34 PM (for 8:34 PM)

<i>Please send time in correct format</i>""")
        raise ReturnCommand()
    except:
        bot.replyText(u, """<b>❌ Invalid time format!</b>

<b>Use:</b> HH:MM AM/PM
<b>Examples:</b> 
• 02:30 PM (for 2:30 PM)
• 09:00 AM (for 9:00 AM)
• 12:00 PM (for 12:00 PM)
• 8:34 PM (for 8:34 PM)

<i>Please send time in correct format</i>""")
        raise ReturnCommand()''',
    '/PIRO_Broadcast': r'''# Admin Check
AllBotAdminss = Bot.getData("AllBotAdminss") or []
is_Admin = False
for userid in AllBotAdminss:
    if str(u) == str(userid):
        is_Admin = True
if is_Admin != True:
    bot.replyText(u, "<b><i>🚫You Are Not This Bot Admin</i></b>")
    raise ReturnCommand()

# Get all channels
AMCTGID = Bot.getData("AllMainChTGID") or []
if not AMCTGID:
    bot.replyText(u, "<b>❌ No Channels Added Yet!</b>")
    raise ReturnCommand()

# Ask for message
msg = bot.replyText(u, f"""<b>📢 Broadcast to {len(AMCTGID)} Channels</b>

<i>Send any message (text/photo/video/document)</i>
<b>Send /cancel to cancel</b>""")

Bot.saveData("BC_MsgID", msg.message_id)
Bot.handleNextCommand("/PIRO_BC_Process", options=True)''',
    '/PIRO_ChLinks': r'''# 19'4'25   00'11'10  v:1'0'0 [√]
# CODED BY: @Owner_Here 
# [ DM TO BUY ANY BOTS & CODES ]

AllBotAdminss = Bot.getData("AllBotAdminss") or []

is_Admin = False
for userid in AllBotAdminss:
    if str(u) == str(userid):
        is_Admin = True

if is_Admin != True:
    bot.replyText(u, "<b><i>🚫You Are Not This Bot Admin</i></b>")
    raise ReturnCommand()

AMCL=Bot.getData("AllMainChlink") or []

Links=""
for L in AMCL:
    Links=f"{Links}\n{L}"

if Links!="":
    bot.sendMessage(Links)
else:
    bot.sendMessage("No Links Found")''',
    '/PIRO_Chanel_Pannel': r'''# 19'4'25   00'11'10  v:1'0'0 [√]
# CODED BY: @Owner_Here 
# [ DM TO BUY ANY BOTS & CODES ]
# Page-wise panel: next/prev nav + auto-add-to-current-page

AllBotAdminss = Bot.getData("AllBotAdminss") or []

is_Admin = False
for userid in AllBotAdminss:
    if str(u) == str(userid):
        is_Admin = True

if is_Admin != True:
    bot.replyText(u, "<b><i>🚫You Are Not This Bot Admin</i></b>")
    raise ReturnCommand()


OWNER=Bot.getData("Owner") or "1294066842"

AMC = Bot.getData("AllMainCh") or []
AMCL=Bot.getData("AllMainChlink") or []
AMCU=Bot.getData("AllMainChUsernm") or []
AMCTGID=Bot.getData("AllMainChTGID") or []

AMCP=Bot.getData("AllMainChPage") or []
for _pad in range(len(AMCTGID) - len(AMCP)):
    AMCP.append(1)

# Self-heal any array-length drift so a bad delete/add can never crash the panel
for _pad in range(len(AMCTGID) - len(AMC)):
    AMC.append("Unknown")
for _pad in range(len(AMCTGID) - len(AMCU)):
    AMCU.append("Unknown")
for _pad in range(len(AMCTGID) - len(AMCL)):
    AMCL.append("https://t.me/" + str(AMCTGID[len(AMCL)]))

StoredPageCount = Bot.getData("AllMainChPageCount") or 1
MaxChPage = max(AMCP) if AMCTGID else 1
TotalPages = max(int(StoredPageCount), int(MaxChPage), 1)

AdminCurPage = User.getData("PIRO_AdminCurPage")
if AdminCurPage is None:
    AdminCurPage = 1
else:
    AdminCurPage = int(AdminCurPage)
if AdminCurPage < 1:
    AdminCurPage = 1
if AdminCurPage > TotalPages:
    AdminCurPage = TotalPages


now = libs.DateAndTime.now("Asia/Kolkata")
date = now["date"]  
time = now["time"][:5] 
year, month, day = date.split("-")
hour, minute = time.split(":")
hour = int(hour)
ampm = "am"
if hour >= 12:
    ampm = "pm"
if hour > 12:
    hour -= 12
if hour == 0:
    hour = 12

MONTHS = {"01": "Jan", "02": "Feb", "03": "Mar", "04": "Apr","05": "May", "06": "Jun", "07": "Jul", "08": "Aug","09": "Sep", "10": "Oct", "11": "Nov", "12": "Dec"}

EasyTime = f"{int(day)} {MONTHS[month]}, {hour:02}:{minute} {ampm}"


AdmAC=Bot.getData("AdmAC") or []


if str(params) != "None":
    P = str(params).split(" ")

    if P[0] == "page":
        if P[1] == "next":
            AdminCurPage = min(AdminCurPage + 1, TotalPages)
        elif P[1] == "prev":
            AdminCurPage = max(AdminCurPage - 1, 1)
        elif P[1] == "addnew":
            TotalPages = TotalPages + 1
            Bot.saveData("AllMainChPageCount", TotalPages)
            AdminCurPage = TotalPages
        User.saveData("PIRO_AdminCurPage", AdminCurPage)

    if P[0] == "DelSocial":
        SocialLinks = Bot.getData("AllSocialLinks") or []

        try:
            ind = int(P[1])
            SocialLinks.pop(ind)
            Bot.saveData("AllSocialLinks", SocialLinks)
        except:
            pass

    MMM = InlineKeyboardMarkup()
    MMM.add(
        InlineKeyboardButton(
            text='🟢CHECK',
            callback_data=f'/JoinReqSt {P[1]} Y'
        ),
        InlineKeyboardButton(
            text='🔴NO CHECK',
            callback_data=f'/JoinReqSt {P[1]} N'
        )
    )

    DC = Bot.getData(str(P[1]) + "isDef") or "N"

    if P[0]=="setLink":
        
        if DC=="Y" and str(u)!=OWNER:
            bot.replyText(u,"<b>😖This is a Permanent Channel</b>")
            raise ReturnCommand()
        
        bot.replyText(u,"<b>Send Invite Link For Channel\n\nSend /cancel To Cancel\n\nYou Can Also Send Public Channel Link (t.me) Here To Reset If You Have Done Wrong</b>")
        Bot.handleNextCommand("/PIRO_Chanel_Pannel1",options=str(P[1])) 
        raise ReturnCommand()

    if P[0]=="setPage":
        bot.replyText(u,"<b>📄 Send the new page number for this channel (e.g. 1, 2, 3)\n\nSend /cancel To Cancel</b>")
        Bot.handleNextCommand("/PIRO_Chanel_PannelSetPage",options=str(P[1]))
        raise ReturnCommand()

    if P[0]=="delete":
        if DC=="Y" and str(u)!=OWNER:
            bot.replyText(u,"<b>😖This is a Permanent Channel</b>")
            raise ReturnCommand()
        
        try:
            IND=AMCTGID.index(int(P[1]))
        except:
            IND=AMCTGID.index(str(P[1]))
        
        try:
            AMCTGID.pop(IND)
            AMCU.pop(IND)
            AMCL.pop(IND)
            AMC.pop(IND)
            try:
                AMCP.pop(IND)
            except:
                pass
        except:
            pass
        
        act=f"Channel ({P[1]}) Removed "
        AdmAC.append(f"<b>📆 Time:</b> {EasyTime}\n👥 <b>By {message.from_user.first_name}</b> [ID: <code>{u}</code>]\n🔍<b> Action: </b> {act}")
        Bot.saveData("AdmAC",AdmAC)
        
        
        bot.replyText(u,"<b>Channel Removed Successfully</b>")
        
        
    if P[0]=="UP":
        try:
            IND=AMCTGID.index(int(P[1]))
        except:
            IND=AMCTGID.index(str(P[1]))
        
        ThisPg = AMCP[IND]
        SwapIdx = None
        for j in range(IND - 1, -1, -1):
            if AMCP[j] == ThisPg:
                SwapIdx = j
                break
        
        if SwapIdx is not None:
            t=AMCTGID[IND]; AMCTGID[IND]=AMCTGID[SwapIdx]; AMCTGID[SwapIdx]=t
            t=AMCU[IND]; AMCU[IND]=AMCU[SwapIdx]; AMCU[SwapIdx]=t
            t=AMCL[IND]; AMCL[IND]=AMCL[SwapIdx]; AMCL[SwapIdx]=t
            t=AMC[IND]; AMC[IND]=AMC[SwapIdx]; AMC[SwapIdx]=t
        
    if P[0]=="DOWN":
        try:
            IND=AMCTGID.index(int(P[1]))
        except:
            IND=AMCTGID.index(str(P[1]))
        
        ThisPg = AMCP[IND]
        SwapIdx = None
        for j in range(IND + 1, len(AMCTGID)):
            if AMCP[j] == ThisPg:
                SwapIdx = j
                break
        
        if SwapIdx is not None:
            t=AMCTGID[IND]; AMCTGID[IND]=AMCTGID[SwapIdx]; AMCTGID[SwapIdx]=t
            t=AMCU[IND]; AMCU[IND]=AMCU[SwapIdx]; AMCU[SwapIdx]=t
            t=AMCL[IND]; AMCL[IND]=AMCL[SwapIdx]; AMCL[SwapIdx]=t
            t=AMC[IND]; AMC[IND]=AMC[SwapIdx]; AMC[SwapIdx]=t
    
    if P[0]=="info":
       
        try:
            IND=AMCTGID.index(int(P[1]))
        except:
            IND=AMCTGID.index(str(P[1]))
        
        L=AMCL[IND]
        if str(L)=="https://t.me/"+str(AMCTGID[IND]):
            L=" none "
        
        if DC=="Y":
            bot.replyText(u, f"<b>💡Channel Info\n\n🆔Chat ID/Chat Username :- {AMCU[IND]}\n📄Page :- {AMCP[IND]}\n🖇Private Chat Link :- {L}</b>", disable_web_page_preview=True,reply_markup=MMM)
        else:
            bot.replyText(u, f"<b>💡Channel Info\n\n🆔Chat ID/Chat Username :- {AMCU[IND]}\n📄Page :- {AMCP[IND]}\n🖇Private Chat Link :- {L}</b>", disable_web_page_preview=True)
            
        raise ReturnCommand()


    Bot.saveData("AllMainCh",AMC) 
    Bot.saveData("AllMainChlink",AMCL) 
    Bot.saveData("AllMainChUsernm",AMCU) 
    Bot.saveData("AllMainChTGID",AMCTGID) 
    Bot.saveData("AllMainChPage",AMCP) 


# Save which page admin is currently viewing (so newly added channels attach here)
User.saveData("PIRO_AdminCurPage", AdminCurPage)

keyboard = []
IND=0
for ch_tgid in AMCTGID:
    if AMCP[IND] != AdminCurPage:
        IND = IND + 1
        continue
    
    ch=str(AMCU[IND]) 
    
    keyboard.append([
    {"text": ch, "callback_data": "/PIRO_Chanel_Pannel setLink "+str(ch_tgid)},
    {"text": "❌", "callback_data": "/PIRO_Chanel_Pannel delete "+str(ch_tgid)},
    {"text": "🔼", "callback_data": "/PIRO_Chanel_Pannel UP "+str(ch_tgid)},
    {"text": "🔽", "callback_data": "/PIRO_Chanel_Pannel DOWN "+str(ch_tgid)},
    {"text": "👀", "callback_data": "/PIRO_Chanel_Pannel info "+str(ch_tgid)}])

    IND=IND+1

# Page navigation row
NavRow = []
if AdminCurPage > 1:
    NavRow.append({"text": "◀ Prev", "callback_data": "/PIRO_Chanel_Pannel page prev"})
else:
    NavRow.append({"text": "▪️", "callback_data": "/PIRO_Chanel_Pannel noop"})

NavRow.append({"text": f"📄 Page {AdminCurPage}/{TotalPages}", "callback_data": "/PIRO_Chanel_Pannel noop"})

if AdminCurPage < TotalPages:
    NavRow.append({"text": "Next ▶", "callback_data": "/PIRO_Chanel_Pannel page next"})
else:
    NavRow.append({"text": "➕ New Page", "callback_data": "/PIRO_Chanel_Pannel page addnew"})

keyboard.append(NavRow)

keyboard.append([{"text": f"➕Add Channel To Page {AdminCurPage}", "callback_data": "/PIRO_AddCh"}])

keyboard.append([
    {"text": "✏️ Edit Join Message", "callback_data": "/PIRO_SetJoinMsg"}
])
keyboard.append([
    {"text": "✏️ Edit Extra Message (below join)", "callback_data": "/PIRO_SetJoinMsg2"}
])
keyboard.append([
    {"text": "🔘 Join Button (Text + Emoji)", "callback_data": "/PIRO_SetJoinBtnTxt"}
])
keyboard.append([
    {"text": "🔘 Claim Button (Text + Emoji)", "callback_data": "/PIRO_SetClaimBtnTxt"}
])

keyboard.append([{"text": "📡 Channel Broadcast", "callback_data": "/CHANNEL_BC"}])

keyboard.append([
    {"text": "➕ Add Social Link", "callback_data": "/social"}
])

SocialLinks = Bot.getData("AllSocialLinks") or []

for i, item in enumerate(SocialLinks):
    keyboard.append([
        {"text": "🌐 " + item["name"], "url": item["url"]},
        {"text": "❌", "callback_data": "/PIRO_Chanel_Pannel DelSocial " + str(i)}
    ])

keyboard.append([{"text": "🔙Back", "callback_data": "/admin AP"}])


TXT=f"""<b>Here You Can Manage Your Channels\n\nViewing Page {AdminCurPage} of {TotalPages}. Use ◀ Prev / Next ▶ to switch pages.\nAny channel you add now will be added to Page {AdminCurPage}.\nClick a channel name to set its invite link.</b>"""


try:
    bot.editMessageText(chat_id=u, message_id=message.message_id,text=TXT,reply_markup={"inline_keyboard": keyboard})
except:
    try:
        bot.sendMessage(TXT, reply_markup={"inline_keyboard": keyboard})
    except:
        pass''',
    '/PIRO_Chanel_Pannel1': r'''AllBotAdminss = Bot.getData("AllBotAdminss") or []

is_Admin = False
for userid in AllBotAdminss:
    if str(u) == str(userid):
        is_Admin = True

if is_Admin != True:
    bot.replyText(u, "<b><i>🚫You Are Not This Bot Admin</i></b>")
    raise ReturnCommand()



if str(message.text)=="/cancel":
    bot.sendMessage("canceled")
    raise ReturnCommand()
    
    
AMC = Bot.getData("AllMainCh") or []
AMCL=Bot.getData("AllMainChlink") or []
AMCU=Bot.getData("AllMainChUsernm") or []

AMCTGID=Bot.getData("AllMainChTGID") or []
        

try:
    IND=AMCTGID.index(int(options))
except:
    IND=AMCTGID.index(str(options))
    
      
   
   
   
 
now = libs.DateAndTime.now("Asia/Kolkata")
date = now["date"]  
time = now["time"][:5] 
year, month, day = date.split("-")
hour, minute = time.split(":")
hour = int(hour)
ampm = "am"
if hour >= 12:
    ampm = "pm"
if hour > 12:
    hour -= 12
if hour == 0:
    hour = 12

MONTHS = {"01": "Jan", "02": "Feb", "03": "Mar", "04": "Apr","05": "May", "06": "Jun", "07": "Jul", "08": "Aug","09": "Sep", "10": "Oct", "11": "Nov", "12": "Dec"}

EasyTime = f"{int(day)} {MONTHS[month]}, {hour:02}:{minute} {ampm}"


AdmAC=Bot.getData("AdmAC") or []

act=f"Invite Link ({AMCL[IND]}) changed to ({message.text}) in Bot Channel ({AMCTGID[IND]})"
AdmAC.append(f"<b>📆 Time:</b> {EasyTime}\n👥 <b>By {message.from_user.first_name}</b> [ID: <code>{u}</code>]\n🔍<b> Action: </b> {act}")

Bot.saveData("AdmAC",AdmAC)
  
   
   
   
   
   
   
   
AMCL[IND]=str(message.text) 
Bot.saveData("AllMainChlink",AMCL) 
    
   
bot.replyText(u, f"<b> Invite Link Updated for {AMCU[IND]} {AMCTGID[IND]} </b>", disable_web_page_preview=True)

Bot.runCommand("/PIRO_Chanel_Pannel")''',
    '/PIRO_Chanel_PannelSetPage': r'''AllBotAdminss = Bot.getData("AllBotAdminss") or []
is_Admin = str(u) in map(str, AllBotAdminss)

if not is_Admin:
    bot.replyText(u, "<b><i>🚫 You Are Not This Bot Admin</i></b>")
    raise ReturnCommand()

if str(message.text) == "/cancel":
    bot.sendMessage("<b>Cancelled</b>")
    raise ReturnCommand()

PageTxt = str(message.text).strip()
if not PageTxt.isdigit() or int(PageTxt) < 1:
    bot.sendMessage("<b>⚠️ Please send a valid whole number page (1 or more). Try again, or /cancel:</b>")
    Bot.handleNextCommand("/PIRO_Chanel_PannelSetPage")
    raise ReturnCommand()

PageNum = int(PageTxt)

User.saveData("PIRO_AdmViewPage", PageNum)

bot.replyText(u, f"<b>✅ Now Viewing Page {PageNum}</b>")
Bot.runCommand("/PIRO_Chanel_Pannel")''',
    '/PIRO_DebugPages': r'''AllBotAdminss = Bot.getData("AllBotAdminss") or []
is_Admin = str(u) in map(str, AllBotAdminss)
if not is_Admin:
    bot.replyText(u, "<b>🚫 Not Admin</b>")
    raise ReturnCommand()

AMC = Bot.getData("AllMainCh") or []
AMCU = Bot.getData("AllMainChUsernm") or []
AMCL = Bot.getData("AllMainChlink") or []
AMCTGID = Bot.getData("AllMainChTGID") or []
AMCP = Bot.getData("AllMainChPage") or []
for _pad in range(len(AMCTGID) - len(AMCP)):
    AMCP.append(1)

Lines = [f"Total channels: {len(AMCTGID)}"]
IND = 0
for i in AMCTGID:
    try:
        UC = bot.getChatMember(i, u).status
    except Exception as e:
        UC = f"error:{str(e)[:60]}"
    Lines.append(f"{IND}: id={i} ({type(i).__name__}) user={AMCU[IND] if IND<len(AMCU) else '?'} page={AMCP[IND]} my_status={UC}")
    IND += 1

bot.sendMessage("<pre>" + "\n".join(Lines) + "</pre>")''',
    '/PIRO_JoinGate': r'''EMOJI_FAILED = "6129840374971112593"
EMOJI_PIN = "6129434968713076807"
EMOJI_SUCCESS = "6082398290773546707"
EMOJI_HEART = "5388790256772331442"
# Shared page-wise join gate (FAST: cached 1 minute; old join message only deleted on Joined tap).
# Caller stores the target command in User data "PIRO_GateTarget".

if message.chat.type != "private":
    raise ReturnCommand()

Target = User.getData("PIRO_GateTarget") or "/PIRO_MainMenu"

usr = f"""<a href="tg://user?id={str(u)}">{message.from_user.first_name}</a>"""

BotAdmins = Bot.getData("AllBotAdminss") or []
BOTID = int(Bot.info().token.split(":")[0])

AMC = Bot.getData("AllMainCh") or []
AMCU = Bot.getData("AllMainChUsernm") or []
AMCL = Bot.getData("AllMainChlink") or []
AMCTGID = Bot.getData("AllMainChTGID") or []
AMCO = Bot.getData("AllMainChOptional") or []
AMCP = Bot.getData("AllMainChPage") or []
for _pad in range(len(AMCTGID) - len(AMCP)):
    AMCP.append(1)
for _pad in range(len(AMCTGID) - len(AMCU)):
    AMCU.append("Unknown")
for _pad in range(len(AMCTGID) - len(AMCL)):
    AMCL.append("https://t.me/" + str(AMCTGID[len(AMCL)]))

ALL_PAGES = sorted(set(AMCP)) if AMCTGID else []
CurPage = ALL_PAGES[0] if ALL_PAGES else 1

# ---- verified-channel cache (valid for the current MINUTE only) ----
_now = libs.DateAndTime.now("Asia/Kolkata")
Stamp = str(_now["date"]) + " " + str(_now["time"])[:5]
OKids = []
try:
    _c = User.getData("PIRO_OK")
    if _c and _c.get("s") == Stamp:
        OKids = list(_c.get("ids") or [])
except:
    OKids = []

joinSTAT = "JOIND"
PIRO_NJCL = []
FoundIdx = None
CacheDirty = False

for PageIdx in range(0, len(ALL_PAGES)):
    ThisPage = ALL_PAGES[PageIdx]
    NotJoinedThisPage = []

    IND = 0
    for i in AMCTGID:
        if AMCP[IND] != ThisPage:
            IND += 1
            continue

        sid = str(i)
        if sid in OKids:
            IND += 1
            continue

        UC = "error"
        ERR = ""
        try:
            UC = bot.getChatMember(i, u).status
        except Exception as e:
            UC = "error"
            ERR = str(e)[:150]

        if UC == "member" or UC == "administrator" or UC == "creator":
            OKids.append(sid)
            CacheDirty = True

        elif UC == "left" or UC == "kicked":
            ReqRec = Bot.getData(sid + "JoinRRof" + str(u)) or "N"
            JR = Bot.getData(sid + "JoinReq") or "Y"
            if ReqRec != "Y" and JR == "Y" and (not AMCO or IND >= len(AMCO) or AMCO[IND] != "O"):
                NotJoinedThisPage.append(sid)

        elif UC == "error":
            BotIsAdm = False
            try:
                BS = bot.getChatMember(i, BOTID).status
                if BS == "administrator" or BS == "creator":
                    BotIsAdm = True
            except:
                BotIsAdm = False

            if not BotIsAdm:
                if Bot.getData("PIRO_NAsent" + sid) != "Y":
                    Bot.saveData("PIRO_NAsent" + sid, "Y")
                    for admin in BotAdmins:
                        try:
                            bot.replyText(admin, f"<b>👀 Bot Is Not Admin In {AMCU[IND]}! ⚠️\n\n✨ Only admins see this reminder (sent once per channel). 😊</b>")
                        except:
                            pass
            else:
                Bot.deleteData("PIRO_NAsent" + sid)
                if Bot.getData("PIRO_ERRsent" + sid) != "Y":
                    Bot.saveData("PIRO_ERRsent" + sid, "Y")
                    for admin in BotAdmins:
                        try:
                            bot.replyText(admin, f"<b>ℹ️ Bot IS admin in {AMCU[IND]}, but a member check failed:</b>\n<code>{ERR}</code>\n<i>(sent once per channel, only admins see this)</i>")
                        except:
                            pass

        IND += 1

    if NotJoinedThisPage:
        joinSTAT = "NOTJOIN"
        PIRO_NJCL = NotJoinedThisPage
        FoundIdx = PageIdx
        break

if CacheDirty:
    User.saveData("PIRO_OK", {"s": Stamp, "ids": OKids})

if FoundIdx is not None:
    CurPage = ALL_PAGES[FoundIdx]
elif ALL_PAGES:
    CurPage = ALL_PAGES[-1]

User.saveData("PIRO_CurPage", CurPage)
User.saveData("PIRO_NJCL", PIRO_NJCL)

if joinSTAT == "JOIND":
    Bot.runCommand(Target)
    raise ReturnCommand()

# NOTE: old join message is intentionally NOT deleted here anymore.
# It is only deleted when the user actually taps the Joined button (faster gate).

JoinBtnText = Bot.getData("PIRO_JoinBtnText") or "Join"
ClaimBtnText = Bot.getData("PIRO_ClaimBtnText") or "🔒 Claim"
JoinIcon = Bot.getData("PIRO_JoinBtnEmojiId")
ClaimIcon = Bot.getData("PIRO_ClaimBtnEmojiId")

keyboard = []
row = []
IND = 0
for i in AMCTGID:
    if str(i) in PIRO_NJCL:
        btn = {"text": JoinBtnText, "url": AMCL[IND], "style": "primary"}
        if JoinIcon:
            btn["icon_custom_emoji_id"] = JoinIcon
        row.append(btn)
        if len(row) == 2:
            keyboard.append(row)
            row = []
    IND += 1
if row:
    keyboard.append(row)

SocialLinks = Bot.getData("AllSocialLinks") or []
if SocialLinks:
    row = []
    for s in SocialLinks:
        row.append({"text": f"{s['name']}", "url": s['url'], "style": "primary"})
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)

cbtn = {"text": ClaimBtnText, "callback_data": "🟢 Joined", "style": "success"}
if ClaimIcon:
    cbtn["icon_custom_emoji_id"] = ClaimIcon
keyboard.append([cbtn])

JoinMsgChatId = Bot.getData("PIRO_JoinMsgChatId")
JoinMsgId = Bot.getData("PIRO_JoinMsgId")

SentOk = False
if JoinMsgChatId and JoinMsgId:
    try:
        Sent = bot.copyMessage(chat_id=u, from_chat_id=JoinMsgChatId, message_id=JoinMsgId, reply_markup={"inline_keyboard": keyboard})
        try:
            Mid = Sent.message_id
        except:
            Mid = Sent["message_id"]
        User.saveData("PIRO_ltmg", Mid)
        SentOk = True
    except:
        SentOk = False

if not SentOk:
    msg = bot.sendMessage(
    f"""<tg-emoji emoji-id="{EMOJI_HEART}">👋🏻</tg-emoji><b>Hey There {usr} Welcome To Wallet Bot !

<tg-emoji emoji-id="{EMOJI_PIN}">🛑</tg-emoji> Must Join All Channels To Use Our Bot

<tg-emoji emoji-id="{EMOJI_SUCCESS}">🚫</tg-emoji> After Joining Click Claim</b>""",
        reply_markup={"inline_keyboard": keyboard},
        disable_web_page_preview=True
    )
    User.saveData("PIRO_ltmg", msg["message_id"])

Ex1 = Bot.getData("PIRO_JoinMsg2ChatId")
Ex2 = Bot.getData("PIRO_JoinMsg2Id")
if Ex1 and Ex2:
    try:
        S2 = bot.copyMessage(chat_id=u, from_chat_id=Ex1, message_id=Ex2)
        try:
            M2 = S2.message_id
        except:
            M2 = S2["message_id"]
        User.saveData("PIRO_ltmg2", M2)
    except:
        pass''',
    '/PIRO_MainMenu': r'''EMOJI_PARTY = "6224161941305169199"
EMOJI_STAR = "5469741319330996757"
EMOJI_FAIL = "6129840374971112593"
EMOJI_GIFT = "5193085063998224234"
EMOJI_MONEY = "6089081350080434641"
EMOJI_TROPHY = "6156436440260549720"
EMOJI_TIE = "5375203677487248777"
EMOJI_GUN = "6129488844782836766"
EMOJI_WARN = "5447644880824181073"
EMOJI_PAISA = "6287207173537666597"
EMOJI_CLOCK = "6231214251835397322"
EMOJI_POTL = "5375296873982604963"
EMOJI_SEARCH = "6195146353434169614"
EMOJI_DONE = "5021905410089550576"
EMOJI_SUCCESS = "6082398290773546707"
EMOJI_CROWN = "4956420911310832630"
EMOJI_POTLI = "6278489025581944577"
EMOJI_MST = "6278337731063975777"
EMOJI_BANK = "5332455502917949981"


# --- Referral & First-time check ---
dn = User.getData("dn") or "n"
if dn == "n":
    refBy = Bot.getData(str(u) + "Referral") or "None"
    RefJC = Bot.getData(str(refBy) + "RefJC") or 0
    Bot.saveData(str(refBy) + "RefJC", RefJC + 1)
    User.saveData("dn", "y")
# --- TBC Rewarded Ad: Show Only Once ---

# ============================================================
# 📱 CONTACT VERIFICATION FIRST
# ============================================================

VerifySystemStatus = Bot.getData(
    "VerifySystemStatus"
) or "OFF"


if VerifySystemStatus == "ON":

    if not User.getData("is_verified"):

        Bot.runCommand(
            "/shareprofile"
        )

        raise ReturnCommand()


# ============================================================
# 🔐 CAPTCHA + MANUAL APPROVAL VERIFICATION
# ============================================================

iisApproved = Bot.getData(
    str(u) + "ManualAproval"
) or "N"

UsrPhVerif = User.getData(
    "PhVerification"
)


Captcha = Bot.getData(
    "CaptchaMode"
) or "ON"


verify = User.getData(
    "verify"
)


if Captcha == "ON":

    if verify != "ok":

        # ✅ Phone verified OR manually approved = allow
        if UsrPhVerif == "Verified" or iisApproved == "Y":
            pass

        else:
            Bot.runCommand(
                "/PIRO_Verification"
            )

            raise ReturnCommand()
# --- Menu Function ---
def PIRO_Menu():
    # --- Get saved link ---
    saved_link = Bot.getData("SavedLinks")

    # --- Get owner ID (save once with: Bot.saveData("Owner", 123456789)) ---
    owner_id = Bot.getData("Owner")

    # --- Decide final link ---
    if saved_link:
        final_link = saved_link
    elif owner_id:
        final_link = f"tg://openmessage?user_id={owner_id}"
    else:
        final_link = "https://t.me/"   # fallback if nothing set

    # --- Welcome text ---
    HHT = (
        f'<tg-emoji emoji-id="{EMOJI_STAR
        }">🏡</tg-emoji><b> Welcome To Yono App Bot!</b>')
        
    if options:   # override if a custom message is passed
        HHT = options

    # --- Keyboard menu ---
    keyboard = {
    "keyboard": [
        [
            {
                "text": "Yono 777",
                "icon_custom_emoji_id": 5217822164362739968
            },
            {
                "text": "Lucky Gullak",
                "icon_custom_emoji_id": 5461117441612462242
            }
        ],
        [
            {
                "text": "Jaiho 777",
                "icon_custom_emoji_id": 5438496463044752972
            }
        ]
    ],
    "resize_keyboard": True
}
    bot.sendMessage(
        HHT,
        chat_id=message.chat.id,
        reply_markup=keyboard,
        parse_mode="HTML",
        disable_web_page_preview=True
    )
# --- TBC Rewarded Ad: Show Only Once ---

# --- Show Menu ---
PIRO_Menu()

# --- Referral Logic ---
is_invited = User.getData("is_invited")
ref = Bot.getData(str(u) + "Referral") or "None"
perRef = Bot.getData("PerRefer") or "0"   # string format support

if is_invited is None:
    if ref != "None":
        sameDev = Bot.getData(str(u) + "sameDev") or "no"
        if sameDev == "no":
            RefCount = Bot.getData(str(ref) + "RefCount") or 0
            Bot.saveData(str(ref) + "RefCount", int(RefCount + 1))

            usr = f"""<a href="tg://user?id={str(u)}">{str(u)}</a>"""

            # --- Decide Referral Bonus ---
            bonus = 0
            if "-" in str(perRef):
                try:
                    start, end = map(int, perRef.split("-"))
                    # ✅ Built-in random (no import)
                    bonus = libs.Random.randomInt(start,end)
                except:
                    bonus = 0
            else:
                try:
                    bonus = int(perRef)
                except:
                    bonus = 0

            # --- Add bonus to referrer ---
            if bonus > -1:
                libs.Resources.anotherRes('Balance', user=ref).add(bonus)
                try:
                    bot.replyText(
                        chat_id=ref,
                        text=f"""<tg-emoji emoji-id="{EMOJI_PARTY}">🤑</tg-emoji><b>{usr} Got Invited By Your Url: +{bonus} Rs </b>""",
                        parse_mode="HTML"
                    )
                except:
                    pass

    # --- Count verified users ---
    T_verifUsrs = Bot.getData('T_verifUsrs') or 0
    Bot.saveData('T_verifUsrs', T_verifUsrs + 1)

    User.saveData("is_invited", True)''',
    '/PIRO_PayoutMenu': r'''EMOJI_FAILED = "6129840374971112593"
EMOJI_DOWN = "5470177992950946662"
EMOJI_TI = "5375152498656961898"

# ----------- WALLET / UPI MENU (opened only after the join gate passes) -----------
# coded by @Jenish_Dobariya1

wallet = Bot.getData("UserWallet" + str(u)) or "Not Linked"
upi = Bot.getData("UserUPI" + str(u)) or "Not Linked"

WalletStatus = Bot.getData("WalletWithdraw") or "ON"
UpiStatus = Bot.getData("UpiWithdraw") or "ON"

if WalletStatus == "OFF" and UpiStatus == "OFF":
    bot.replyText(u, f'<b><tg-emoji emoji-id="{EMOJI_FAILED}">⛔</tg-emoji>All Withdrawal Methods Are Currently Disabled</b>')
    raise ReturnCommand()

text = f'<b><tg-emoji emoji-id="{EMOJI_TI}">💳</tg-emoji> Choose Desired Payment Method From Below <tg-emoji emoji-id="{EMOJI_DOWN}">👇</tg-emoji>\n\n'
buttons = []
row = []

if WalletStatus == "ON":
    text += f"Your Current Wallet - <code>{wallet}</code>\n"
    row.append({"text": "Link Wallet", "callback_data": "Link Wallet"})

if UpiStatus == "ON":
    text += f"Your Current UPI - <code>{upi}</code>\n"
    row.append({"text": "Link UPI", "callback_data": "link_upi"})

text += "</b>"
if row:
    buttons.append(row)

bot.replyText(
    u,
    text,
    parse_mode="html",
    reply_markup={"inline_keyboard": buttons}
)''',
    '/PIRO_ScheduleBroadcast': r'''# Admin Check
# Admin Check
AllBotAdminss = Bot.getData("AllBotAdminss") or []
is_Admin = False
for userid in AllBotAdminss:
    if str(u) == str(userid):
        is_Admin = True
if is_Admin != True:
    bot.replyText(u, "<b><i>🚫You Are Not This Bot Admin</i></b>")
    raise ReturnCommand()

AMCTGID = Bot.getData("AllMainChTGID") or []
if not AMCTGID:
    bot.replyText(u, "<b>❌ No Channels Added Yet!</b>")
    raise ReturnCommand()

# Ask for date
msg = bot.replyText(u, f"""<b>📅 Schedule Broadcast to {len(AMCTGID)} Channels</b>

<i>Send the date for broadcast</i>
<b>Format:</b> DD-MM-YYYY
<b>Example:</b> 25-12-2025

<i>Send /cancel to cancel</i>""")

Bot.saveData("BC_MsgID", msg.message_id)
Bot.handleNextCommand("/PIRO_BC_Schedule_Date", options=True)''',
    '/PIRO_SetClaimBtnEmoji': r'''AllBotAdminss = Bot.getData("AllBotAdminss") or []
is_Admin = str(u) in map(str, AllBotAdminss)
if not is_Admin:
    bot.replyText(u, "<b>🚫 You Are Not This Bot Admin</b>")
    raise ReturnCommand()

CurId = Bot.getData("PIRO_ClaimBtnEmojiId")

bot.replyText(
    u,
    f"<b>🎭 Current Claim button icon_custom_emoji_id:</b> {CurId or 'not set'}\n\n"
    "<b>Send a message containing ONLY the custom/premium emoji you want as the Claim button's icon "
    "(paste it from Telegram's premium emoji picker). Send /cancel to cancel, /reset_default to remove it.</b>"
)
Bot.handleNextCommand("/PIRO_SetClaimBtnEmojiSave")''',
    '/PIRO_SetClaimBtnEmojiSave': r'''AllBotAdminss = Bot.getData("AllBotAdminss") or []
is_Admin = str(u) in map(str, AllBotAdminss)
if not is_Admin:
    bot.replyText(u, "<b>🚫 You Are Not This Bot Admin</b>")
    raise ReturnCommand()

Txt = str(message.text or "").strip()

if Txt == "/cancel":
    bot.sendMessage("<b>Cancelled</b>")
    raise ReturnCommand()

if Txt == "/reset_default":
    Bot.deleteData("PIRO_ClaimBtnEmojiId")
    bot.sendMessage("<b>♻️ Claim button icon removed.</b>")
    raise ReturnCommand()

FoundId = None
Ents = message.entities or []
for e in Ents:
    if e.type == "custom_emoji":
        FoundId = e.custom_emoji_id
        break

if not FoundId:
    bot.sendMessage("<b>⚠️ I couldn't find a custom/premium emoji in that message. Make sure you pasted an actual premium emoji (not a regular one), then try again, or /cancel.</b>")
    Bot.handleNextCommand("/PIRO_SetClaimBtnEmojiSave")
    raise ReturnCommand()

Bot.saveData("PIRO_ClaimBtnEmojiId", str(FoundId))
bot.sendMessage(f"<b>✅ Claim button icon_custom_emoji_id saved: {FoundId}</b>")''',
    '/PIRO_SetClaimBtnTxt': r'''AllBotAdminss = Bot.getData("AllBotAdminss") or []
is_Admin = str(u) in map(str, AllBotAdminss)
if not is_Admin:
    bot.replyText(u, "<b>🚫 You Are Not This Bot Admin</b>")
    raise ReturnCommand()

CurClaim = Bot.getData("PIRO_ClaimBtnText") or "🔒 Claim"
CurIcon = Bot.getData("PIRO_ClaimBtnEmojiId") or "none"

bot.replyText(
    u,
    f"<b>🔘 Current Claim button:</b> {CurClaim}  (icon id: {CurIcon})\n\n"
    "<b>Send the new Claim button text together with its emoji, in one message.</b>\n"
    "• Premium emoji anywhere in the message = saved automatically as the button icon (shown in front)\n"
    "• Normal emoji written at the end is moved to the front automatically\n"
    "• If there is more than one page, \"(Page X/Y)\" is added automatically\n\n"
    "Send /cancel to cancel, /reset_default to reset."
)
Bot.handleNextCommand("/PIRO_SetClaimBtnTxtSave")''',
    '/PIRO_SetClaimBtnTxtSave': r'''AllBotAdminss = Bot.getData("AllBotAdminss") or []
is_Admin = str(u) in map(str, AllBotAdminss)
if not is_Admin:
    bot.replyText(u, "<b>🚫 You Are Not This Bot Admin</b>")
    raise ReturnCommand()

Full = str(message.text or "")
Txt = Full.strip()

if Txt == "/cancel":
    bot.sendMessage("<b>Cancelled</b>")
    raise ReturnCommand()

if Txt == "/reset_default":
    Bot.deleteData("PIRO_ClaimBtnText")
    Bot.deleteData("PIRO_ClaimBtnEmojiId")
    bot.sendMessage("<b>♻️ Claim button reset to default (🔒 Claim, no icon).</b>")
    raise ReturnCommand()

# 1) Premium emoji -> becomes the button icon (shown in front), removed from text
Icon = None
Cut = []
for e in (message.entities or []):
    if e.type == "custom_emoji":
        if Icon is None:
            Icon = str(e.custom_emoji_id)
        Cut.append((e.offset, e.length))

if Cut:
    Res = ""
    p = 0
    for ch in Full:
        w = 2 if ord(ch) > 65535 else 1
        inside = False
        for (o, l) in Cut:
            if p >= o and p < o + l:
                inside = True
        if not inside:
            Res += ch
        p += w
    Txt = Res.strip()

# 2) Regular emoji written at the END gets moved to the FRONT
tokens = Txt.split()
last = -1
for idx, t in enumerate(tokens):
    if len([c for c in t if c.isalnum()]) > 0:
        last = idx

if last >= 0:
    body = tokens[:last + 1]
    tail = tokens[last + 1:]
    w = body[-1]
    cut = len(w)
    for k in range(len(w) - 1, -1, -1):
        c = w[k]
        if c.isalnum():
            break
        if ord(c) >= 8592 or ord(c) == 8205:
            cut = k
        else:
            break
    glued = w[cut:]
    body[-1] = w[:cut]
    frontparts = [x for x in ([glued] + tail) if x]
    Txt = " ".join(frontparts + body).strip()

if not Txt and Icon:
    Bot.saveData("PIRO_ClaimBtnEmojiId", Icon)
    bot.sendMessage("<b>✅ Claim button icon saved (text unchanged).</b>")
    raise ReturnCommand()

if not Txt or len(Txt) > 30:
    bot.sendMessage("<b>⚠️ Please send text between 1 and 30 characters.</b>")
    Bot.handleNextCommand("/PIRO_SetClaimBtnTxtSave")
    raise ReturnCommand()

Bot.saveData("PIRO_ClaimBtnText", Txt)
IconMsg = ""
if Icon:
    Bot.saveData("PIRO_ClaimBtnEmojiId", Icon)
    IconMsg = f"\n🎭 Premium emoji saved as icon (id {Icon})"

bot.sendMessage(f"<b>✅ Claim button text: {Txt}</b>{IconMsg}")''',
    '/PIRO_SetJoinBtnEmoji': r'''AllBotAdminss = Bot.getData("AllBotAdminss") or []
is_Admin = str(u) in map(str, AllBotAdminss)
if not is_Admin:
    bot.replyText(u, "<b>🚫 You Are Not This Bot Admin</b>")
    raise ReturnCommand()

CurId = Bot.getData("PIRO_JoinBtnEmojiId")

bot.replyText(
    u,
    f"<b>🎭 Current Join button icon_custom_emoji_id:</b> {CurId or 'not set'}\n\n"
    "<b>Send a message containing ONLY the custom/premium emoji you want as the Join button's icon "
    "(paste it from Telegram's premium emoji picker). Send /cancel to cancel, /reset_default to remove it.</b>\n\n"
    "<i>Note: Telegram's official Bot API does not document icon support for buttons, so this may or may not visually show — but I'll wire it in exactly as requested.</i>"
)
Bot.handleNextCommand("/PIRO_SetJoinBtnEmojiSave")''',
    '/PIRO_SetJoinBtnEmojiSave': r'''AllBotAdminss = Bot.getData("AllBotAdminss") or []
is_Admin = str(u) in map(str, AllBotAdminss)
if not is_Admin:
    bot.replyText(u, "<b>🚫 You Are Not This Bot Admin</b>")
    raise ReturnCommand()

Txt = str(message.text or "").strip()

if Txt == "/cancel":
    bot.sendMessage("<b>Cancelled</b>")
    raise ReturnCommand()

if Txt == "/reset_default":
    Bot.deleteData("PIRO_JoinBtnEmojiId")
    bot.sendMessage("<b>♻️ Join button icon removed.</b>")
    raise ReturnCommand()

FoundId = None
Ents = message.entities or []
for e in Ents:
    if e.type == "custom_emoji":
        FoundId = e.custom_emoji_id
        break

if not FoundId:
    bot.sendMessage("<b>⚠️ I couldn't find a custom/premium emoji in that message. Make sure you pasted an actual premium emoji (not a regular one), then try again, or /cancel.</b>")
    Bot.handleNextCommand("/PIRO_SetJoinBtnEmojiSave")
    raise ReturnCommand()

Bot.saveData("PIRO_JoinBtnEmojiId", str(FoundId))
bot.sendMessage(f"<b>✅ Join button icon_custom_emoji_id saved: {FoundId}</b>")''',
    '/PIRO_SetJoinBtnTxt': r'''AllBotAdminss = Bot.getData("AllBotAdminss") or []
is_Admin = str(u) in map(str, AllBotAdminss)
if not is_Admin:
    bot.replyText(u, "<b>🚫 You Are Not This Bot Admin</b>")
    raise ReturnCommand()

CurJoin = Bot.getData("PIRO_JoinBtnText") or "Join"
CurIcon = Bot.getData("PIRO_JoinBtnEmojiId") or "none"

bot.replyText(
    u,
    f"<b>🔘 Current Join button:</b> {CurJoin}  (icon id: {CurIcon})\n\n"
    "<b>Send the new Join button text together with its emoji, in one message.</b>\n"
    "• Premium emoji anywhere in the message = saved automatically as the button icon (shown in front)\n"
    "• Normal emoji written at the end is moved to the front automatically\n\n"
    "Example: <code>Join Now 🔥</code> becomes <code>🔥 Join Now</code>\n"
    "Send /cancel to cancel, /reset_default to reset."
)
Bot.handleNextCommand("/PIRO_SetJoinBtnTxtSave")''',
    '/PIRO_SetJoinBtnTxtSave': r'''AllBotAdminss = Bot.getData("AllBotAdminss") or []
is_Admin = str(u) in map(str, AllBotAdminss)
if not is_Admin:
    bot.replyText(u, "<b>🚫 You Are Not This Bot Admin</b>")
    raise ReturnCommand()

Full = str(message.text or "")
Txt = Full.strip()

if Txt == "/cancel":
    bot.sendMessage("<b>Cancelled</b>")
    raise ReturnCommand()

if Txt == "/reset_default":
    Bot.deleteData("PIRO_JoinBtnText")
    Bot.deleteData("PIRO_JoinBtnEmojiId")
    bot.sendMessage("<b>♻️ Join button reset to default (Join, no icon).</b>")
    raise ReturnCommand()

# 1) Premium emoji -> becomes the button icon (shown in front), removed from text
Icon = None
Cut = []
for e in (message.entities or []):
    if e.type == "custom_emoji":
        if Icon is None:
            Icon = str(e.custom_emoji_id)
        Cut.append((e.offset, e.length))

if Cut:
    Res = ""
    p = 0
    for ch in Full:
        w = 2 if ord(ch) > 65535 else 1
        inside = False
        for (o, l) in Cut:
            if p >= o and p < o + l:
                inside = True
        if not inside:
            Res += ch
        p += w
    Txt = Res.strip()

# 2) Regular emoji written at the END gets moved to the FRONT
tokens = Txt.split()
last = -1
for idx, t in enumerate(tokens):
    if len([c for c in t if c.isalnum()]) > 0:
        last = idx

if last >= 0:
    body = tokens[:last + 1]
    tail = tokens[last + 1:]
    w = body[-1]
    cut = len(w)
    for k in range(len(w) - 1, -1, -1):
        c = w[k]
        if c.isalnum():
            break
        if ord(c) >= 8592 or ord(c) == 8205:
            cut = k
        else:
            break
    glued = w[cut:]
    body[-1] = w[:cut]
    frontparts = [x for x in ([glued] + tail) if x]
    Txt = " ".join(frontparts + body).strip()

if not Txt and Icon:
    Bot.saveData("PIRO_JoinBtnEmojiId", Icon)
    bot.sendMessage("<b>✅ Join button icon saved (text unchanged).</b>")
    raise ReturnCommand()

if not Txt or len(Txt) > 30:
    bot.sendMessage("<b>⚠️ Please send text between 1 and 30 characters.</b>")
    Bot.handleNextCommand("/PIRO_SetJoinBtnTxtSave")
    raise ReturnCommand()

Bot.saveData("PIRO_JoinBtnText", Txt)
IconMsg = ""
if Icon:
    Bot.saveData("PIRO_JoinBtnEmojiId", Icon)
    IconMsg = f"\n🎭 Premium emoji saved as icon (id {Icon})"

bot.sendMessage(f"<b>✅ Join button text: {Txt}</b>{IconMsg}")''',
    '/PIRO_SetJoinMsg': r'''AllBotAdminss = Bot.getData("AllBotAdminss") or []
is_Admin = str(u) in map(str, AllBotAdminss)
if not is_Admin:
    bot.replyText(u, "<b>🚫 You Are Not This Bot Admin</b>")
    raise ReturnCommand()

bot.replyText(
    u,
    "<b>✏️ Send the message you want users to see on the Join screen.\n\n"
    "Format it however you like using Telegram's own formatting (bold, italic, links, emoji) "
    "— no HTML needed. You can also forward a message here to copy it exactly. "
    "Photos/stickers with captions work too.\n\n"
    "Send /cancel to cancel, or /reset_default to go back to the built-in default message.</b>"
)
Bot.handleNextCommand("/PIRO_SetJoinMsgSave")''',
    '/PIRO_SetJoinMsg2': r'''AllBotAdminss = Bot.getData("AllBotAdminss") or []
is_Admin = str(u) in map(str, AllBotAdminss)
if not is_Admin:
    bot.replyText(u, "<b>🚫 You Are Not This Bot Admin</b>")
    raise ReturnCommand()

bot.replyText(
    u,
    "<b>✏️ Send the EXTRA message that should come together with the channel join message.</b>\n\n"
    "Format it normally in Telegram (bold, italic, links, premium emoji, photo with caption etc.) — no HTML needed. "
    "You can also forward a message to copy it exactly.\n\n"
    "It is sent right after the join message, and when the user taps Joined it is deleted and sent again along with the next join message.\n\n"
    "Send /cancel to cancel, or /reset_default to remove the extra message."
)
Bot.handleNextCommand("/PIRO_SetJoinMsg2Save")''',
    '/PIRO_SetJoinMsg2Save': r'''AllBotAdminss = Bot.getData("AllBotAdminss") or []
is_Admin = str(u) in map(str, AllBotAdminss)
if not is_Admin:
    bot.replyText(u, "<b>🚫 You Are Not This Bot Admin</b>")
    raise ReturnCommand()

TxtIn = str(message.text or message.caption or "")

if TxtIn.strip() == "/cancel":
    bot.sendMessage("<b>Cancelled</b>")
    raise ReturnCommand()

if TxtIn.strip() == "/reset_default":
    Bot.deleteData("PIRO_JoinMsg2ChatId")
    Bot.deleteData("PIRO_JoinMsg2Id")
    bot.sendMessage("<b>♻️ Extra message removed.</b>")
    raise ReturnCommand()

Bot.saveData("PIRO_JoinMsg2ChatId", message.chat.id)
Bot.saveData("PIRO_JoinMsg2Id", message.message_id)

bot.sendMessage("<b>✅ Saved! This extra message will come along with the join message:</b>")

try:
    bot.copyMessage(chat_id=u, from_chat_id=message.chat.id, message_id=message.message_id)
except Exception as e:
    bot.sendMessage(f"<b>⚠️ Saved, but couldn't preview it: {str(e)[:150]}</b>")''',
    '/PIRO_SetJoinMsgSave': r'''AllBotAdminss = Bot.getData("AllBotAdminss") or []
is_Admin = str(u) in map(str, AllBotAdminss)
if not is_Admin:
    bot.replyText(u, "<b>🚫 You Are Not This Bot Admin</b>")
    raise ReturnCommand()

TxtIn = str(message.text or message.caption or "")

if TxtIn.strip() == "/cancel":
    bot.sendMessage("<b>Cancelled</b>")
    raise ReturnCommand()

if TxtIn.strip() == "/reset_default":
    Bot.deleteData("PIRO_JoinMsgChatId")
    Bot.deleteData("PIRO_JoinMsgId")
    bot.sendMessage("<b>♻️ Reset — the built-in default Join message will be used again.</b>")
    raise ReturnCommand()

Bot.saveData("PIRO_JoinMsgChatId", message.chat.id)
Bot.saveData("PIRO_JoinMsgId", message.message_id)

bot.sendMessage("<b>✅ Saved! This is exactly how it will look to users (buttons will be added automatically):</b>")

try:
    bot.copyMessage(chat_id=u, from_chat_id=message.chat.id, message_id=message.message_id)
except Exception as e:
    bot.sendMessage(f"<b>⚠️ Saved, but couldn't preview it: {str(e)[:150]}</b>")''',
    '/PIRO_Verification': r'''EMOJI_PARTY = "6224161941305169199"
EMOJI_STAR = "5469741319330996757"
EMOJI_FAIL = "6129840374971112593"
EMOJI_GIFT = "5193085063998224234"
EMOJI_MONEY = "6089081350080434641"
EMOJI_TROPHY = "6156436440260549720"
EMOJI_TIE = "5375203677487248777"
EMOJI_GUN = "6129488844782836766"
EMOJI_WARN = "5447644880824181073"
EMOJI_PAISA = "6287207173537666597"
EMOJI_CLOCK = "6231214251835397322"
EMOJI_POTL = "5375296873982604963"
EMOJI_SEARCH = "6195146353434169614"
EMOJI_DONE = "5021905410089550576"
EMOJI_SUCCESS = "6082398290773546707"
EMOJI_CROWN = "4956420911310832630"
EMOJI_POTLI = "6278489025581944577"
EMOJI_MST = "6278337731063975777"
EMOJI_BANK = "5332455502917949981"

EMOJI_FAILED = EMOJI_FAIL
EMOJI_SECURE = "5197288647275071607"
EMOJI_BULLET = "5458603043203327669"


BotMode = Bot.getData("BotMode")
if BotMode == "OFF":
    bot.replyText(u, f'<tg-emoji emoji-id="{EMOJI_FAILED}">🚫</tg-emoji><b> Bot Is Currently Off</b>')
# --- TBC Rewarded Ad: Show Only Once ---

Captcha = Bot.getData("CaptchaMode") or "ON"

if Captcha == "OFF":

    # ================= REFERRAL CREDIT =================
    User.deleteData("verify")   # ✅ Yeh line important hai

    # Ab menu dikhao
    saved_link = Bot.getData("SavedLinks")
    owner_id = Bot.getData("Owner")
    if saved_link:
        final_link = saved_link
    elif owner_id:
        final_link = f"tg://openmessage?user_id={owner_id}"
    else:
        final_link = "https://t.me/"

    HHT = (
        f'<tg-emoji emoji-id="{EMOJI_STAR
        }">🏡</tg-emoji><b> Welcome To Yono App Bot!</b>')

    keyboard = {
    "keyboard": [
        [
            {
                "text": "Yono 777",
                "icon_custom_emoji_id": 5217822164362739968
            },
            {
                "text": "Lucky Gullak",
                "icon_custom_emoji_id": 5461117441612462242
            }
        ],
        [
            {
                "text": "Jaiho 777",
                "icon_custom_emoji_id": 5438496463044752972
            }
        ]
    ],
    "resize_keyboard": True
}

    bot.sendMessage(
        HHT,
        chat_id=message.chat.id,
        reply_markup=keyboard,
        parse_mode="HTML",
        disable_web_page_preview=True
    )

    raise ReturnCommand()

# ================= VERIFY STATUS =================
verify = User.getData("verify")

if verify == "ok":
    # Agar verify "ok" hai toh menu dikhao
    saved_link = Bot.getData("SavedLinks")
    owner_id = Bot.getData("Owner")
    if saved_link:
        final_link = saved_link
    elif owner_id:
        final_link = f"tg://openmessage?user_id={owner_id}"
    else:
        final_link = "https://t.me/"

    HHT = (
        f'<tg-emoji emoji-id="{EMOJI_STAR
        }">🏡</tg-emoji><b> Welcome To Yono App Bot!</b>')
    keyboard = {
    "keyboard": [
        [
            {
                "text": "Yono 777",
                "icon_custom_emoji_id": 5217822164362739968
            },
            {
                "text": "Lucky Gullak",
                "icon_custom_emoji_id": 5461117441612462242
            }
        ],
        [
            {
                "text": "Jaiho 777",
                "icon_custom_emoji_id": 5438496463044752972
            }
        ]
    ],
    "resize_keyboard": True
}

    bot.sendMessage(
        HHT,
        chat_id=message.chat.id,
        reply_markup=keyboard,
        parse_mode="HTML",
        disable_web_page_preview=True
    )

    raise ReturnCommand()




# ================= HASH GENERATE =================
random_hash = User.getData(
    "user_hash"
)

if random_hash is None:

    chars = (
        "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        "abcdefghijklmnopqrstuvwxyz"
        "0123456789"
    )

    random_hash = ""

    for i in range(32):

        random_hash += libs.Random.randomWeightedChoice(
            chars,
            [1] * len(chars)
        )

    User.saveData(
        "user_hash",
        random_hash
    )


# ================= BOT USERNAME =================
me = HTTP.get(
    "https://api.telegram.org/bot"
    + bot_token +
    "/getMe"
).json()

BOT_USERNAME = me["result"]["username"]


# ================= WEBHOOK URL =================
webhook = libs.Webhook.getUrlFor(
    command="/onWebhook",
    user_id=u
)

webhook_encoded = rawurlencode(
    webhook
)


# ================= WEBAPP URL =================
WEBAPP_URL = (
    "https://verification-page-six.vercel.app/"
    f"?botusername={BOT_USERNAME}"
    f"&webhook={webhook_encoded}"
    f"&hash={random_hash}"
)

# ================= SEND VERIFY =================
EMOJI_BULLET = "5458603043203327669"
EMOJI_SUCCESS = "6082398290773546707"
EMOJI_TELEGRAM = "6082461220634368541"

bot.sendMessage(
    f"""<tg-emoji emoji-id="{EMOJI_SECURE}">🔐</tg-emoji><b>Verify Your Self To Continue Bot.</b>
""",

    parse_mode="HTML",

    reply_markup={
        "inline_keyboard": [
            [
                {
                    "text": "Verify Now",
                    "web_app": {
                        "url": WEBAPP_URL
                    },
                    "style": "primary",
                    "icon_custom_emoji_id": EMOJI_BULLET
                }
            ]
        ]
    }
)

raise ReturnCommand()''',
    '/PIRO_Verification0': r'''# 19'4'25   00'11'10  v:1'0'0 [√]
# CODED BY: @Owner_Here 
# [ DM TO BUY ANY BOTS & CODES ]

BotMode=Bot.getData("BotMode")
if BotMode=="OFF":
    bot.replyText(u, "<b>🙇‍♂️ Bot Is Currently Off</b>")
    raise ReturnCommand()


verify = User.getData("verifyYYY")
if verify:
  bot.sendMessage("🙄Already verified")
  raise ReturnCommand()

webhook = libs.Webhook.getUrlFor(command="/onWebhook", user_id=u)

BOTID = str(bot.info().token).split(":")[0]

webPage="https://api.jobians.top/telegram/verify?webhookUrl="+str(encodeURIComponent(webhook))+"&botId="+BOTID

keys = [[InlineKeyboardButton(text='Verify', web_app=webPage)]]
button = InlineKeyboardMarkup(keys)

bot.sendMessage("<b>🔐 Verify Yourself To Start Bot</b>", reply_markup=button)''',
    '/PIRO_broadcast1': r'''# 19'4'25 00'11'10 v:1'0'0 [√]
# Premium Entity Broadcast

AllBotAdminss = Bot.getData("AllBotAdminss") or []

is_Admin = False
for userid in AllBotAdminss:
    if str(u) == str(userid):
        is_Admin = True
        break

if not is_Admin:
    bot.replyText(u, "<b><i>🚫 You Are Not This Bot Admin</i></b>", parse_mode="html")
    raise ReturnCommand()

if message.text == "/cancel":
    bot.sendMessage("<b>❌ Cancelled</b>", parse_mode="html")
    raise ReturnCommand()

broadcast_data = None

is_forwarded = False
try:
    if message.forward_from or message.forward_from_chat or message.forward_date:
        is_forwarded = True
except:
    is_forwarded = False

if is_forwarded:
    broadcast_data = {
        "function": "forward_message",
        "from_chat_id": message.chat.id,
        "message_id": message.message_id
    }

elif message.photo:
    broadcast_data = {
        "function": "send_photo",
        "photo": message.photo[-1].file_id
    }

    if message.caption:
        broadcast_data["caption"] = message.caption
        try:
            if message.caption_entities:
                broadcast_data["caption_entities"] = message.caption_entities
        except:
            pass

elif message.video:
    broadcast_data = {
        "function": "send_video",
        "video": message.video.file_id
    }

    if message.caption:
        broadcast_data["caption"] = message.caption
        try:
            if message.caption_entities:
                broadcast_data["caption_entities"] = message.caption_entities
        except:
            pass

elif message.document:
    broadcast_data = {
        "function": "send_document",
        "document": message.document.file_id
    }

    if message.caption:
        broadcast_data["caption"] = message.caption
        try:
            if message.caption_entities:
                broadcast_data["caption_entities"] = message.caption_entities
        except:
            pass

elif message.audio:
    broadcast_data = {
        "function": "send_audio",
        "audio": message.audio.file_id
    }

    try:
        if message.caption:
            broadcast_data["caption"] = message.caption
            if message.caption_entities:
                broadcast_data["caption_entities"] = message.caption_entities
    except:
        pass

elif message.sticker:
    broadcast_data = {
        "function": "send_sticker",
        "sticker": message.sticker.file_id
    }

elif message.animation:
    broadcast_data = {
        "function": "send_animation",
        "animation": message.animation.file_id
    }

    try:
        if message.caption:
            broadcast_data["caption"] = message.caption
            if message.caption_entities:
                broadcast_data["caption_entities"] = message.caption_entities
    except:
        pass

elif message.text:
    broadcast_data = {
        "function": "send_message",
        "text": message.text
    }

    try:
        if message.entities:
            broadcast_data["entities"] = message.entities
    except:
        pass

else:
    bot.sendMessage("<b>❌ Unsupported File Format!</b>", parse_mode="html")
    raise ReturnCommand()

User.saveData("broadcast_data", broadcast_data)

FulUsrs = Bot.getData("FulBotUsrs") or []

keyboard = [
    [{"text": "✅ Confirm", "callback_data": "/PIRO_broadcast2 CONFIRM"}],
    [{"text": "❌ Cancel", "callback_data": "/PIRO_broadcast2 CANCEL"}]
]

bot.sendMessage(
    f"<b>Are You Sure You Want To Broadcast This Message To {len(FulUsrs)} User(s)?</b>",
    parse_mode="html",
    reply_markup={"inline_keyboard": keyboard}
)''',
    '/PIRO_broadcast1_btnbulk': r'''AllBotAdminss = Bot.getData("AllBotAdminss") or []
is_Admin = str(u) in map(str, AllBotAdminss)
if not is_Admin:
    bot.replyText(u, "<b><i>🚫 You Are Not This Bot Admin</i></b>", parse_mode="html")
    raise ReturnCommand()

Txt = str(message.text or "").strip()

if Txt == "/cancel":
    bot.sendMessage("<b>Cancelled — no buttons added.</b>")
    Bot.runCommand("/PIRO_broadcast1c")
    raise ReturnCommand()

data = User.getData("broadcast_data")
if not data:
    bot.sendMessage("<b>⚠️ Broadcast data not found. Please start again with /broadcast.</b>")
    raise ReturnCommand()

Lines = [ln.strip() for ln in Txt.split("\n") if ln.strip()]

if not Lines:
    bot.sendMessage("<b>⚠️ Send at least one line, or /cancel.</b>")
    Bot.handleNextCommand("/PIRO_broadcast1_btnbulk")
    raise ReturnCommand()

Rows = []
BadLine = None

for line in Lines:
    parts = [p.strip() for p in line.split("&&")]
    row = []
    for p in parts:
        if " - " not in p:
            BadLine = line
            break
        label, link = p.split(" - ", 1)
        label = label.strip()
        link = link.strip()
        if not label or not (link.startswith("http://") or link.startswith("https://") or link.startswith("tg://")):
            BadLine = line
            break
        row.append({"text": label[:30], "url": link})
    if BadLine:
        break
    Rows.append(row)

if BadLine:
    bot.sendMessage(
        f"<b>⚠️ Couldn't read this line:</b>\n<code>{BadLine}</code>\n\n"
        "<b>Use: Label - https://link.com</b>\nTry again, or /cancel."
    )
    Bot.handleNextCommand("/PIRO_broadcast1_btnbulk")
    raise ReturnCommand()

data["button_rows"] = Rows
User.saveData("broadcast_data", data)

Bot.runCommand("/PIRO_broadcast1c")''',
    '/PIRO_broadcast1_btnlink': r'''AllBotAdminss = Bot.getData("AllBotAdminss") or []
is_Admin = str(u) in map(str, AllBotAdminss)
if not is_Admin:
    bot.replyText(u, "<b><i>🚫 You Are Not This Bot Admin</i></b>", parse_mode="html")
    raise ReturnCommand()

Url = str(message.text or "").strip()

if Url == "/cancel":
    User.deleteData("broadcast_pending_btn_text")
    bot.sendMessage("<b>Cancelled adding button.</b>")
    Bot.runCommand("/PIRO_broadcast1c")
    raise ReturnCommand()

if not (Url.startswith("http://") or Url.startswith("https://") or Url.startswith("tg://")):
    bot.sendMessage("<b>⚠️ Please send a valid link starting with https:// (or tg://), or /cancel.</b>")
    Bot.handleNextCommand("/PIRO_broadcast1_btnlink")
    raise ReturnCommand()

data = User.getData("broadcast_data")
if not data:
    bot.sendMessage("<b>⚠️ Broadcast data not found. Please start again with /broadcast.</b>")
    raise ReturnCommand()

BtnText = User.getData("broadcast_pending_btn_text") or "Open"

Buttons = data.get("buttons") or []
Buttons.append({"text": BtnText, "url": Url})
data["buttons"] = Buttons
User.saveData("broadcast_data", data)
User.deleteData("broadcast_pending_btn_text")

Bot.runCommand("/PIRO_broadcast1c")''',
    '/PIRO_broadcast1c': r'''# Confirm-broadcast interactive card: mode toggle, add/clear buttons, confirm/cancel.
# Re-renders (edits) the same message so it behaves like a live settings panel.

AllBotAdminss = Bot.getData("AllBotAdminss") or []
is_Admin = str(u) in map(str, AllBotAdminss)
if not is_Admin:
    bot.replyText(u, "<b><i>🚫 You Are Not This Bot Admin</i></b>", parse_mode="html")
    raise ReturnCommand()

data = User.getData("broadcast_data")
if not data:
    bot.sendMessage("<b>⚠️ Broadcast data not found. Please start again with /broadcast.</b>", parse_mode="html")
    raise ReturnCommand()

P = str(params) if params else ""

if P == "cancel":
    User.deleteData("broadcast_data")
    User.deleteData("broadcast_confirm_msgid")
    try:
        bot.editMessageText(chat_id=u, message_id=message.message_id, text="<b>❌ Broadcast Cancelled</b>", parse_mode="html")
    except:
        bot.sendMessage("<b>❌ Broadcast Cancelled</b>", parse_mode="html")
    raise ReturnCommand()

if P == "mode direct":
    data["mode"] = "direct"
    User.saveData("broadcast_data", data)

elif P == "mode forward":
    if not data.get("is_forwarded"):
        bot.replyText(u, "<b>⚠️ Forward Mode only works if you forwarded a message from a channel/chat. Your message wasn't forwarded.</b>")
    else:
        data["mode"] = "forward"
        User.saveData("broadcast_data", data)

elif P == "clearbtns":
    data["button_rows"] = []
    User.saveData("broadcast_data", data)

elif P == "addbtn":
    bot.replyText(
        u,
        '<tg-emoji emoji-id="6224161941305169199">🎛️</tg-emoji><b> Set Up Your Buttons</b>\n\n'
        "Type one line per button (or group), like this:\n"
        "<code>Label - https://link.com</code>\n\n"
        '<tg-emoji emoji-id="6082398290773546707">▹</tg-emoji><b> A single button</b>\n'
        "<code>Visit Site - https://example.com</code>\n\n"
        '<tg-emoji emoji-id="6082398290773546707">▹</tg-emoji><b> Stacked, one per row</b>\n'
        "<code>Join Channel - https://t.me/YourChannel\n"
        "Get Support - https://t.me/YourSupport</code>\n\n"
        '<tg-emoji emoji-id="6082398290773546707">▹</tg-emoji><b> Side by side in one row</b>\n'
        "Separate with <code>&&</code>:\n"
        "<code>Join - https://t.me/A && Support - https://t.me/B</code>\n\n"
        '<tg-emoji emoji-id="6082398290773546707">▹</tg-emoji><b> Mix rows and pairs freely</b>\n'
        "<code>Join - https://t.me/A && Support - https://t.me/B\n"
        "Website - https://example.com</code>\n\n"
        '<tg-emoji emoji-id="5398001711786762757">✎</tg-emoji><i> Every new line becomes a new row. Use && to keep buttons on the same row.</i>\n\n'
        "Send /cancel if you don't want any buttons."
    )
    Bot.handleNextCommand("/PIRO_broadcast1_btnbulk")
    raise ReturnCommand()

elif P == "confirm":
    try:
        bot.editMessageText(chat_id=u, message_id=message.message_id, text="<b>🔁 Sending broadcast...</b>", parse_mode="html")
    except:
        pass
    Bot.runCommand("/PIRO_broadcast2")
    raise ReturnCommand()

# ---- Render / re-render the card ----
TYPE_LABELS = {
    "send_message": "TEXT",
    "send_photo": "PHOTO",
    "send_video": "VIDEO",
    "send_document": "DOCUMENT",
    "send_audio": "AUDIO",
    "send_voice": "VOICE",
    "send_sticker": "STICKER",
    "send_animation": "GIF / ANIMATION",
    "send_contact": "CONTACT",
}

TypeLabel = TYPE_LABELS.get(data.get("direct_function"), "UNKNOWN")
Mode = data.get("mode", "direct")
Rows = data.get("button_rows") or []
BtnCount = sum(len(r) for r in Rows)

if Mode == "direct":
    ModeLine = "🚀 <b>DIRECT MODE</b> (Clean broadcast, no forwarding tag)"
else:
    ModeLine = "🔄 <b>FORWARD MODE</b> (Shows 'Forwarded from' tag)"

if BtnCount:
    BtnLine = f"✅ {BtnCount} Button(s) In {len(Rows)} Row(s)"
    if Mode == "forward":
        BtnLine += "  <i>(ignored in Forward Mode)</i>"
else:
    BtnLine = "❌ No Buttons"

FulUsrs = Bot.getData("FulBotUsrs") or []

TXT = (
    "<b>⚠️ CONFIRM BROADCAST</b>\n\n"
    f"<b>Type:</b> {TypeLabel}\n"
    f"<b>Mode:</b> {ModeLine}\n"
    f"<b>Buttons:</b> {BtnLine}\n\n"
    "<i>Select mode then click Confirm:</i>\n"
    "• 🚀 Direct Mode: Fast, clean (recommended)\n"
    "• 🔄 Forward Mode: Shows 'Forwarded from' tag\n\n"
    f"Do you want to send this message to all {len(FulUsrs)} user(s)?"
)

keyboard = []

keyboard.append([
    {"text": ("🚀 ✅ Direct Mode" if Mode == "direct" else "🚀 Direct Mode"), "callback_data": "/PIRO_broadcast1c mode direct"},
    {"text": ("🔄 ✅ Forward Mode" if Mode == "forward" else "🔄 Forward Mode"), "callback_data": "/PIRO_broadcast1c mode forward"}
])

if BtnCount:
    keyboard.append([
        {"text": "➕ Add More", "callback_data": "/PIRO_broadcast1c addbtn"},
        {"text": "🗑 Clear Buttons", "callback_data": "/PIRO_broadcast1c clearbtns"}
    ])
else:
    keyboard.append([{"text": "➕ Add Buttons", "callback_data": "/PIRO_broadcast1c addbtn"}])

keyboard.append([
    {"text": "✅ Confirm & Send", "callback_data": "/PIRO_broadcast1c confirm"},
    {"text": "❌ Cancel", "callback_data": "/PIRO_broadcast1c cancel"}
])

StoredMid = User.getData("broadcast_confirm_msgid")
Edited = False
if StoredMid:
    try:
        bot.editMessageText(chat_id=u, message_id=StoredMid, text=TXT, parse_mode="html", reply_markup={"inline_keyboard": keyboard})
        Edited = True
    except:
        Edited = False

if not Edited:
    sent = bot.sendMessage(TXT, parse_mode="html", reply_markup={"inline_keyboard": keyboard})
    try:
        Mid = sent.message_id
    except:
        Mid = sent["message_id"]
    User.saveData("broadcast_confirm_msgid", Mid)''',
    '/PIRO_broadcast2': r'''# Admin Check
AllBotAdminss = Bot.getData("AllBotAdminss") or []

is_Admin = False
for userid in AllBotAdminss:
    if str(u) == str(userid):
        is_Admin = True
        break

if not is_Admin:
    bot.replyText(u, "<b><i>🚫 You Are Not This Bot Admin</i></b>", parse_mode="html")
    raise ReturnCommand()

action = str(params).upper()

# Handle Cancel
if action == "CANCEL":
    User.deleteData("broadcast_data")
    User.deleteData("broadcast_btn_text")
    try:
        bot.deleteMessage(message.chat.id, message.message_id)
    except Exception:
        pass
    bot.sendMessage("<b>❌ Broadcast Cancelled</b>", parse_mode="html")
    raise ReturnCommand()

# Handle Confirm
if action == "CONFIRM":
    data = User.getData("broadcast_data")

    if not data:
        bot.sendMessage("<b>⚠️ Broadcast data not found. Please resend the message.</b>", parse_mode="html")
        raise ReturnCommand()

    # Generate webhook callback URL to trigger /PIRO_broadcast3 upon completion
    url = libs.Webhook.getUrlFor("/PIRO_broadcast3", u)

    try:
        func = data.get("function")
        kwargs = {k: v for k, v in data.items() if k != "function"}

        # Launch mass broadcast task using function safelist
        task = Bot.broadcast(function=func, callback_url=url, **kwargs)

        if task.get("status") != "success":
            bot.sendMessage(f"<b>❌ Broadcast failed:</b>\n\n<code>{task}</code>", parse_mode="html")
            raise ReturnCommand()

        broadcast_id = task.get("broadcast_id")
        User.saveData("active_broadcast_id", broadcast_id)
        User.deleteData("broadcast_data")
        User.deleteData("broadcast_btn_text")

        try:
            bot.deleteMessage(message.chat.id, message.message_id)
        except Exception:
            pass

        bot.sendMessage("<b>🔁 Broadcast started successfully!</b>", parse_mode="html")

    except Exception as e:
        bot.sendMessage(f"<b>❌ Error starting broadcast:</b>\n\n<code>{str(e)}</code>", parse_mode="html")
        ''',
    '/PIRO_broadcast3': r'''try:
    # Parse webhook options payload delivered upon completion
    if hasattr(options, "json") and options.json:
        data = bunchify(options.json)
    else:
        data = options or {}

    total = data.get("total", 0)
    success = data.get("total_success", 0)
    fail = data.get("total_errors", 0)

    txt = (
        f"<b>📊 Broadcast Completed!</b>\n\n"
        f"<b>Total Attempted:</b> {total}\n"
        f"<b>Total Success:</b> {success}\n"
        f"<b>Total Failed:</b> {fail}"
    )

    bot.sendMessage(text=txt, parse_mode="html")
    User.deleteData("active_broadcast_id")

except Exception as e:
    bot.sendMessage(text=f"<b>📊 Broadcast Completion Error:</b>\n\n<code>{str(e)}</code>", parse_mode="html")
    ''',
    '/SCHEDULE_BROADCAST': r'''# Admin Check
AllBotAdminss = Bot.getData("AllBotAdminss") or []
is_Admin = False
for userid in AllBotAdminss:
    if str(u) == str(userid):
        is_Admin = True
if is_Admin != True:
    bot.sendMessage("<b><i>🚫You Are Not This Bot Admin</i></b>")
    raise ReturnCommand()

# Get all users
FulBotUsrs = Bot.getData("FulBotUsrs") or []
if not FulBotUsrs:
    bot.sendMessage("<b>❌ No users found in bot!</b>")
    raise ReturnCommand()

bot.sendMessage(f"""<b>📅 Schedule Broadcast to {len(FulBotUsrs)} Users</b>

<i>Send date & time in one line:</i>
<code>DD-MM-YYYY HH:MM AM/PM</code>
<b>Example:</b> <code>25-12-2025 02:30 PM</code>

<i>Send /cancel to cancel</i>""")

Bot.handleNextCommand("/SCHEDULE_SET_TIME")''',
    '/SCHEDULE_EXECUTE': r'''# No admin check – runs automatically

task_id = options
task_data = Bot.getData(f"BC_Task_{task_id}") or {}
if not task_data:
    raise ReturnCommand()

u = task_data.get("user")
bc_data = task_data.get("data", {})
txt = bc_data.get("text", "")
file_id = bc_data.get("file_id")
msg_type = bc_data.get("type", "text")

# ✅ Get latest user list at execution time
users = Bot.getData("FulBotUsrs") or []

if not users:
    bot.sendMessage("<b>❌ No users to broadcast!</b>")
    raise ReturnCommand()

# Send to all users
success = 0
failed = 0
failed_list = []

for user_id in users:
    try:
        if msg_type == "text":
            bot.sendMessage(chat_id=str(user_id), text=txt, parse_mode="html", disable_web_page_preview=True)
        elif msg_type == "photo":
            bot.sendPhoto(chat_id=str(user_id), photo=file_id, caption=txt, parse_mode="html")
        elif msg_type == "video":
            bot.sendVideo(chat_id=str(user_id), video=file_id, caption=txt, parse_mode="html")
        elif msg_type == "document":
            bot.sendDocument(chat_id=str(user_id), document=file_id, caption=txt, parse_mode="html")
        elif msg_type == "audio":
            bot.sendAudio(chat_id=str(user_id), audio=file_id, caption=txt, parse_mode="html")
        elif msg_type == "sticker":
            bot.sendSticker(chat_id=str(user_id), sticker=file_id)
        elif msg_type == "animation":
            bot.sendAnimation(chat_id=str(user_id), animation=file_id, caption=txt, parse_mode="html")
        success += 1
    except:
        failed += 1
        failed_list.append(str(user_id))

# Report to admin
report = f"""<b>📢 Scheduled Broadcast Complete</b>
✅ Success: {success}
❌ Failed: {failed}
📊 Total: {len(users)}"""
if failed_list:
    report += "\n\n<b>⚠️ Failed Users:</b>\n" + "\n".join(failed_list[:10])

bot.sendMessage(report, disable_web_page_preview=True)

# Cleanup task data
Bot.saveData(f"BC_Task_{task_id}", None)''',
    '/SCHEDULE_SET_MESSAGE': r'''# Admin Check
AllBotAdminss = Bot.getData("AllBotAdminss") or []
is_Admin = False
for userid in AllBotAdminss:
    if str(u) == str(userid):
        is_Admin = True
if is_Admin != True:
    bot.sendMessage("<b><i>🚫You Are Not This Bot Admin</i></b>")
    raise ReturnCommand()

if message.text == "/cancel":
    bot.sendMessage("<b>❌ Cancelled</b>")
    Bot.saveData("BC_Schedule_Date", None)
    Bot.saveData("BC_Schedule_Time_12hr", None)
    Bot.saveData("BC_Schedule_Hour", None)
    Bot.saveData("BC_Schedule_Minute", None)
    raise ReturnCommand()

# Detect content
txt = message.caption if message.caption else message.text
entities = message.caption_entities if message.caption else message.entities
if entities:
    txt = apply_html_entities(txt, entities, {})

msg_type = "text"
file_id = None
if message.photo:
    msg_type = "photo"
    file_id = message.photo[-1].file_id
elif message.video:
    msg_type = "video"
    file_id = message.video.file_id
elif message.document:
    msg_type = "document"
    file_id = message.document.file_id
elif message.audio:
    msg_type = "audio"
    file_id = message.audio.file_id
elif message.sticker:
    msg_type = "sticker"
    file_id = message.sticker.file_id
elif message.animation:
    msg_type = "animation"
    file_id = message.animation.file_id
elif message.text:
    msg_type = "text"
else:
    bot.sendMessage("<b>❌ Unsupported file!</b>")
    raise ReturnCommand()

# ✅ Store only a flag, not the user list (to include new users later)
bc_data = {
    "type": msg_type,
    "text": txt,
    "file_id": file_id,
    "target": "all_users"   # Fetch latest users at execution
}
Bot.saveData("BC_Data", bc_data)

# Calculate delay
date_str = Bot.getData("BC_Schedule_Date")
time_12hr = Bot.getData("BC_Schedule_Time_12hr")
hour24 = Bot.getData("BC_Schedule_Hour")
minute = Bot.getData("BC_Schedule_Minute")

now = libs.DateAndTime.now("Asia/Kolkata")
cur_d = now["date"].split("-")
cur_y, cur_m, cur_d = map(int, cur_d)
cur_h, cur_min = map(int, now["time"].split(":")[:2])

d, m, y = map(int, date_str.split("-"))
diff_days = (y - cur_y) * 365 + (m - cur_m) * 30 + (d - cur_d)
diff_sec = diff_days * 86400 + (hour24 - cur_h) * 3600 + (minute - cur_min) * 60
if diff_sec < 5:
    diff_sec = 5

# Create task
task_counter = Bot.getData("BC_Task_Counter") or 0
task_counter += 1
Bot.saveData("BC_Task_Counter", task_counter)
task_id = f"sch_{u}_{task_counter}"
Bot.saveData(f"BC_Task_{task_id}", {"user": u, "data": bc_data, "date": date_str, "time": time_12hr})

# Schedule
Bot.runCommandAfter(diff_sec, "/SCHEDULE_EXECUTE", options=task_id)

# Show confirmation with current user count (new users will be included later)
current_users = Bot.getData("FulBotUsrs") or []
bot.sendMessage(f"""<b>✅ Broadcast Scheduled!</b>

📆 {date_str} at {time_12hr}
👥 {len(current_users)} users (will include new users at execution)
📝 {msg_type.upper()}

<i>Will run automatically.</i>""")

# Cleanup temp
Bot.saveData("BC_Schedule_Date", None)
Bot.saveData("BC_Schedule_Time_12hr", None)
Bot.saveData("BC_Schedule_Hour", None)
Bot.saveData("BC_Schedule_Minute", None)
Bot.saveData("BC_Data", None)''',
    '/SCHEDULE_SET_TIME': r'''# Admin Check
AllBotAdminss = Bot.getData("AllBotAdminss") or []
is_Admin = False
for userid in AllBotAdminss:
    if str(u) == str(userid):
        is_Admin = True
if is_Admin != True:
    bot.sendMessage("<b><i>🚫You Are Not This Bot Admin</i></b>")
    raise ReturnCommand()

if message.text == "/cancel":
    bot.sendMessage("<b>❌ Cancelled</b>")
    raise ReturnCommand()

# Parse: "25-12-2025 02:30 PM"
parts = message.text.strip().split(" ")
if len(parts) < 3:
    bot.sendMessage("<b>❌ Invalid! Use: DD-MM-YYYY HH:MM AM/PM</b>")
    raise ReturnCommand()

date_str = parts[0]
time_str = parts[1]
ampm = parts[2].upper()

# Validate date
try:
    d, m, y = map(int, date_str.split("-"))
    if d<1 or d>31 or m<1 or m>12 or y<2024: raise ValueError
except:
    bot.sendMessage("<b>❌ Invalid date! Use DD-MM-YYYY</b>")
    raise ReturnCommand()

# Validate time
try:
    h, mn = map(int, time_str.split(":"))
    if h<1 or h>12 or mn<0 or mn>59: raise ValueError
    if ampm not in ["AM","PM"]: raise ValueError
except:
    bot.sendMessage("<b>❌ Invalid time! Use HH:MM AM/PM</b>")
    raise ReturnCommand()

# Convert to 24hr
if ampm == "PM" and h != 12:
    h24 = h + 12
elif ampm == "AM" and h == 12:
    h24 = 0
else:
    h24 = h

# Check future
now = libs.DateAndTime.now("Asia/Kolkata")
cur_d = now["date"].split("-")
cur_y, cur_m, cur_d = map(int, cur_d)
cur_h, cur_min = map(int, now["time"].split(":")[:2])

is_future = False
if y > cur_y:
    is_future = True
elif y == cur_y and m > cur_m:
    is_future = True
elif y == cur_y and m == cur_m and d > cur_d:
    is_future = True
elif y == cur_y and m == cur_m and d == cur_d:
    if h24 > cur_h or (h24 == cur_h and mn > cur_min):
        is_future = True

if not is_future:
    bot.sendMessage("<b>❌ Scheduled time is in the past! Choose a future time.</b>")
    raise ReturnCommand()

# Store
Bot.saveData("BC_Schedule_Date", f"{d:02d}-{m:02d}-{y}")
Bot.saveData("BC_Schedule_Time_12hr", f"{h:02d}:{mn:02d} {ampm}")
Bot.saveData("BC_Schedule_Hour", h24)
Bot.saveData("BC_Schedule_Minute", mn)

bot.sendMessage(f"""<b>📅 Schedule Broadcast</b>

📆 Date: {d:02d}-{m:02d}-{y}
⏰ Time: {h:02d}:{mn:02d} {ampm}

<i>Now send your message (text/photo/video/document)</i>
<i>Send /cancel to cancel</i>""")

Bot.handleNextCommand("/SCHEDULE_SET_MESSAGE")''',
    '/TransferOwnership': r'''owner = Bot.getData("Owner")

if str(u) != str(owner):
    bot.replyText(
        u,
        "<b>🚫 Only the bot owner can transfer ownership.</b>",
        parse_mode="HTML"
    )
    raise ReturnCommand()

bot.replyText(
    u,
    "<b>👤 Send the Telegram ID of the new owner.</b>\n\n",
    parse_mode="HTML"
)

Bot.handleNextCommand("/TransferOwnership_Save")''',
    '/TransferOwnership_Save': r'''owner = Bot.getData("Owner")

if str(u) != str(owner):
    raise ReturnCommand()

new_owner = message.text.strip()

if not new_owner.isdigit():
    bot.replyText(
        u,
        "<b>❌ Please send a valid Telegram User ID.</b>",
        parse_mode="HTML"
    )
    raise ReturnCommand()

# Cannot transfer to yourself
if str(new_owner) == str(owner):
    bot.replyText(
        u,
        "<b>❌ You are already the owner of this bot.</b>",
        parse_mode="HTML"
    )
    raise ReturnCommand()

# User must have started the bot
users = Bot.getData("FulBotUsrs") or []

if str(new_owner) not in [str(x) for x in users]:
    bot.replyText(
        u,
        "<b>❌ This user hasn't started the bot yet.\n\nAsk them to start the bot first, then try again.</b>",
        parse_mode="HTML"
    )
    raise ReturnCommand()

# Add to admin list if missing
admins = Bot.getData("AllBotAdminss") or []

if str(new_owner) not in [str(x) for x in admins]:
    admins.append(str(new_owner))
    Bot.saveData("AllBotAdminss", admins)

old_owner = owner
Bot.saveData("Owner", str(new_owner))

bot.replyText(
    u,
    f"""<b>✅ Ownership transferred successfully.

👤 Old Owner:
<code>{old_owner}</code>

👑 New Owner:
<code>{new_owner}</code></b>""",
    parse_mode="HTML"
)

# Notify new owner (ignore errors)
try:
    bot.sendMessage(
        chat_id=new_owner,
        text="<b>👑 Congratulations!\n\nYou are now the owner of this bot.</b>",
        parse_mode="HTML"
    )
except:
    pass''',
    '/admin': r'''AllBotAdminss = Bot.getData("AllBotAdminss") or []

is_Admin = False
for userid in AllBotAdminss:
    if str(u) == str(userid):
        is_Admin = True

if AllBotAdminss == []:
    MAIN_ADMIN = u
    AllBotAdminss.append(MAIN_ADMIN)
    Bot.saveData("AllBotAdminss", AllBotAdminss)

    AllBotAdminss = Bot.getData("AllBotAdminss") or []
    Bot.saveData("Owner", u)

if is_Admin != True:
    bot.replyText(
        u,
        "<b><i>🚫 You Are Not This Bot Admin</i></b>"
    )
    raise ReturnCommand()



Owner = Bot.getData("Owner") or None

# Owner always has all permissions
is_Owner = str(u) == str(Owner)

Permissions = Bot.getData("AdminPermissions") or {}


DEFAULT_OFF_PERMISSIONS = [
    "permission",
    "reset_balance",
]


def HasPermission(key):

    # Owner always has everything
    if is_Owner:
        return True

    # If permission was manually saved, use saved value
    if key in Permissions:
        return Permissions[key]

    # New permissions listed here are OFF by default
    if key in DEFAULT_OFF_PERMISSIONS:
        return False

    # All other permissions are ON by default
    return True

now = libs.DateAndTime.now("Asia/Kolkata")
date = now["date"]
time = now["time"][:5]

year, month, day = date.split("-")
hour, minute = time.split(":")

hour = int(hour)

ampm = "am"

if hour >= 12:
    ampm = "pm"

if hour > 12:
    hour -= 12

if hour == 0:
    hour = 12

MONTHS = {
    "01": "Jan",
    "02": "Feb",
    "03": "Mar",
    "04": "Apr",
    "05": "May",
    "06": "Jun",
    "07": "Jul",
    "08": "Aug",
    "09": "Sep",
    "10": "Oct",
    "11": "Nov",
    "12": "Dec"
}

EasyTime = f"{int(day)} {MONTHS[month]}, {hour:02}:{minute} {ampm}"


# ==============================
# DATA
# ==============================

AdmAC = Bot.getData("AdmAC") or []

perRef = Bot.getData("PerRefer") or 0
daiBon = Bot.getData("DailyBonus") or 0

minWit = Bot.getData("MinWith") or 0
maxWit = Bot.getData("MaxWith") or 0

upi_minWit = Bot.getData("MinWith1") or 0
upi_maxWit = Bot.getData("MaxWith1") or 0

minWit1 = Bot.getData("MinWith1") or 0
maxWit1 = Bot.getData("MaxWith1") or 0
SetMinRef1 = Bot.getData("SetMinRef1") or 0
tax = Bot.getData("Tax") or 0

PayOutChannel = Bot.getData("Botpaychannel") or "🚫 Not Set"

MinRef = Bot.getData("MinRefToWithdrw") or 0
SetMinRef = Bot.getData("SetMinRef") or 0

Owner = Bot.getData("Owner") or None

ownerid = f"tg://openmessage?user_id={Owner}"
owner = f"<a href='{ownerid}'>{Owner}</a>"

GatewayList={
    "vsv":{"name":"Vsv Wallet","status_key":"vsv","tax_key":"vsvtax"},
    "payzy":{"name":"Payzy Wallet","status_key":"payzy","tax_key":"payzytax"},
    "sa":{"name":"Saathi Gateway","status_key":"sa","tax_key":"satax"},
    "ultra":{"name":"Ultra Wallet","status_key":"ultra","tax_key":"ultratax"},
    "txg":{"name":"TXG Wallet","status_key":"txg","tax_key":"txgtax"},
    "rupix":{"name":"Rupix Wallet","status_key":"rupix","tax_key":"rupixtax"}
}

tax_lines=[]

for gid,gw in GatewayList.items():
    status_key=gw.get("status_key")
    tax_key=gw.get("tax_key")

    if Bot.getData(status_key):
        tax=Bot.getData(tax_key) or 0
        name=gw.get("name",gid)
        tax_lines.append(f"🧾 {name} Tax: ₹{tax}")

gateway_tax_text="\n".join(tax_lines) or "🧾 Tax: Not Set"
# ==============================
# BOT MODE
# ==============================

if "BotMode" in str(params):

    P = params.split(" ")

    mode = "ON" if P[1].upper() == "ON" else "OFF"

    Bot.saveData("BotMode", mode)

    act = f"🤖 Bot Mode Turned {mode}"

    AdmAC.append(
        f"<b>📆 Time:</b> {EasyTime}\n"
        f"👥 <b>By {message.from_user.first_name}</b> "
        f"[ID: <code>{u}</code>]\n"
        f"🔍<b> Action: </b> {act}"
    )

    Bot.saveData("AdmAC", AdmAC)


BOT_MODE = Bot.getData("BotMode") or "ON"

botSta = "🟢 On" if BOT_MODE == "ON" else "🔴 Off"

botStatChngeBut = (
    "BotMode OFF"
    if BOT_MODE == "ON"
    else "BotMode ON"
)


# ==============================
# NEW USER NOTIFICATION
# ==============================

NewUserNotification = Bot.getData(
    "NewUserNotification"
) or "ON"


if "NewUserNotification" in str(params):

    P = params.split(" ")

    mode = "ON" if P[1].upper() == "ON" else "OFF"

    Bot.saveData(
        "NewUserNotification",
        mode
    )

    NewUserNotification = mode

    act = f"🔔 New User Notification Turned {mode}"

    AdmAC.append(
        f"<b>📆 Time:</b> {EasyTime}\n"
        f"👥 <b>By {message.from_user.first_name}</b> "
        f"[ID: <code>{u}</code>]\n"
        f"🔍 <b>Action:</b> {act}"
    )

    Bot.saveData("AdmAC", AdmAC)


NewUserNotification = Bot.getData(
    "NewUserNotification"
) or "ON"


NewUserSta = (
    "🟢 On"
    if NewUserNotification == "ON"
    else "🔴 Off"
)


NewUserStatChngeBut = (
    "NewUserNotification OFF"
    if NewUserNotification == "ON"
    else "NewUserNotification ON"
)


# ==============================
# WITHDRAW MODE
# ==============================

if "WithdrawMode" in str(params):

    P = params.split(" ")

    mode = "ON" if P[1].upper() == "ON" else "OFF"

    Bot.saveData("WithdrawMode", mode)

    act = f"💸 Withdraws Turned {mode}"

    AdmAC.append(
        f"<b>📆 Time:</b> {EasyTime}\n"
        f"👥 <b>By {message.from_user.first_name}</b> "
        f"[ID: <code>{u}</code>]\n"
        f"🔍<b> Action: </b> {act}"
    )

    Bot.saveData("AdmAC", AdmAC)


WITHD_MODE = Bot.getData("WithdrawMode") or "ON"

witSta = (
    "🟢 On"
    if WITHD_MODE == "ON"
    else "🔴 Off"
)

witStatChngeBut = (
    "WithdrawMode OFF"
    if WITHD_MODE == "ON"
    else "WithdrawMode ON"
)


# ==============================
# VERIFICATION SYSTEM
# ==============================

current_verif_mode = int(
    Bot.getData("VerifModeSystem") or 0
)


if "ToggleVerifMode" in str(params):

    current_verif_mode = (
        current_verif_mode + 1
    ) % 4

    Bot.saveData(
        "VerifModeSystem",
        current_verif_mode
    )

    contact_status = (
        "ON"
        if current_verif_mode in [1, 3]
        else "OFF"
    )

    captcha_status = (
        "ON"
        if current_verif_mode in [2, 3]
        else "OFF"
    )

    Bot.saveData(
        "VerifySystemStatus",
        contact_status
    )

    Bot.saveData(
        "CaptchaMode",
        captcha_status
    )

    mode_labels = {
        0: "ALL OFF ❌",
        1: "CONTACT ONLY 📱",
        2: "CAPTCHA ONLY 🔐",
        3: "BOTH ON 🛡"
    }

    act = (
        f"⚙ Verification Mode Changed To: "
        f"{mode_labels[current_verif_mode]}"
    )

    AdmAC.append(
        f"<b>📆 Time:</b> {EasyTime}\n"
        f"👥 <b>By {message.from_user.first_name}</b> "
        f"[ID: <code>{u}</code>]\n"
        f"🔍<b> Action: </b> {act}"
    )

    Bot.saveData("AdmAC", AdmAC)


verif_display_text = {
    0: "🔴 All Off",
    1: "📱 Contact On",
    2: "🔐 Captcha On",
    3: "🛡 Both On"
}

verif_status_label = verif_display_text.get(
    current_verif_mode,
    "🔴 All Off"
)


# ==============================
# NEW USER MODE
# ==============================

NewU = Bot.getData("NewU") or "ON"

NewSta = (
    "🟢 On"
    if NewU == "ON"
    else "🔴 Off"
)

NewStatChngeBut = (
    "NewU OFF"
    if NewU == "ON"
    else "NewU ON"
)


# ==============================
# MENU
# ==============================

markup = InlineKeyboardMarkup()


# MANAGE ADMIN PERMISSION


# TRANSFER + TAX
buttons = []

if HasPermission("transfer_owner"):
    buttons.append(
        InlineKeyboardButton(
            text="👑 Tʀᴀɴsғᴇʀ Oᴡɴᴇʀsʜɪᴘ",
            callback_data="/TransferOwnership"
        )
    )


if buttons:
    markup.add(*buttons)


# VERIFICATION
if HasPermission("verification"):
    markup.add(
        InlineKeyboardButton(
            text="🛡 Vᴇʀɪғɪᴄᴀᴛɪᴏɴ: " + verif_status_label,
            callback_data="/admin ToggleVerifMode"
        )
    )


# ADMINS + BAN
buttons = []

if HasPermission("admins"):
    buttons.append(
        InlineKeyboardButton(
            text="👮 Mᴀɴᴀɢᴇ Aᴅᴍɪɴs",
            callback_data="/PIRO_Admins"
        )
    )


if buttons:
    markup.add(*buttons)


# PAYOUT CHANNEL



# WITHDRAW + BOT STATUS
buttons = []


if HasPermission("bot_status"):
    buttons.append(
        InlineKeyboardButton(
            text="🤖 Bᴏᴛ Sᴛᴀᴛᴜs: " + botSta,
            callback_data="/admin " + botStatChngeBut
        )
    )

if buttons:
    markup.add(*buttons)


# ADD BALANCE



# VERIFY USER + MANAGE TEXT
buttons = []


if HasPermission("manage_text"):
    buttons.append(
        InlineKeyboardButton(
            text="🚀 Mᴀɴᴀɢᴇ Tᴇxᴛ",
            callback_data="/ManageText"
        )
    )

if buttons:
    markup.add(*buttons)


# CHANNELS
if HasPermission("channels"):
    markup.add(
        InlineKeyboardButton(
            text="⚡ Mᴀɴᴀɢᴇ Cʜᴀɴɴᴇʟs",
            callback_data="/PIRO_Chanel_Pannel"
        )
    )


# RESET + BROADCAST
buttons = []


if buttons:
    markup.add(*buttons)


# WITHDRAWAL LIMITS

# ADMIN ACTIONS
if HasPermission("admin_actions"):
    markup.add(
        InlineKeyboardButton(
            text="📝 Rᴇᴄᴇɴᴛ Aᴅᴍɪɴ Aᴄᴛɪᴏɴs",
            callback_data="/PIRO_AdminAction"
        )
    )


# LEADERBOARD + CLICK HERE
buttons = []


if buttons:
    markup.add(*buttons)


# NEW USER NOTIFICATION
if HasPermission("new_user_notification"):
    markup.add(
        InlineKeyboardButton(
            text="🔔 Nᴇᴡ Usᴇʀ Nᴏᴛɪғɪᴄᴀᴛɪᴏɴ: " + NewUserSta,
            callback_data="/admin " + NewUserStatChngeBut
        )
    )


# GIFT + WITHDRAW OFF TEXT
buttons = []



if HasPermission("gateway"):
    markup.add(
        InlineKeyboardButton(
            text="🤹🏻 Mᴀɴᴀɢᴇ Mᴇɴᴜ Tᴇxᴛ",
            callback_data="/EditButtonTextPanel"
        )
    )
    

# ==============================
# ADMIN TEXT
# ==============================

TXT = f"""<b>👋🏻 Hᴇʟʟᴏ </b> <code>{message.from_user.first_name}</code>

<b>👑 Mᴀɪɴ Oᴡɴᴇʀ: {owner}

   🤖 Bᴏᴛ Sᴛᴀᴛᴜs: {botSta}
   💸 Wɪᴛʜᴅʀᴀᴡᴀʟ Sᴛᴀᴛᴜs: {witSta}
   🛡️ Vᴇʀɪғɪᴄᴀᴛɪᴏɴ Mᴏᴅᴇ: {verif_status_label}
   📢 Pᴀʏᴏᴜᴛ Cʜᴀɴɴᴇʟ: {PayOutChannel}
   🎫 Mɪɴɪᴍᴜᴍ Wɪᴛʜᴅʀᴀᴡᴀʟ(Wᴀʟʟᴇᴛ): ₹{minWit}
   🎟️ Mᴀxɪᴍᴜᴍ Wɪᴛʜᴅʀᴀᴡᴀʟ(Wᴀʟʟᴇᴛ): ₹{maxWit}
   🎫 Mɪɴɪᴍᴜᴍ Wɪᴛʜᴅʀᴀᴡᴀʟ(UPI): ₹{minWit1}
   🎟️ Mᴀxɪᴍᴜᴍ Wɪᴛʜᴅʀᴀᴡᴀʟ(UPI): ₹{maxWit1}
   👥 Pᴇʀ Rᴇғᴇʀ: ₹{perRef}
   🎯 Mɪɴ. Rᴇғᴇʀ Fᴏʀ Wɪᴛʜᴅʀᴀᴡ: {SetMinRef}
   🎁 Dᴀɪʟʏ Bᴏɴᴜs: ₹{daiBon}
   {gateway_tax_text}</b>

<i>🧭 Sᴇʟᴇᴄᴛ A Sᴇᴛᴛɪɴɢ Bᴇʟᴏᴡ Tᴏ Mᴀɴᴀɢᴇ Yᴏᴜʀ Bᴏᴛ.</i>"""


# ==============================
# COMMAND HANDLING
# ==============================

if str(message.text) == "/admin":

    bot.replyText(
        u,
        TXT,
        reply_markup=markup,
        parse_mode="HTML"
    )

    raise ReturnCommand()


if (
    params
    or params != None
    or options == "AP"
    or params == "AP"
):

    try:

        bot.editMessageText(
            chat_id=u,
            message_id=message.message_id,
            text=TXT,
            reply_markup=markup,
            parse_mode="HTML"
        )

    except:
        pass

    raise ReturnCommand()


bot.replyText(
    u,
    TXT,
    reply_markup=markup,
    parse_mode="HTML"
)''',
    '/broadcast': r'''# 19'4'25   00'11'10  v:1'0'0 [√]
# CODED BY: @Owner_Here 
# [ DM TO BUY ANY BOTS & CODES ]

AllBotAdminss = Bot.getData("AllBotAdminss") or []

is_Admin = False
for userid in AllBotAdminss:
    if str(u) == str(userid):
        is_Admin = True

if is_Admin != True:
    bot.replyText(u, "<b><i>🚫You Are Not This Bot Admin</i></b>")
    raise ReturnCommand()


bot.sendMessage(
                "<b>✍️ Write your message just like a normal Telegram chat, If Want With Photo Then Attach Photo And Mention Caption</b>\n\n"
                "You can use Telegram's built-in formatting:\n"
                "• <b>Bold</b> • <i>Italic</i> • <u>Underline</u> • <s>Strike</s> • <code>Mono</code>\n"
                "• <a href=\"https://t.me/\">Clickable Links</a> • <tg-spoiler>Spoiler</tg-spoiler> •\n"
                "<blockquote>Quote</blockquote>"
                "• Plain Text\n\n"
                "<b><i>No HTML or coding is required.</i></b>\n"
                "The bot will send the message exactly as you format it. ✅",parse_mode="html")

    
Bot.handleNextCommand("/PIRO_broadcast1")''',
    '/claimvoucher': r'''# /claimvoucher

if not libs.tbcads.claim():
    raise ReturnCommand

chars = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
voucher = ""

for i in range(32):
    voucher += chars[libs.Random.randomInt(0, 35)]

bot.sendMessage(
    "🎉 <b>Voucher Unlocked!</b>\n\n"
    "🎟️ <code>" + voucher + "</code>\n\n"
    "✅ Ad verified successfully!"
)''',
    '/delete': r'''EMOJI_FAILED = "6188201202337977430"

try:
    bot.deleteMessage(u,message.message_id)
except:
    pass
bot.sendMessage(f'<tg-emoji emoji-id="{EMOJI_FAILED}">🚫</tg-emoji><b>Payout Request Canceled<tg-emoji emoji-id="{EMOJI_FAILED}">🚫</tg-emoji> </b>')''',
    '/handler_special_updates': r'''if update_type == "my_chat_member":
    chat = message.chat
    new_status = message.new_chat_member.status
    
    #proceed if bot gt add in channel
    if chat.type == "channel" and new_status == "administrator":
        chat_id = chat.id
        chat_username = chat.username
        L="link..."
        
        AllBotAdminss = Bot.getData("AllBotAdminss") or []
 
        lnkFound=True
        try:
            L=bot.exportChatInviteLink(message.chat.id)
        except:
            lnkFound=False
         
        if lnkFound==False or str(L)=="None": 
            for A in AllBotAdminss:
                try:
                    bot.replyText(A,f"<b>🚫 Can't Add Invite Link To  {message.chat.id} Because Admin Detected, But No Permission To Invite Users. 😊</b>",parse_mode="HTML",disable_web_page_preview=True)
                except:
                    pass
            raise ReturnCommand()
              
        AMC = Bot.getData("AllMainCh") or []
        AMCL=Bot.getData("AllMainChlink") or []
        AMCU=Bot.getData("AllMainChUsernm") or []

        AMCTGID=Bot.getData("AllMainChTGID") or []
        
        try:
            try:
                IND=AMCTGID.index(int(message.chat.id))
            except:
                IND=AMCTGID.index(str(message.chat.id))
            AMCL[IND]=L
            Bot.saveData("AllMainChlink",AMCL) 
        except:
            raise ReturnCommand()
        
        
       
        for A in AllBotAdminss:
            try:
                bot.replyText(A,f"<b>🎉 Bot Is Admin In {AMCU[IND]} 🎉\n\n🔗 Invite Link Successfully Added Automatically! 😊</b>",parse_mode="HTML",disable_web_page_preview=True)
        
                bot.replyText(A,f"<b>🔗 Invite Link {L} Added To {AMCU[IND]} Because Admin With Invite Users Permissions Was Detected. 😊</b>",parse_mode="HTML",disable_web_page_preview=True)

            except:
                pass




if update_type == "chat_join_request":
    user_id = message.from_user.id
    
    Bot.saveData(str(message.chat.id)+"JoinRRof"+str(user_id),"Y")
    ''',
    '/info': r'''EMOJI_JENISH = "6264839164248727584"

# 19'4'25   00'11'10  v:1'0'0 [√]
# CODED BY: @Owner_Here
# [ DM TO BUY ANY BOTS & CODES ]

text = '<a href="http://t.me/Jenil_AutoMakerbot">AutoPay Maker [Jenil]</a>'

bot.replyText(
    u,
    f'<b>Developed By {text} <tg-emoji emoji-id="{EMOJI_JENISH}">🔍</tg-emoji></b>',
    parse_mode="HTML"
)''',
    '/onWebhook': r'''EMOJI_WARN = "5447644880824181073"
EMOJI_GIFT = "5203996991054432397"
EMOJI_SUCCESS = "6203820727083208720"
EMOJI_FAILED = "6129840374971112593"
EMOJI_CELE = "6129758753412619088"
EMOJI_PARTY = "6224161941305169199"
# ================= VALIDATION =================
if options is None:
    raise ReturnCommand()

if options.json is None:
    raise ReturnCommand()


# ================= RESPONSE =================
res = options.json

# ================= FINGERPRINT =================
fp = str(
    res.get("fingerprint", "")
).strip()

botusername = str(
    res.get("botusername", "")
).strip()

fp = str(
    res.get("fingerprint", "")
).strip()

if fp == "":

    bot.sendMessage(
        f'<tg-emoji emoji-id="{EMOJI_FAILED}">🚫</tg-emoji>Fingerprint Missing'
)

    raise ReturnCommand()


# ================= SAME DEVICE CHECK =================

used_device = Bot.getData(
    "FP_" + fp
)

if used_device is not None:

    Bot.saveData(
        str(u) + "sameDev",
        "yes"
    )

    User.saveData(
        "verify",
        "ok"
    )

    User.saveData(
        "PhVerification",
        "Verified"
    )

    Bot.runCommand(
        "/PIRO_MainMenu",
        options=f"""
<b><tg-emoji emoji-id="{EMOJI_WARN}">⚠️</tg-emoji>Same Device Detected

<tg-emoji emoji-id="{EMOJI_GIFT}">🎁</tg-emoji>Referral Bonus Not Available.

<tg-emoji emoji-id="{EMOJI_CELE}">🥳</tg-emoji>But You Can Still Use The Bot.</b>
"""
    )

    raise ReturnCommand()


# ================= SAVE NEW DEVICE =================

Bot.saveData(
    "FP_" + fp,
    str(u)
)

botusername = str(
    res.get("botusername", "")
).strip()

User.saveData(
    "fingerprint",
    fp
)

User.saveData(
    "botusername",
    botusername
)

Bot.saveData(
    "FP_" + fp,
    str(u)
)

status = str(
    res.get("status", "")
).lower().strip()

message_txt = str(
    res.get("message", "")
).strip()


# ================= VERIFIED =================
if (
    status == "pass"
    and message_txt == "Verified Successfully"
):

    User.saveData(
        "verify",
        "ok"
    )

    User.saveData(
        "PhVerification",
        "Verified"
    )

    bot.sendMessage(
        f"""
<tg-emoji emoji-id="{EMOJI_PARTY}">🎉</tg-emoji><b>Verification Successful!

<tg-emoji emoji-id="{EMOJI_SUCCESS}">✅</tg-emoji>Welcome To Wallet Bot.</b>
""",
        parse_mode="HTML"
    )

    Bot.runCommand(
        "/PIRO_MainMenu"
    )

    raise ReturnCommand()


# ================= ALREADY VERIFIED =================
elif (
    status == "pass"
    and message_txt == "Already Verified"
):

    User.saveData(
        "verify",
        "ok"
    )

    User.saveData(
        "PhVerification",
        "Verified"
    )

    Bot.runCommand(
        "/PIRO_MainMenu"
    )

    raise ReturnCommand()


# ================= VPN DETECTED =================
elif (
    status == "fail"
    and "VPN" in message_txt
):

    bot.sendMessage(
        f"""
<tg-emoji emoji-id="{EMOJI_FAILED}">🚫</tg-emoji><b>VPN Detected

<tg-emoji emoji-id="{EMOJI_WARN}">⚠️</tg-emoji>Disable VPN
And Try Again.</b>
""",
        parse_mode="HTML"
    )

    raise ReturnCommand()


# ================= SAME DEVICE =================
elif (
    status == "fail"
    and "Device already used" in message_txt
):

    Bot.saveData(
        str(u) + "sameDev",
        "yes"
    )

    User.saveData(
        "verify",
        "ok"
    )

    User.saveData(
        "PhVerification",
        "Verified"
    )

    Bot.runCommand(
        "/PIRO_MainMenu",
        options=f"""
<b><tg-emoji emoji-id="{EMOJI_WARN}">⚠️</tg-emoji>Same Device Detected

<tg-emoji emoji-id="{EMOJI_GIFT}">🎁</tg-emoji>Referral Bonus Not Available.

<tg-emoji emoji-id="{EMOJI_CELE}">🥳</tg-emoji>But You Can Still Use The Bot.</b>
"""
    )

    raise ReturnCommand()


# ================= FAILED =================
else:

    User.saveData(
        "verify",
        "pending"
    )

    bot.sendMessage(
        f"""
<tg-emoji emoji-id="{EMOJI_FAILED}">🚫</tg-emoji><b>Verification Failed

🔄 Please Try Again.</b>
""",
        parse_mode="HTML"
    )

    Bot.runCommand(
        "/PIRO_Verification"
    )

    raise ReturnCommand()''',
    '/onogf': r'''AllBotAdminss = Bot.getData("AllBotAdminss") or []
is_Admin = str(u) in [str(userid) for userid in AllBotAdminss]

if not is_Admin:
    bot.replyText(u, "<b><i>🚫 You Are Not Authorized! 👮‍♂️</i></b>")
    raise ReturnCommand()

Bot.sendMessage("✨📌 Send <b>Click Here</b> link for menu text 🔗📲")
Bot.handleNextCommand("//onogf")''',
    '/shareprofile': r'''try:

    # ============================================================
    # 🛑 CHECK CONTACT VERIFICATION SYSTEM
    # ============================================================

    VerifySystemStatus = Bot.getData(
        "VerifySystemStatus"
    ) or "OFF"


    # If Contact Verification is OFF
    if VerifySystemStatus != "ON":

        Bot.runCommand(
            "/PIRO_MainMenu"
        )

        raise ReturnCommand()


    # ============================================================
    # 📱 INCOMING CONTACT
    # ============================================================

    contact = message.contact


    if contact:

        phone_number = contact.phone_number
        user_id = contact.user_id
        current_sender_id = message.from_user.id


        # ========================================================
        # 🔐 SECURITY CHECK
        # ========================================================

        if user_id and str(user_id) != str(current_sender_id):

            bot.sendMessage(
                "❌ <b>Verification Failed!</b>\n\n"
                "Aapko apna khud ka number share karna hoga.",
                parse_mode="HTML"
            )

            Bot.handleNextCommand(
                "/shareprofile"
            )

            raise ReturnCommand()


        # ========================================================
        # ✅ SAVE CONTACT VERIFICATION
        # ========================================================

        User.saveData(
            "user_phone",
            str(phone_number)
        )

        User.saveData(
            "is_verified",
            True
        )


        # ========================================================
        # 🔐 CAPTCHA CHECK
        # ========================================================

        Captcha = Bot.getData(
            "CaptchaMode"
        ) or "ON"


        if Captcha == "ON":

            verify = User.getData(
                "verify"
            )


            if verify != "ok":

                # 🧹 DELETE CONTACT MESSAGE
                try:

                    current_msg_id = message.message_id

                    bot.deleteMessage(
                        chat_id=current_sender_id,
                        message_id=current_msg_id
                    )

                    bot.deleteMessage(
                        chat_id=current_sender_id,
                        message_id=current_msg_id - 1
                    )

                except:

                    pass


                # 🚀 GO TO MAIN MENU
                # MainMenu will send user to CAPTCHA
                Bot.runCommand(
                    "/PIRO_MainMenu"
                )

                raise ReturnCommand()


        # ========================================================
        # 🧹 DELETE CONTACT MESSAGES
        # ========================================================

        try:

            current_msg_id = message.message_id

            bot.deleteMessage(
                chat_id=current_sender_id,
                message_id=current_msg_id
            )

            bot.deleteMessage(
                chat_id=current_sender_id,
                message_id=current_msg_id - 1
            )

        except:

            pass


        # ========================================================
        # 🚀 ALL VERIFICATION PASSED
        # ========================================================

        Bot.runCommand(
            "/PIRO_MainMenu"
        )

        raise ReturnCommand()


    # ============================================================
    # 📱 INITIAL CONTACT PROMPT
    # ============================================================

    reply_markup = {
        "keyboard": [
            [
                {
                    "text": "📱 Share Profile",
                    "request_contact": True
                }
            ]
        ],
        "resize_keyboard": True,
        "one_time_keyboard": True
    }


    bot.sendMessage(
        "<b>🔒 Please Verify Your Profile To Avoid Fake Accounts\n\n"
        "⚠️ Your Profile Is Not Shared With Anyone & Is Safe !!</b>",
        parse_mode="HTML",
        reply_markup=reply_markup
    )


    # ============================================================
    # 🔁 HANDLE NEXT CONTACT
    # ============================================================

    Bot.handleNextCommand(
        "/shareprofile"
    )

    raise ReturnCommand()


except Exception as e:

    if type(e).__name__ != "ReturnCommand":

        pass''',
    '/start': r'''EMOJI_FAILED="6129840374971112593"
EMOJI_PIN="6129434968713076807"
EMOJI_SUCCESS="6082398290773546707"
EMOJI_HEART="5388790256772331442"

# 19'4'25   00'11'10  v:1'0'0 [√]
# CODED BY: @Owner_Here 
# [ DM TO BUY ANY BOTS & CODES ]
# Page-wise channel join (FAST: cache 1 min, delete only on Joined)

if message.chat.type!="private": raise returnCommand()

AllBotAdminss=Bot.getData("AllBotAdminss") or []
if AllBotAdminss==[]:
    MAIN_ADMIN=u
    AllBotAdminss.append(MAIN_ADMIN)
    Bot.saveData("AllBotAdminss",AllBotAdminss)
    Bot.saveData("Owner",u)
    AllBotAdminss=Bot.getData("AllBotAdminss") or []

BotMode=Bot.getData("BotMode") or "ON"
if BotMode=="OFF":
    bot.replyText(u,f'<tg-emoji emoji-id="{EMOJI_FAILED}">🚫</tg-emoji><b> Bot Is Currently Off</b>')
    raise ReturnCommand()

if Bot.getData("Bot7DayTimer")!="STARTED":
    Bot.saveData("Bot7DayTimer","STARTED")
    Bot.runCommandAfter(604800,"/AutoCheckBot")

referalID=None
if params!=None: referalID=Bot.getData(str(params)+"GetTgIDbyRefLetters")

FulUsrs=Bot.getData("FulBotUsrs") or []
refer=params

if params!=None:
    A=params
    if str(A[slice(3)])=="ref": refer=referalID

if params==None: refer="@not_referred"

Neww=Bot.getData("UserID"+str(u))
if Neww==None:
    Bot.saveData("UserID"+str(u),"done")
    FulUsrs.append(u)
    Bot.saveData("FulBotUsrs",FulUsrs)
    RefStrtC=Bot.getData(str(refer)+"RefStrtC") or 0
    Bot.saveData(str(refer)+"RefStrtC",RefStrtC+1)

already=User.getData("bot_user")

if already==None:
    Bot.saveData(str(u)+"Referral",refer)

    NewUserNotification=Bot.getData("NewUserNotification") or "ON"

    if NewUserNotification=="ON":

        if refer=="@not_referred":
            invited_text="🤖 Auto Started"
        else:
            invited_text=f'<a href="tg://user?id={refer}">{refer}</a>'

        safe_name=str(message.from_user.first_name or "Trying to fetch").replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")

        notify_text=f"""
<b><tg-emoji emoji-id="6224161941305169199">🎉</tg-emoji> New User Joined!</b>

<tg-emoji emoji-id="5039613856603702817">👤</tg-emoji> <b>User:</b> <a href="tg://user?id={u}">{safe_name}</a>
<tg-emoji emoji-id="5042334757040423886">🆔</tg-emoji> <b>User ID:</b> <code>{u}</code>
<tg-emoji emoji-id="5398001711786762757">👥</tg-emoji> <b>Invited By:</b> {invited_text}
"""

        for admin in AllBotAdminss:
            try: bot.replyText(admin,notify_text,parse_mode="HTML")
            except: pass

    User.saveData("bot_user",True)

    T_usrs=Bot.getData("total_users")
    if T_usrs==None: Bot.saveData("total_users",1)
    else: Bot.saveData("total_users",int(T_usrs)+1)

BotAdmins=Bot.getData("AllBotAdminss") or [str(u)]
BOTID=int(Bot.info().token.split(":")[0])

AMC=Bot.getData("AllMainCh") or []
AMCU=Bot.getData("AllMainChUsernm") or []
AMCL=Bot.getData("AllMainChlink") or []
AMCTGID=Bot.getData("AllMainChTGID") or []
AMCP=Bot.getData("AllMainChPage") or []
for _pad in range(len(AMCTGID) - len(AMCP)):
    AMCP.append(1)
for _pad in range(len(AMCTGID) - len(AMCU)):
    AMCU.append("Unknown")
for _pad in range(len(AMCTGID) - len(AMCL)):
    AMCL.append("https://t.me/" + str(AMCTGID[len(AMCL)]))

ALL_PAGES=sorted(set(AMCP)) if AMCTGID else []
CurPage=ALL_PAGES[0] if ALL_PAGES else 1

# ---- verified-channel cache (valid for the current MINUTE only) ----
_now = libs.DateAndTime.now("Asia/Kolkata")
Stamp = str(_now["date"]) + " " + str(_now["time"])[:5]
OKids = []
try:
    _c = User.getData("PIRO_OK")
    if _c and _c.get("s") == Stamp:
        OKids = list(_c.get("ids") or [])
except:
    OKids = []

joinSTAT="JOIND"
PIRO_NJCL=[]
FoundIdx=None

# Always start from the FIRST page, so pages come strictly 1 -> 2 -> 3
for PageIdx in range(0, len(ALL_PAGES)):
    ThisPage=ALL_PAGES[PageIdx]
    NotJoinedThisPage=[]

    IND=0
    for i in AMCTGID:
        if AMCP[IND]!=ThisPage:
            IND+=1
            continue

        sid=str(i)
        if sid in OKids:
            IND+=1
            continue

        UC="error"
        ERR=""
        try:
            UC=bot.getChatMember(i,u).status
        except Exception as e:
            UC="error"
            ERR=str(e)[:150]

        if UC=="member" or UC=="administrator" or UC=="creator":
            OKids.append(sid)

        elif UC=="left" or UC=="kicked":
            ReqRec=Bot.getData(sid+"JoinRRof"+str(u)) or "N"
            if ReqRec=="N":
                NotJoinedThisPage.append(sid)

        elif UC=="error":
            DC=Bot.getData(sid+"isDef") or "Y"
            JR=Bot.getData(sid+"JoinReq") or "Y"
            if JR=="Y" and DC!="Y":
                BotIsAdm=False
                try:
                    BS=bot.getChatMember(i,BOTID).status
                    if BS=="administrator" or BS=="creator":
                        BotIsAdm=True
                except:
                    BotIsAdm=False

                if not BotIsAdm:
                    if Bot.getData("PIRO_NAsent"+sid)!="Y":
                        Bot.saveData("PIRO_NAsent"+sid,"Y")
                        for admin in BotAdmins:
                            try: bot.replyText(admin,f"<b>👀 Bot Is Not Admin In {AMCU[IND]}! ⚠️\n\n✨ Only admins see this reminder (sent once per channel). 😊</b>")
                            except: pass
                else:
                    Bot.deleteData("PIRO_NAsent"+sid)
                    if Bot.getData("PIRO_ERRsent"+sid)!="Y":
                        Bot.saveData("PIRO_ERRsent"+sid,"Y")
                        for admin in BotAdmins:
                            try: bot.replyText(admin,f"<b>ℹ️ Bot IS admin in {AMCU[IND]}, but a member check failed:</b>\n<code>{ERR}</code>\n<i>(sent once per channel, only admins see this)</i>")
                            except: pass

        IND+=1

    if NotJoinedThisPage:
        joinSTAT="NOTJOIN"
        PIRO_NJCL=NotJoinedThisPage
        FoundIdx=PageIdx
        break

User.saveData("PIRO_OK",{"s":Stamp,"ids":OKids})

if FoundIdx is not None:
    CurPage=ALL_PAGES[FoundIdx]
elif ALL_PAGES:
    CurPage=ALL_PAGES[-1]

User.saveData("PIRO_CurPage",CurPage)
User.saveData("PIRO_NJCL",PIRO_NJCL)

Captcha=Bot.getData("CaptchaMode") or "ON"

if joinSTAT=="JOIND":
    if Captcha=="OFF": User.saveData("PhVerification","verify")
    Bot.runCommand("/PIRO_MainMenu")
    raise ReturnCommand()

# NOTE: old join message is intentionally NOT deleted here anymore.
# It is only deleted when the user actually taps the Joined button (faster /start & gates).

JoinBtnText = Bot.getData("PIRO_JoinBtnText") or "Join"
ClaimBtnText = Bot.getData("PIRO_ClaimBtnText") or "🔒 Claim"
JoinIcon = Bot.getData("PIRO_JoinBtnEmojiId")
ClaimIcon = Bot.getData("PIRO_ClaimBtnEmojiId")

keyboard=[]
row=[]
IND=0

for i in AMCTGID:
    if str(i) in PIRO_NJCL:
        btn={"text":JoinBtnText,"url":AMCL[IND],"style":"primary"}
        if JoinIcon: btn["icon_custom_emoji_id"]=JoinIcon
        row.append(btn)
        if len(row)==2:
            keyboard.append(row)
            row=[]
    IND+=1

if row: keyboard.append(row)

SocialLinks=Bot.getData("AllSocialLinks") or []

if SocialLinks:
    row=[]
    for s in SocialLinks:
        row.append({"text":f"{s['name']}","url":s['url'],"style":"primary"})
        if len(row)==2:
            keyboard.append(row)
            row=[]
    if row: keyboard.append(row)

cbtn={"text":ClaimBtnText,"callback_data":"🟢 Joined","style":"success"}
if ClaimIcon: cbtn["icon_custom_emoji_id"]=ClaimIcon
keyboard.append([cbtn])

usr=f'<a href="tg://user?id={str(u)}">{message.from_user.first_name}</a>'

JoinMsgChatId = Bot.getData("PIRO_JoinMsgChatId")
JoinMsgId = Bot.getData("PIRO_JoinMsgId")

SentOk = False
if JoinMsgChatId and JoinMsgId:
    try:
        Sent = bot.copyMessage(chat_id=u, from_chat_id=JoinMsgChatId, message_id=JoinMsgId, reply_markup={"inline_keyboard":keyboard})
        try:
            Mid = Sent.message_id
        except:
            Mid = Sent["message_id"]
        User.saveData("PIRO_ltmg", Mid)
        SentOk = True
    except:
        SentOk = False

if not SentOk:
    PIRO=bot.sendMessage(
        f"""<tg-emoji emoji-id="{EMOJI_HEART}">👋🏻</tg-emoji><b>Hey There {usr} Welcome To Wallet Bot !

<tg-emoji emoji-id="{EMOJI_PIN}">🛑</tg-emoji> Must Join All Channels To Use Our Bot

<tg-emoji emoji-id="{EMOJI_SUCCESS}">🚫</tg-emoji> After Joining Click Claim</b>""",
        reply_markup={"inline_keyboard":keyboard}
    )

    User.saveData("PIRO_ltmg",PIRO.message_id)

# Extra message shown below the join message (admin-editable)
Ex1 = Bot.getData("PIRO_JoinMsg2ChatId")
Ex2 = Bot.getData("PIRO_JoinMsg2Id")
if Ex1 and Ex2:
    try:
        S2 = bot.copyMessage(chat_id=u, from_chat_id=Ex1, message_id=Ex2)
        try:
            M2 = S2.message_id
        except:
            M2 = S2["message_id"]
        User.saveData("PIRO_ltmg2", M2)
    except:
        pass''',
    '/test': r'''# /gateway_status_checker

# Admin Verification
Admins = Bot.getData("AllBotAdminss") or []
if str(u) not in list(map(str, Admins)):
    bot.replyText(u, "<b><i>🚫 You Are Not This Bot Admin</i></b>")
    raise ReturnCommand()

def get_status(key):
    return "✅" if Bot.getData(key) else "❌"

# Gateway status indicators

payzy = get_status("payzy")
vsv = get_status("vsv")

# Inline Keyboard
markup = {
    "inline_keyboard": [
        [
            {"text": "Payzy Wallet", "callback_data": "/none"},
            {"text": payzy, "callback_data": "/payzywalletsele0"}
        ],
        [
            {"text": "Vsv Wallet", "callback_data": "/none"},
            {"text": vsv, "callback_data": "/Vsvwalletsele1"}
        ],
        [
            {"text": "🔙 Back", "callback_data": "/addgetwayonbbot"}
        ]
    ]
}

# Displaying or Editing the message
bot.editMessageText(
    chat_id=message.chat.id,
    message_id=message.message_id,
    text="<b>🚧 Choose The Gateways</b>",
    reply_markup=markup
)''',
    '@': r'''EMOJI_PARTY = "6224161941305169199"
EMOJI_BALL = "5039613856603702817"
EMOJI_DONE = "5398001711786762757"
EMOJI_PAISA = "5042334757040423886"
EMOJI_FAILED = "6129840374971112593"
EMOJI_WARN = "5447644880824181073"

if message.update_type == "chat_join_request":
    Bot.saveData(f"{message.chat.id}JoinRRof{u}", "Y")

if message.chat.type != "private":
    raise returnCommand()

PIRO_ban = Bot.getData(f"{u}ban") or False

if PIRO_ban:
    bot.replyText(u, f'<tg-emoji emoji-id="{EMOJI_FAILED}">🚫</tg-emoji><b> You\'re banned in this bot</b>')
    raise returnCommand()''',
    'Jaiho 777': r'''EMOJI_FAILED = "6129840374971112593"

if message.chat.type != "private":
    raise ReturnCommand()

BotMode = Bot.getData("BotMode")
if BotMode == "OFF":
    bot.replyText(u, f'<tg-emoji emoji-id="{EMOJI_FAILED}">🚫</tg-emoji><b> Bot Is Currently Off</b>')
    raise ReturnCommand()

# Page-wise join check, then open the referral screen
User.saveData("PIRO_GateTarget", "👫Referral1")
Bot.runCommand("/PIRO_JoinGate")''',
    'Lucky Gullak': r'''EMOJI_FAILED = "6129840374971112593"

if message.chat.type != "private":
    raise ReturnCommand()

BotMode = Bot.getData("BotMode")
if BotMode == "OFF":
    bot.replyText(u, f'<tg-emoji emoji-id="{EMOJI_FAILED}">🚫</tg-emoji><b> Bot Is Currently Off</b>')
    raise ReturnCommand()

# Page-wise join check, then open the bonus screen
User.saveData("PIRO_GateTarget", "🎁 Bonus1")
Bot.runCommand("/PIRO_JoinGate")''',
    'Yono 777': r'''EMOJI_FAILED = "6129840374971112593"

if message.chat.type != "private":
    raise ReturnCommand()

BotMode = Bot.getData("BotMode")
if BotMode == "OFF":
    bot.replyText(u, f'<tg-emoji emoji-id="{EMOJI_FAILED}">🚫</tg-emoji><b>Bot Is Currently Off</b>')
    raise ReturnCommand()

# Page-wise join check, then open the balance screen
User.saveData("PIRO_GateTarget", "💴 Account1")
Bot.runCommand("/PIRO_JoinGate")''',
    '🎁 Bonus1': r'''# ================= /LuckyGullak =================

EMOJI_WARN = "5447644880824181073"
EMOJI_EDIT = "5469741319330996757"
KEY = "LUCKYGULLAK_TEXT"
TAG = "lucky"

admin_ids = [str(x) for x in (Bot.getData("AllBotAdminss") or [])]
admin_ids.append(str(Bot.getData("Owner")))
is_admin = str(u) in admin_ids

text = Bot.getData(KEY) or ""
media = Bot.getData(KEY + "_MEDIA")


def send_media(mt, chat, fid, cap, pm, mk):
    kw = {"chat_id": str(chat)}
    if cap:
        kw["caption"] = cap
    if pm:
        kw["parse_mode"] = pm
    if mk:
        kw["reply_markup"] = mk
    if mt == "photo":
        kw["photo"] = fid
        return bot.sendPhoto(**kw)
    if mt == "video":
        kw["video"] = fid
        return bot.sendVideo(**kw)
    if mt == "animation":
        kw["animation"] = fid
        return bot.sendAnimation(**kw)
    if mt == "document":
        kw["document"] = fid
        return bot.sendDocument(**kw)
    if mt == "audio":
        kw["audio"] = fid
        return bot.sendAudio(**kw)
    if mt == "voice":
        kw["voice"] = fid
        return bot.sendVoice(**kw)
    raise Exception("unknown media type")


def send_content(chat, text, media, mk):
    mt = None
    fid = None
    if isinstance(media, dict):
        mt = media.get("type")
        fid = media.get("file_id")
    for attempt in range(3):
        pm = "HTML"
        m2 = mk
        if attempt == 1:
            m2 = None
        if attempt == 2:
            pm = None
            m2 = None
        try:
            if mt and fid:
                send_media(mt, chat, fid, text, pm, m2)
            elif pm:
                if m2:
                    bot.replyText(chat, text, parse_mode=pm, reply_markup=m2)
                else:
                    bot.replyText(chat, text, parse_mode=pm)
            else:
                bot.replyText(chat, text)
            return True
        except Exception:
            pass
    return False


admin_markup = {
    "inline_keyboard": [
        [
            {"text": "Edit This Text", "callback_data": "/EditBtnTxtStart " + TAG, "icon_custom_emoji_id": EMOJI_EDIT}
        ]
    ]
}

has_media = isinstance(media, dict) and media.get("file_id")

if not text.strip() and not has_media:
    if is_admin:
        send_content(u, f'<tg-emoji emoji-id="{EMOJI_WARN}">⚠️</tg-emoji> <b>No text added yet.</b>\nTap below to add it.', None, admin_markup)
    else:
        send_content(u, f'<tg-emoji emoji-id="{EMOJI_WARN}">⚠️</tg-emoji> <b>This section is being updated. Please check back soon.</b>', None, None)
    raise ReturnCommand

if is_admin:
    send_content(u, text, media, admin_markup)
else:
    send_content(u, text, media, None)''',
    '👫Referral1': r'''# ================= /JaiHo777 =================

EMOJI_WARN = "5447644880824181073"
EMOJI_EDIT = "5469741319330996757"
KEY = "JAIHO777_TEXT"
TAG = "jaiho"

admin_ids = [str(x) for x in (Bot.getData("AllBotAdminss") or [])]
admin_ids.append(str(Bot.getData("Owner")))
is_admin = str(u) in admin_ids

text = Bot.getData(KEY) or ""
media = Bot.getData(KEY + "_MEDIA")


def send_media(mt, chat, fid, cap, pm, mk):
    kw = {"chat_id": str(chat)}
    if cap:
        kw["caption"] = cap
    if pm:
        kw["parse_mode"] = pm
    if mk:
        kw["reply_markup"] = mk
    if mt == "photo":
        kw["photo"] = fid
        return bot.sendPhoto(**kw)
    if mt == "video":
        kw["video"] = fid
        return bot.sendVideo(**kw)
    if mt == "animation":
        kw["animation"] = fid
        return bot.sendAnimation(**kw)
    if mt == "document":
        kw["document"] = fid
        return bot.sendDocument(**kw)
    if mt == "audio":
        kw["audio"] = fid
        return bot.sendAudio(**kw)
    if mt == "voice":
        kw["voice"] = fid
        return bot.sendVoice(**kw)
    raise Exception("unknown media type")


def send_content(chat, text, media, mk):
    mt = None
    fid = None
    if isinstance(media, dict):
        mt = media.get("type")
        fid = media.get("file_id")
    for attempt in range(3):
        pm = "HTML"
        m2 = mk
        if attempt == 1:
            m2 = None
        if attempt == 2:
            pm = None
            m2 = None
        try:
            if mt and fid:
                send_media(mt, chat, fid, text, pm, m2)
            elif pm:
                if m2:
                    bot.replyText(chat, text, parse_mode=pm, reply_markup=m2)
                else:
                    bot.replyText(chat, text, parse_mode=pm)
            else:
                bot.replyText(chat, text)
            return True
        except Exception:
            pass
    return False


admin_markup = {
    "inline_keyboard": [
        [
            {"text": "Edit This Text", "callback_data": "/EditBtnTxtStart " + TAG, "icon_custom_emoji_id": EMOJI_EDIT}
        ]
    ]
}

has_media = isinstance(media, dict) and media.get("file_id")

if not text.strip() and not has_media:
    if is_admin:
        send_content(u, f'<tg-emoji emoji-id="{EMOJI_WARN}">⚠️</tg-emoji> <b>No text added yet.</b>\nTap below to add it.', None, admin_markup)
    else:
        send_content(u, f'<tg-emoji emoji-id="{EMOJI_WARN}">⚠️</tg-emoji> <b>This section is being updated. Please check back soon.</b>', None, None)
    raise ReturnCommand

if is_admin:
    send_content(u, text, media, admin_markup)
else:
    send_content(u, text, media, None)''',
    '💴 Account1': r'''# ================= /Yono777 =================

EMOJI_WARN = "5447644880824181073"
EMOJI_EDIT = "5469741319330996757"
KEY = "YONO777_TEXT"
TAG = "yono"

admin_ids = [str(x) for x in (Bot.getData("AllBotAdminss") or [])]
admin_ids.append(str(Bot.getData("Owner")))
is_admin = str(u) in admin_ids

text = Bot.getData(KEY) or ""
media = Bot.getData(KEY + "_MEDIA")


def send_media(mt, chat, fid, cap, pm, mk):
    kw = {"chat_id": str(chat)}
    if cap:
        kw["caption"] = cap
    if pm:
        kw["parse_mode"] = pm
    if mk:
        kw["reply_markup"] = mk
    if mt == "photo":
        kw["photo"] = fid
        return bot.sendPhoto(**kw)
    if mt == "video":
        kw["video"] = fid
        return bot.sendVideo(**kw)
    if mt == "animation":
        kw["animation"] = fid
        return bot.sendAnimation(**kw)
    if mt == "document":
        kw["document"] = fid
        return bot.sendDocument(**kw)
    if mt == "audio":
        kw["audio"] = fid
        return bot.sendAudio(**kw)
    if mt == "voice":
        kw["voice"] = fid
        return bot.sendVoice(**kw)
    raise Exception("unknown media type")


def send_content(chat, text, media, mk):
    mt = None
    fid = None
    if isinstance(media, dict):
        mt = media.get("type")
        fid = media.get("file_id")
    for attempt in range(3):
        pm = "HTML"
        m2 = mk
        if attempt == 1:
            m2 = None
        if attempt == 2:
            pm = None
            m2 = None
        try:
            if mt and fid:
                send_media(mt, chat, fid, text, pm, m2)
            elif pm:
                if m2:
                    bot.replyText(chat, text, parse_mode=pm, reply_markup=m2)
                else:
                    bot.replyText(chat, text, parse_mode=pm)
            else:
                bot.replyText(chat, text)
            return True
        except Exception:
            pass
    return False


admin_markup = {
    "inline_keyboard": [
        [
            {"text": "Edit This Text", "callback_data": "/EditBtnTxtStart " + TAG, "icon_custom_emoji_id": EMOJI_EDIT}
        ]
    ]
}

has_media = isinstance(media, dict) and media.get("file_id")

if not text.strip() and not has_media:
    if is_admin:
        send_content(u, f'<tg-emoji emoji-id="{EMOJI_WARN}">⚠️</tg-emoji> <b>No text added yet.</b>\nTap below to add it.', None, admin_markup)
    else:
        send_content(u, f'<tg-emoji emoji-id="{EMOJI_WARN}">⚠️</tg-emoji> <b>This section is being updated. Please check back soon.</b>', None, None)
    raise ReturnCommand

if is_admin:
    send_content(u, text, media, admin_markup)
else:
    send_content(u, text, media, None)''',
    '📊 Statistics1': r'''# 19'4'25   00'11'10  v:1'0'0 [√]
# CODED BY: @Owner_Here 
# [ DM TO BUY ANY BOTS & CODES ]

BotMode=Bot.getData("BotMode")
if BotMode=="OFF":
    bot.replyText(u, "<b>🙇‍♂️ Bot Is Currently Off</b>")
    raise ReturnCommand()


Tusrs=Bot.getData('total_users') or 0

wit=libs.Resources.globalRes('BotWithdraws').value()

FulUsrs=Bot.getData("FulBotUsrs") or []
T_verifUsrs=Bot.getData('T_verifUsrs') or 0


markup = InlineKeyboardMarkup()

markup.row(
    InlineKeyboardButton(
        text="⬅️ Back",
        callback_data="/RawStats"
    )
)


bot.editMessageText(
    chat_id=u,
    message_id=call.message.message_id,
    text=f"""<b>👍Total Members In Bot: {len(FulUsrs)} Users

👍Total Verified Users In Bot : {T_verifUsrs} Users

👍Total Payouts In Bot : {wit} INR

📍 Powered By - @Jenish_Dobariya1

</b>""",
    parse_mode="html",
    reply_markup=markup
)''',
    '🟢 Joined': r'''EMOJI_FAILED = "6129840374971112593"
EMOJI_PIN = "6129434968713076807"
EMOJI_SUCCESS = "6082398290773546707"
EMOJI_HEART = "5388790256772331442"
# --- JOINING CHECK (page-wise, always starts from page 1, FAST: cached for 1 minute only) ---

PIRO_ltmg = User.getData("PIRO_ltmg")
try:
    bot.deleteMessage(message.chat.id, PIRO_ltmg)
except:
    pass
PIRO_ltmg2 = User.getData("PIRO_ltmg2")
try:
    bot.deleteMessage(message.chat.id, PIRO_ltmg2)
except:
    pass

usr = f"""<a href="tg://user?id={str(u)}">{message.from_user.first_name}</a>"""

BotAdmins = Bot.getData("AllBotAdminss") or []
BOTID = int(Bot.info().token.split(":")[0])

AMC = Bot.getData("AllMainCh") or []
AMCU = Bot.getData("AllMainChUsernm") or []
AMCL = Bot.getData("AllMainChlink") or []
AMCTGID = Bot.getData("AllMainChTGID") or []
AMCO = Bot.getData("AllMainChOptional") or []
AMCP = Bot.getData("AllMainChPage") or []
for _pad in range(len(AMCTGID) - len(AMCP)):
    AMCP.append(1)
for _pad in range(len(AMCTGID) - len(AMC)):
    AMC.append("Channel")
for _pad in range(len(AMCTGID) - len(AMCU)):
    AMCU.append("Unknown")
for _pad in range(len(AMCTGID) - len(AMCL)):
    AMCL.append("https://t.me/" + str(AMCTGID[len(AMCL)]))

ALL_PAGES = sorted(set(AMCP)) if AMCTGID else []
CurPage = ALL_PAGES[0] if ALL_PAGES else 1

# ---- verified-channel cache (valid for the current MINUTE only) ----
_now = libs.DateAndTime.now("Asia/Kolkata")
Stamp = str(_now["date"]) + " " + str(_now["time"])[:5]
OKids = []
try:
    _c = User.getData("PIRO_OK")
    if _c and _c.get("s") == Stamp:
        OKids = list(_c.get("ids") or [])
except:
    OKids = []

joinSTAT = "JOIND"
PIRO_NJCL = []
FoundIdx = None
CacheDirty = False

for PageIdx in range(0, len(ALL_PAGES)):
    ThisPage = ALL_PAGES[PageIdx]
    NotJoinedThisPage = []

    IND = 0
    for i in AMCTGID:
        if AMCP[IND] != ThisPage:
            IND += 1
            continue

        sid = str(i)
        if sid in OKids:
            IND += 1
            continue

        UC = "error"
        ERR = ""
        try:
            UC = bot.getChatMember(i, u).status
        except Exception as e:
            UC = "error"
            ERR = str(e)[:150]

        if UC == "member" or UC == "administrator" or UC == "creator":
            OKids.append(sid)
            CacheDirty = True

        elif UC == "left" or UC == "kicked":
            ReqRec = Bot.getData(sid + "JoinRRof" + str(u)) or ""
            JR = Bot.getData(sid + "JoinReq") or "Y"
            if ReqRec != "Y" and JR == "Y" and (not AMCO or IND >= len(AMCO) or AMCO[IND] != "O"):
                NotJoinedThisPage.append(sid)

        elif UC == "error":
            BotIsAdm = False
            try:
                BS = bot.getChatMember(i, BOTID).status
                if BS == "administrator" or BS == "creator":
                    BotIsAdm = True
            except:
                BotIsAdm = False

            if not BotIsAdm:
                if Bot.getData("PIRO_NAsent" + sid) != "Y":
                    Bot.saveData("PIRO_NAsent" + sid, "Y")
                    for admin in BotAdmins:
                        try:
                            bot.replyText(admin, f"<b>👀 Bot Is Not Admin In {AMCU[IND]}! ⚠️\n\n✨ Only admins see this reminder (sent once per channel). 😊</b>")
                        except:
                            pass
            else:
                Bot.deleteData("PIRO_NAsent" + sid)
                if Bot.getData("PIRO_ERRsent" + sid) != "Y":
                    Bot.saveData("PIRO_ERRsent" + sid, "Y")
                    for admin in BotAdmins:
                        try:
                            bot.replyText(admin, f"<b>ℹ️ Bot IS admin in {AMCU[IND]}, but a member check failed:</b>\n<code>{ERR}</code>\n<i>(sent once per channel, only admins see this)</i>")
                        except:
                            pass

        IND += 1

    if NotJoinedThisPage:
        joinSTAT = "NOTJOIN"
        PIRO_NJCL = NotJoinedThisPage
        FoundIdx = PageIdx
        break

if CacheDirty:
    User.saveData("PIRO_OK", {"s": Stamp, "ids": OKids})

if FoundIdx is not None:
    CurPage = ALL_PAGES[FoundIdx]
elif ALL_PAGES:
    CurPage = ALL_PAGES[-1]

User.saveData("PIRO_CurPage", CurPage)

if joinSTAT == "JOIND":
    Bot.runCommand("/PIRO_MainMenu")
    raise ReturnCommand()

User.saveData("PIRO_NJCL", PIRO_NJCL)

JoinBtnText = Bot.getData("PIRO_JoinBtnText") or "Join"
ClaimBtnText = Bot.getData("PIRO_ClaimBtnText") or "🔒 Claim"
JoinIcon = Bot.getData("PIRO_JoinBtnEmojiId")
ClaimIcon = Bot.getData("PIRO_ClaimBtnEmojiId")

keyboard = []
row = []
IND = 0
for i in AMCTGID:
    if str(i) in PIRO_NJCL:
        btn = {"text": JoinBtnText, "url": AMCL[IND], "style": "primary"}
        if JoinIcon:
            btn["icon_custom_emoji_id"] = JoinIcon
        row.append(btn)
        if len(row) == 2:
            keyboard.append(row)
            row = []
    IND += 1
if row:
    keyboard.append(row)

SocialLinks = Bot.getData("AllSocialLinks") or []
if SocialLinks:
    row = []
    for s in SocialLinks:
        row.append({"text": f"{s['name']}", "url": s['url'], "style": "primary"})
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)

cbtn = {"text": ClaimBtnText, "callback_data": "🟢 Joined", "style": "success"}
if ClaimIcon:
    cbtn["icon_custom_emoji_id"] = ClaimIcon
keyboard.append([cbtn])

JoinMsgChatId = Bot.getData("PIRO_JoinMsgChatId")
JoinMsgId = Bot.getData("PIRO_JoinMsgId")

SentOk = False
if JoinMsgChatId and JoinMsgId:
    try:
        Sent = bot.copyMessage(chat_id=u, from_chat_id=JoinMsgChatId, message_id=JoinMsgId, reply_markup={"inline_keyboard": keyboard})
        try:
            Mid = Sent.message_id
        except:
            Mid = Sent["message_id"]
        User.saveData("PIRO_ltmg", Mid)
        SentOk = True
    except:
        SentOk = False

if not SentOk:
    msg = bot.sendMessage(
    f"""<tg-emoji emoji-id="{EMOJI_HEART}">👋🏻</tg-emoji><b>Hey There {usr} Welcome To Wallet Bot !

<tg-emoji emoji-id="{EMOJI_PIN}">🛑</tg-emoji> Must Join All Channels To Use Our Bot

<tg-emoji emoji-id="{EMOJI_SUCCESS}">🚫</tg-emoji> After Joining Click Claim</b>""",
        reply_markup={"inline_keyboard": keyboard},
        disable_web_page_preview=True
    )

    User.saveData("PIRO_ltmg", msg["message_id"])

Ex1 = Bot.getData("PIRO_JoinMsg2ChatId")
Ex2 = Bot.getData("PIRO_JoinMsg2Id")
if Ex1 and Ex2:
    try:
        S2 = bot.copyMessage(chat_id=u, from_chat_id=Ex1, message_id=Ex2)
        try:
            M2 = S2.message_id
        except:
            M2 = S2["message_id"]
        User.saveData("PIRO_ltmg2", M2)
    except:
        pass''',
}

def seed_builtin(bot_id, overwrite=True):
    """Copy the built-in commands into a bot (they stay editable in the panel)."""
    verb = "INSERT OR REPLACE" if overwrite else "INSERT OR IGNORE"
    with LOCK:
        c = db()
        try:
            c.executemany(f"{verb} INTO commands VALUES(?,?,?,?)", [(bot_id, n, code, now()) for n, code in BUILTIN_COMMANDS.items()])
            c.commit()
        finally:
            c.close()
    return len(BUILTIN_COMMANDS)

@app.post("/bot/<int:bid>/commands/builtin")
def cmds_builtin(bid):
    bot_or_404(bid); flash(f"Loaded {seed_builtin(bid)} built-in commands"); return redirect(f"/bot/{bid}")

@app.route("/health")
def health(): return "ok"

def register_env_bots():
    """BOT_TOKENS=tok1,tok2 -> auto-add + start on every boot (needed on free hosts with ephemeral disk)."""
    for t in filter(None, (x.strip() for x in os.environ.get("BOT_TOKENS", "").split(","))):
        try:
            r = q("SELECT id FROM bots WHERE token=?", (t,), one=True)
            if not r:
                info = tg_call(t, "getMe", {})
                if not info.get("ok"): print("BOT_TOKENS: invalid token skipped"); continue
                q("INSERT INTO bots(name,username,token,created) VALUES(?,?,?,?)", (info["result"]["first_name"], info["result"]["username"], t, now()), commit=True)
                r = q("SELECT id FROM bots WHERE token=?", (t,), one=True)
                seed_builtin(r["id"])
            start_bot(r["id"])
        except Exception as e: print("BOT_TOKENS error:", e)


# ---------------------------------------------------------------- boot
init_db(); ensure_admin()
for _r in q("SELECT id FROM bots WHERE running=1"): start_bot(_r["id"])
threading.Thread(target=register_env_bots, daemon=True).start()  # never block web boot on Telegram

if __name__ == "__main__":
    app.run(host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", 8000)), threaded=True)
