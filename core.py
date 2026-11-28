import os, re, json, sqlite3, threading, subprocess, sys, time, queue, gzip, secrets as _secrets
from concurrent.futures import ThreadPoolExecutor
import requests
from requests.adapters import HTTPAdapter

BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("PANEL_DB", os.path.join(BASE, "panel.db"))
SANDBOX = os.path.join(BASE, "sandbox.py")
EXEC_TIMEOUT = int(os.environ.get("EXEC_TIMEOUT", "10"))
WORKERS = int(os.environ.get("WORKERS", "4"))
TRASH_DAYS = int(os.environ.get("TRASH_DAYS", "30"))
TG_BASE = os.environ.get("TG_API_BASE", "https://api.telegram.org").rstrip("/")

SCHEMA = """
CREATE TABLE IF NOT EXISTS bots(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, username TEXT,
  token TEXT UNIQUE, running INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS commands(id INTEGER PRIMARY KEY AUTOINCREMENT, bot_id INTEGER,
  name TEXT, code TEXT, UNIQUE(bot_id, name));
CREATE TABLE IF NOT EXISTS kv(bot_id INTEGER, scope TEXT, k TEXT, v TEXT, PRIMARY KEY(bot_id, scope, k));
CREATE TABLE IF NOT EXISTS pending(bot_id INTEGER, user_id INTEGER, command TEXT, PRIMARY KEY(bot_id, user_id));
CREATE TABLE IF NOT EXISTS users(bot_id INTEGER, user_id INTEGER, PRIMARY KEY(bot_id, user_id));
CREATE TABLE IF NOT EXISTS broadcasts(id INTEGER PRIMARY KEY AUTOINCREMENT, bot_id INTEGER, ts REAL,
  text TEXT, total INTEGER, sent INTEGER, failed INTEGER, status TEXT);
CREATE TABLE IF NOT EXISTS errors(id INTEGER PRIMARY KEY AUTOINCREMENT, bot_id INTEGER, ts REAL,
  command TEXT, message TEXT, line INTEGER, level TEXT);
CREATE TABLE IF NOT EXISTS env(bot_id INTEGER, k TEXT, v TEXT, raw INTEGER DEFAULT 0, PRIMARY KEY(bot_id, k));
CREATE TABLE IF NOT EXISTS folders(bot_id INTEGER, name TEXT, PRIMARY KEY(bot_id, name));
CREATE TABLE IF NOT EXISTS miniapps(bot_id INTEGER PRIMARY KEY, html TEXT);
CREATE TABLE IF NOT EXISTS settings(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS scheduled(id TEXT PRIMARY KEY, bot_id INTEGER, user_id INTEGER, chat_id INTEGER, run_at REAL,
  command TEXT, options TEXT, user_json TEXT);
CREATE TABLE IF NOT EXISTS bcjobs(id TEXT PRIMARY KEY, bot_id INTEGER, ts REAL, function TEXT, command TEXT, kwargs TEXT,
  callback_url TEXT, status TEXT, total INTEGER, sent INTEGER, failed INTEGER);
CREATE TABLE IF NOT EXISTS ctl(id INTEGER PRIMARY KEY AUTOINCREMENT, bot_id INTEGER, action TEXT);
CREATE TABLE IF NOT EXISTS trash_commands(id INTEGER PRIMARY KEY AUTOINCREMENT, bot_id INTEGER, name TEXT, code TEXT,
  folder TEXT, deleted_at REAL);
CREATE TABLE IF NOT EXISTS trash_bots(id INTEGER PRIMARY KEY, name TEXT, username TEXT, deleted_at REAL, n_commands INTEGER,
  n_users INTEGER, snapshot TEXT);
CREATE TABLE IF NOT EXISTS logs(id INTEGER PRIMARY KEY AUTOINCREMENT, bot_id INTEGER, ts REAL, level TEXT, text TEXT);
CREATE INDEX IF NOT EXISTS idx_users_bot ON users(bot_id, last_seen);
"""
MIGRATIONS = [
    "ALTER TABLE users ADD COLUMN first_name TEXT", "ALTER TABLE users ADD COLUMN username TEXT",
    "ALTER TABLE users ADD COLUMN first_seen REAL", "ALTER TABLE users ADD COLUMN last_seen REAL",
    "ALTER TABLE users ADD COLUMN blocked INTEGER DEFAULT 0",
    "ALTER TABLE bots ADD COLUMN translate INTEGER DEFAULT 0",
    "ALTER TABLE bots ADD COLUMN version TEXT DEFAULT '1.0.0'",
    "ALTER TABLE commands ADD COLUMN folder TEXT", "ALTER TABLE bots ADD COLUMN created_at REAL", "ALTER TABLE ctl ADD COLUMN args TEXT", "ALTER TABLE ctl ADD COLUMN result TEXT", "ALTER TABLE ctl ADD COLUMN done INTEGER DEFAULT 0", "ALTER TABLE bcjobs ADD COLUMN owner_bot INTEGER", "ALTER TABLE bcjobs ADD COLUMN code TEXT", "ALTER TABLE commands ADD COLUMN updated_at REAL", "ALTER TABLE pending ADD COLUMN options TEXT", "ALTER TABLE users ADD COLUMN banned INTEGER DEFAULT 0", "ALTER TABLE bcjobs ADD COLUMN speed REAL",
]


# ---------- database: one shared connection, WAL, autocommit ----------
_lock = threading.RLock()
_db = sqlite3.connect(DB_PATH, check_same_thread=False, isolation_level=None, timeout=30)
_db.row_factory = sqlite3.Row
_db.execute("PRAGMA journal_mode=WAL")
_db.execute("PRAGMA synchronous=NORMAL")


def run(sql, args=()):
    with _lock:
        return _db.execute(sql, args).lastrowid


def rows(sql, args=()):
    with _lock:
        return [dict(r) for r in _db.execute(sql, args).fetchall()]


def one(sql, args=()):
    r = rows(sql, args)
    return r[0] if r else None


def init_db():
    with _lock:
        _db.executescript(SCHEMA.replace("CREATE INDEX IF NOT EXISTS idx_users_bot ON users(bot_id, last_seen);", ""))
    for sql in MIGRATIONS:
        try:
            run(sql)
        except sqlite3.OperationalError:
            pass
    run("CREATE INDEX IF NOT EXISTS idx_users_bot ON users(bot_id, last_seen)")
    run("UPDATE bots SET created_at=? WHERE created_at IS NULL", (time.time(),))
    run("CREATE TRIGGER IF NOT EXISTS bots_created AFTER INSERT ON bots WHEN NEW.created_at IS NULL "
        "BEGIN UPDATE bots SET created_at=CAST(strftime('%s','now') AS REAL) WHERE id=NEW.id; END")


# ---------- Telegram (keep-alive connection pool) ----------
SESSION = requests.Session()
SESSION.mount("https://", HTTPAdapter(pool_connections=8, pool_maxsize=64))


def tg(token, method, **params):
    r = SESSION.post(f"{TG_BASE}/bot{token}/{method}", json=params, timeout=40)
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(data.get("description", "Telegram error"))
    return data["result"]


def log(bot_id, level, text):
    run("INSERT INTO logs(bot_id, ts, level, text) VALUES(?,?,?,?)", (bot_id, time.time(), level, str(text)[:3000]))
    run("DELETE FROM logs WHERE bot_id=? AND id<(SELECT MIN(id) FROM (SELECT id FROM logs WHERE bot_id=? "
        "ORDER BY id DESC LIMIT 500))", (bot_id, bot_id))


def record_error(bot_id, command, message, line=None):
    run("INSERT INTO errors(bot_id, ts, command, message, line, level) VALUES(?,?,?,?,?,'ERROR')",
        (bot_id, time.time(), command, str(message)[:6000], line))
    run("DELETE FROM errors WHERE bot_id=? AND id<(SELECT MIN(id) FROM (SELECT id FROM errors WHERE bot_id=? "
        "ORDER BY id DESC LIMIT 200))", (bot_id, bot_id))


# ---------- users ----------
def touch_user(bid, u):
    now = time.time()
    run("INSERT INTO users(bot_id,user_id,first_name,username,first_seen,last_seen,blocked) VALUES(?,?,?,?,?,?,0) "
        "ON CONFLICT(bot_id,user_id) DO UPDATE SET first_name=COALESCE(excluded.first_name,first_name), "
        "username=COALESCE(excluded.username,username), last_seen=excluded.last_seen, blocked=0, "
        "first_seen=COALESCE(first_seen,excluded.first_seen)",
        (bid, u["id"], u.get("first_name"), u.get("username"), now, now))


def _is_blocked(err):
    e = str(err).lower()
    return "blocked" in e or "deactivated" in e or "chat not found" in e


def _fix(text, parse_mode):
    if parse_mode == "HTML":
        from sandbox import fix_html
        return fix_html(text)
    return text


def send_broadcast(bot, text, parse_mode="HTML"):
    uids = [r["user_id"] for r in rows("SELECT user_id FROM users WHERE bot_id=? AND COALESCE(blocked,0)=0", (bot["id"],))]
    bc = run("INSERT INTO broadcasts(bot_id,ts,text,total,sent,failed,status) VALUES(?,?,?,?,0,0,'sending')",
             (bot["id"], time.time(), text, len(uids)))

    def work():
        s = f = 0
        for uid in uids:
            try:
                tg(bot["token"], "sendMessage", chat_id=uid, text=_fix(text, parse_mode), parse_mode=parse_mode)
                s += 1
            except Exception as e:
                f += 1
                if _is_blocked(e):
                    run("UPDATE users SET blocked=1 WHERE bot_id=? AND user_id=?", (bot["id"], uid))
            if (s + f) % 10 == 0:
                run("UPDATE broadcasts SET sent=?, failed=? WHERE id=?", (s, f, bc))
            time.sleep(0.04)
        run("UPDATE broadcasts SET sent=?, failed=?, status='done' WHERE id=?", (s, f, bc))
    threading.Thread(target=work, daemon=True).start()
    return bc


def import_users(bot, ids):
    def work():
        ok = bad = 0
        for uid in ids:
            try:
                tg(bot["token"], "sendChatAction", chat_id=uid, action="typing")
                touch_user(bot["id"], {"id": uid})
                ok += 1
            except Exception:
                bad += 1
            time.sleep(0.1)
        log(bot["id"], "info", f"User import finished: {ok} added, {bad} could not be reached")
    threading.Thread(target=work, daemon=True).start()


# ---------- storage / env ----------
def kv_all(bot_id, scope):
    return {r["k"]: json.loads(r["v"]) for r in rows("SELECT k, v FROM kv WHERE bot_id=? AND scope=?", (bot_id, scope))}


def kv_apply(bot_id, scope, changes):
    for k, v in (changes or {}).items():
        if v is None:
            run("DELETE FROM kv WHERE bot_id=? AND scope=? AND k=?", (bot_id, scope, k))
        else:
            run("INSERT OR REPLACE INTO kv(bot_id,scope,k,v) VALUES(?,?,?,?)", (bot_id, scope, k, json.dumps(v, default=str)))


def env_for(bot_id):
    out = {}
    for r in rows("SELECT k, v, raw FROM env WHERE bot_id=?", (bot_id,)):
        try:
            out[r["k"]] = json.loads(r["v"]) if r["raw"] else r["v"]
        except Exception:
            out[r["k"]] = r["v"]
    return out


# ---------- command matching: "hii", "/hii" and "Link Wallet" all work ----------
def nk(s):
    """Match key: case-insensitive, one leading '/' ignored ('//0' stays different from '0')."""
    s = re.sub(r"\s+", " ", (s or "").strip().lower())
    return s[1:].strip() if s.startswith("/") else s


def nkc(s):
    """Like nk() but case-sensitive: /BROADCAST and /broadcast can both exist."""
    s = re.sub(r"\s+", " ", (s or "").strip())
    return s[1:].strip() if s.startswith("/") else s


def nraw(s):
    return re.sub(r"\s+", " ", (s or "").strip())


class Index(dict):
    """Lookup order: exactly as typed ('/Bonus' vs 'Bonus'), then without the slash, then ignoring case."""
    def __init__(self, items):
        super().__init__()
        self.slashless, self.lower = {}, {}
        for name, cid in items:
            self[nraw(name)] = cid
            self.slashless.setdefault(nkc(name), cid)
            self.lower.setdefault(nk(name), cid)

    def lookup(self, text):
        t = nraw(text)
        return self.get(t) or self.slashless.get(nkc(t)) or self.lower.get(nk(t))


_index = {}


def cmd_index(bid):
    idx = _index.get(bid)
    if idx is None:
        idx = _index[bid] = Index((r["name"], r["id"]) for r in rows("SELECT id, name FROM commands WHERE bot_id=?", (bid,)))
    return idx


def invalidate(bid):
    _index.pop(bid, None)


def resolve(bid, text):
    """Longest-prefix match on words, ignoring a leading '/', else the '*' command."""
    idx, words = cmd_index(bid), text.split()
    if words:
        if words[0].startswith("/"):
            words[0] = words[0].split("@")[0]
        for k in range(len(words), 0, -1):
            cid = idx.lookup(" ".join(words[:k]))
            if cid:
                return cid, words[k:]
    return idx.get("*"), words


# ---------- warm worker pool ----------
class Worker:
    def __init__(self):
        self.jobs = 0
        self.p = subprocess.Popen([sys.executable, SANDBOX], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL, text=True, encoding="utf-8", bufsize=1)

    def call(self, job, timeout):
        killed = []

        def kill():
            killed.append(1)
            self.p.kill()
        t = threading.Timer(timeout, kill)
        t.start()
        try:
            self.p.stdin.write(json.dumps(job) + "\n")
            self.p.stdin.flush()
            line = self.p.stdout.readline()
        except (BrokenPipeError, OSError, ValueError):
            line = ""
        finally:
            t.cancel()
        if killed:
            raise TimeoutError
        if not line:
            raise RuntimeError("The command stopped the worker process (for example with os._exit).")
        self.jobs += 1
        return json.loads(line)

    def close(self):
        try:
            self.p.kill()
        except Exception:
            pass


_pool = queue.Queue()
for _ in range(WORKERS):
    _pool.put(None)          # None = start a worker on first use


def warm_up():
    for _ in range(min(2, WORKERS)):
        w = _pool.get()
        _pool.put(w or Worker())


EMPTY = {"actions": [], "pending": None, "scheduled": [], "stdout": "", "errors": [], "error": None, "error_line": None}


def get_setting(k, default=None):
    r = one("SELECT v FROM settings WHERE k=?", (k,))
    return r["v"] if r else default


def set_setting(k, v):
    run("INSERT OR REPLACE INTO settings(k,v) VALUES(?,?)", (k, v))


def secret():
    s = get_setting("hook_secret")
    if not s:
        import secrets as _s
        s = _s.token_hex(24)
        set_setting("hook_secret", s)
    return s


def base_url():
    return os.environ.get("PUBLIC_URL") or get_setting("base_url") or "http://localhost:5000"


def execute(bot, cmd, ctx, dry=False, gate=False, options=None):
    job = {"cmd": {"name": cmd["name"], "code": cmd["code"]}, "ctx": ctx, "dry": dry, "gate": gate, "options": options,
           "db_path": DB_PATH, "token": bot["token"], "env": env_for(bot["id"]), "base_url": base_url(), "secret": secret(),
           "trash_days": TRASH_DAYS,
           "bot": {"id": bot["id"], "username": bot.get("username"), "name": bot.get("name")}}
    w = _pool.get()
    try:
        if w is None or w.p.poll() is not None or w.jobs >= 500:
            if w:
                w.close()
            w = Worker()
        return {**EMPTY, **w.call(job, EXEC_TIMEOUT)}
    except TimeoutError:
        w.close(); w = None
        msg = f"Stopped: the command ran longer than {EXEC_TIMEOUT} seconds."
        return {**EMPTY, "error": msg, "errors": [{"command": cmd["name"], "message": msg, "short": msg, "line": None}]}
    except Exception as e:
        if w:
            w.close()
        w = None
        return {**EMPTY, "error": str(e), "errors": [{"command": cmd["name"], "message": str(e), "line": None}]}
    finally:
        _pool.put(w)


def apply_result(bot, res):
    bid = bot["id"]
    for e in res.get("errors", []):
        record_error(bid, e.get("command", ""), e.get("message", ""), e.get("line"))
    if res.get("stdout"):
        log(bid, "print", res["stdout"].rstrip())


def make_ctx(utype, obj, user, chat, text, args, data=None, cq_id=None, message_id=None):
    return {"update_type": utype, "user": user, "chat": chat, "message": obj, "text": text, "args": args,
            "data": data, "callback_query_id": cq_id, "message_id": message_id}


def handle_update(bot, upd):
    bid = bot["id"]
    utype = next((k for k in upd if k != "update_id"), None)
    if not utype:
        return
    obj, idx, options, cid, args = upd[utype], cmd_index(bid), None, None, []
    if utype == "callback_query":
        msg = obj.get("message") or {"chat": {"id": obj["from"]["id"], "type": "private"}}
        user, chat, data = obj["from"], msg["chat"], obj.get("data", "")
        cid = idx.lookup("cb:" + data) or idx.get("cb:*")
        if cid:
            args = data.split()
        else:
            cid, args = resolve(bid, data)
        m = dict(obj, chat=chat, message_id=msg.get("message_id"), message=msg)
        ctx = make_ctx(utype, m, user, chat, data, args, data, obj["id"], msg.get("message_id"))
    elif utype == "message":
        if not obj.get("from"):
            return
        user, chat = obj["from"], obj["chat"]
        text = obj.get("text") or obj.get("caption") or ""
        pend = one("SELECT command, options FROM pending WHERE bot_id=? AND user_id=?", (bid, user["id"]))
        if pend:
            run("DELETE FROM pending WHERE bot_id=? AND user_id=?", (bid, user["id"]))
        if pend and not text.startswith("/"):
            cid, args = idx.lookup(pend["command"]), text.split()
            options = json.loads(pend["options"]) if pend.get("options") else None
        else:
            cid, args = resolve(bid, text)
        ctx = make_ctx(utype, obj, user, chat, text, args, None, None, obj.get("message_id"))
    else:
        user = obj.get("from") or ({"id": obj["chat"]["id"]} if obj.get("chat") else None)
        if not user:
            return
        chat = obj.get("chat") or {"id": user["id"], "type": "private"}
        if utype == "my_chat_member" and chat.get("type") == "private" and obj["new_chat_member"]["status"] == "kicked":
            run("UPDATE users SET blocked=1 WHERE bot_id=? AND user_id=?", (bid, user["id"]))
        cid = idx.lookup("handler_" + utype) or idx.lookup("handler_special_updates")
        ctx = make_ctx(utype, dict(obj), user, chat, "", [], None, None, obj.get("message_id"))
    is_cb = utype == "callback_query"

    def answer_cb():
        if is_cb:
            try:
                tg(bot["token"], "answerCallbackQuery", callback_query_id=obj["id"])
            except Exception:
                pass
    if one("SELECT 1 FROM users WHERE bot_id=? AND user_id=? AND banned=1", (bid, user["id"])):
        return answer_cb()
    if utype in ("message", "callback_query") and chat.get("type") == "private":
        touch_user(bid, user)
    if not cid:
        return answer_cb()
    cmd = one("SELECT id, name, code FROM commands WHERE id=?", (cid,))
    if not cmd:
        return answer_cb()
    res = execute(bot, cmd, ctx, gate=True, options=options)
    apply_result(bot, res)
    notify_errors(bot, res, ctx)
    if not res.get("answered"):        # worker crashed or timed out before answering the button
        answer_cb()


ERR_DEFAULT = {"send_alert": True, "alert_token": "", "alert_chat": "", "owner_ids": [], "show_owner": True,
               "show_user": True, "user_msg": "⚠️ There is some error. Please contact @Jenish_Dobariya1"}
_SEEN = {}


def err_cfg():
    d = dict(ERR_DEFAULT)
    try:
        d.update(json.loads(get_setting("error_alerts") or "{}"))
    except Exception:
        pass
    return d


def set_err_cfg(cfg):
    set_setting("error_alerts", json.dumps(cfg))


def _h(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _noise(short):
    s = (short or "").lower()
    return any(w in s for w in ("blocked by the user", "user is deactivated", "chat not found", "bot was kicked", "bot can't initiate"))


def _short_of(e):
    s = e.get("short")
    if s:
        return s
    lines = [x for x in str(e.get("message", "")).strip().splitlines() if x.strip()]
    s = lines[-1] if lines else "Unknown error"
    return s + (f" (line {e['line']})" if e.get("line") else "")


def send_error_alert(cfg, bot, e, user=None):
    """To the dedicated alert bot/chat, so one person sees the errors of every bot."""
    short = _short_of(e)
    tail = str(e.get("message", "")).strip()[-1800:]
    who = f"\nUser: <code>{user['id']}</code>{' @' + _h(user['username']) if user.get('username') else ''}" if user else ""
    text = (f"🚨 <b>Botbench error</b>\nBot: @{_h(bot.get('username'))} (id {bot['id']})\nCommand: <code>{_h(e.get('command') or '-')}</code>"
            f"{' · line ' + str(e['line']) if e.get('line') else ''}{who}\nTime: {time.strftime('%Y-%m-%d %H:%M:%S')} UTC\n\n"
            f"<code>{_h(short[:700])}</code>" + (f"\n\n<pre>{_h(tail)}</pre>" if tail and tail != short else ""))
    tg(cfg["alert_token"], "sendMessage", chat_id=str(cfg["alert_chat"]).strip(), text=text[:4000], parse_mode="HTML")


def _alert_job(cfg, bot, e, user):
    try:
        send_error_alert(cfg, bot, e, user)
        set_setting("error_alert_status", json.dumps({"ts": time.time(), "ok": True}))
    except Exception as ex:
        set_setting("error_alert_status", json.dumps({"ts": time.time(), "ok": False, "msg": str(ex)[:150]}))


def _chat_job(bot, chat_id, text, html):
    try:
        tg(bot["token"], "sendMessage", chat_id=chat_id, text=text, **({"parse_mode": "HTML"} if html else {}))
    except Exception:
        pass


def notify_errors(bot, res, ctx=None):
    errs = res.get("errors") or []
    if not errs:
        return
    cfg, now, e0 = err_cfg(), time.time(), errs[0]
    short = _short_of(e0)
    user = (ctx or {}).get("user")
    if cfg["send_alert"] and cfg["alert_token"] and str(cfg["alert_chat"]).strip() and not _noise(short):
        key = ("a", bot["id"], e0.get("command"), short[:120])
        if now - _SEEN.get(key, 0) > 300:          # the same error is sent at most every 5 minutes
            _SEEN[key] = now
            BG.submit(_alert_job, cfg, bot, e0, user)
    if ctx and (ctx.get("chat") or {}).get("type") == "private" and ctx.get("update_type") in ("message", "callback_query") \
            and not _noise(short):
        owners = {str(x) for x in cfg["owner_ids"]} | ({str(cfg["alert_chat"]).strip()} if cfg["alert_chat"] else set())
        uid = str(user["id"])
        key = ("c", bot["id"], uid, e0.get("command"))
        if now - _SEEN.get(key, 0) < 20:
            return
        if uid in owners and cfg["show_owner"]:
            _SEEN[key] = now
            BG.submit(_chat_job, bot, ctx["chat"]["id"],
                      f"<b>Botbench Error:</b>\n{_h(short)}\n\nCommand: <code>{_h(e0.get('command') or '-')}</code>", True)
        elif uid not in owners and cfg["show_user"] and not res.get("handled") and cfg["user_msg"].strip():
            _SEEN[key] = now
            BG.submit(_chat_job, bot, ctx["chat"]["id"], cfg["user_msg"], False)


def run_code_for_user(bot, code, uid, options=None):
    """Bot.broadcast(code=…): run a code string once for one user."""
    u = one("SELECT user_id id, first_name, username FROM users WHERE bot_id=? AND user_id=?", (bot["id"], uid)) or {"id": uid}
    chat = {"id": uid, "type": "private"}
    ctx = make_ctx("broadcast", {"message_id": 0, "from": u, "chat": chat, "text": ""}, u, chat, "", [], None, None, 0)
    res = execute(bot, {"name": "(broadcast code)", "code": code}, ctx, options=options)
    apply_result(bot, res)
    return res


def fire_hook(bot, command, uid, options=None, user_json=None):
    """Run a command outside a normal update: webhook, scheduled task, broadcast callback."""
    cid, args = resolve(bot["id"], command)
    cmd = one("SELECT id, name, code FROM commands WHERE id=?", (cid,)) if cid else None
    if not cmd:
        return None
    u = (json.loads(user_json) if user_json else None) or one(
        "SELECT user_id id, first_name, username FROM users WHERE bot_id=? AND user_id=?", (bot["id"], uid)) or {"id": uid}
    chat = {"id": uid, "type": "private"}
    msg = {"message_id": 0, "from": u, "chat": chat, "text": ""}
    ctx = make_ctx("webhook", msg, u, chat, "", args, None, None, 0)
    res = execute(bot, cmd, ctx, options=options)
    apply_result(bot, res)
    notify_errors(bot, res, ctx)
    return res


BOT_TABLES = ("commands", "kv", "pending", "users", "logs", "errors", "env", "folders", "miniapps", "broadcasts",
              "scheduled", "bcjobs")


def bot_snapshot(b, with_data=True, with_token=True):
    bid = b["id"]
    snap = {"id": bid, "name": b["name"], "username": b["username"], "version": b.get("version") or "1.0.0",
            "created_at": b.get("created_at"),
            "was_running": bool(b["running"]),
            "commands": [{"name": c["name"], "code": c["code"], "folder": c["folder"]} for c in
                         rows("SELECT name, code, folder FROM commands WHERE bot_id=? ORDER BY name", (bid,))],
            "folders": [r["name"] for r in rows("SELECT name FROM folders WHERE bot_id=?", (bid,))],
            "env": rows("SELECT k, v, raw FROM env WHERE bot_id=?", (bid,)),
            "miniapp": (one("SELECT html FROM miniapps WHERE bot_id=?", (bid,)) or {}).get("html")}
    if with_token:
        snap["token"] = b["token"]
    if with_data:
        snap["bot_data"] = kv_all(bid, "bot")
        snap["user_data"] = {}
        for r in rows("SELECT DISTINCT scope FROM kv WHERE bot_id=? AND scope LIKE 'user:%'", (bid,)):
            snap["user_data"][r["scope"][5:]] = kv_all(bid, r["scope"])
        snap["users"] = rows("SELECT user_id, first_name, username, first_seen, last_seen, blocked "
                             "FROM users WHERE bot_id=?", (bid,))
    return snap


def backup_doc(with_data=True, with_token=True):
    return {"botbench_backup": 1, "created": time.strftime("%Y-%m-%d %H:%M:%S"), "includes_data": with_data,
            "bots": [bot_snapshot(b, with_data, with_token) for b in rows("SELECT * FROM bots ORDER BY id")]}


def restore_snapshot(bid, s):
    """Write a snapshot into bot `bid`. Commands it replaces go to the recycle bin first."""
    if s.get("created_at"):
        run("UPDATE bots SET created_at=? WHERE id=?", (s["created_at"], bid))
    old = [r["id"] for r in rows("SELECT id FROM commands WHERE bot_id=?", (bid,))]
    if old:
        trash_commands(bid, old)
    now = time.time()
    for cm in s.get("commands", []):
        run("INSERT OR REPLACE INTO commands(bot_id, name, code, folder, updated_at) VALUES(?,?,?,?,?)",
            (bid, cm["name"], cm.get("code", ""), cm.get("folder"), now))
    for f in s.get("folders", []):
        run("INSERT OR IGNORE INTO folders(bot_id, name) VALUES(?,?)", (bid, f))
    run("DELETE FROM env WHERE bot_id=?", (bid,))
    for e in s.get("env", []):
        run("INSERT OR REPLACE INTO env(bot_id,k,v,raw) VALUES(?,?,?,?)", (bid, e["k"], e["v"], e.get("raw", 0)))
    if s.get("miniapp"):
        run("INSERT OR REPLACE INTO miniapps(bot_id, html) VALUES(?,?)", (bid, s["miniapp"]))
    kv_apply(bid, "bot", s.get("bot_data"))
    for uid, d in (s.get("user_data") or {}).items():
        kv_apply(bid, f"user:{uid}", d)
    for u in s.get("users", []):
        run("INSERT OR REPLACE INTO users(bot_id,user_id,first_name,username,first_seen,last_seen,blocked) VALUES(?,?,?,?,?,?,?)",
            (bid, u["user_id"], u.get("first_name"), u.get("username"), u.get("first_seen"), u.get("last_seen"), u.get("blocked") or 0))
    invalidate(bid)
    if s.get("was_running"):
        start_bot(bid)


def load_backup(raw):
    """bytes or text of a backup (JSON, optionally gzip) -> dict"""
    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    if raw[:15] == b"SQLite format 3":
        raise ValueError("That is a database file (.db). Restore needs a JSON backup; use the 'All bots with data' link instead.")
    doc = json.loads(raw.decode("utf-8"))
    if not isinstance(doc.get("bots"), list):
        raise ValueError("That isn't a Botbench backup file.")
    return doc


def restore_doc(doc):
    made = updated = skipped = 0
    for s in doc["bots"]:
        row = None
        if s.get("token"):
            row = one("SELECT * FROM bots WHERE token=?", (s["token"],))
        if not row and s.get("username"):
            row = one("SELECT * FROM bots WHERE username=?", (s["username"],))
        if not row:
            if not s.get("token"):
                skipped += 1
                continue
            want = s.get("id")           # keep the bot id from the backup when it is free
            if want and not one("SELECT 1 FROM bots WHERE id=?", (want,)) and not one("SELECT 1 FROM trash_bots WHERE id=?", (want,)):
                run("INSERT INTO bots(id, name, username, token, version) VALUES(?,?,?,?,?)",
                    (want, s.get("name"), s.get("username"), s["token"], s.get("version") or "1.0.0"))
                bid = want
            else:
                bid = run("INSERT INTO bots(name, username, token, version) VALUES(?,?,?,?)",
                          (s.get("name"), s.get("username"), s["token"], s.get("version") or "1.0.0"))
            made += 1
        else:
            bid = row["id"]
            updated += 1
        restore_snapshot(bid, s)
    return {"created": made, "updated": updated, "skipped": skipped}


# ----- recycle bins -----
def trash_commands(bot_id, ids):
    now, n = time.time(), 0
    for cid in ids:
        r = one("SELECT * FROM commands WHERE id=? AND bot_id=?", (cid, bot_id))
        if r:
            run("INSERT INTO trash_commands(bot_id,name,code,folder,deleted_at) VALUES(?,?,?,?,?)",
                (bot_id, r["name"], r["code"], r["folder"], now))
            run("DELETE FROM commands WHERE id=?", (cid,))
            n += 1
    invalidate(bot_id)
    return n


def restore_command(bot_id, trash_id):
    t = one("SELECT * FROM trash_commands WHERE id=? AND bot_id=?", (trash_id, bot_id))
    if not t:
        return None
    have = {nraw(r["name"]) for r in rows("SELECT name FROM commands WHERE bot_id=?", (bot_id,))}
    name, i = t["name"], 1
    while nraw(name) in have:
        i += 1
        name = f'{t["name"]} (restored{"" if i == 2 else " " + str(i - 1)})'
    run("INSERT INTO commands(bot_id,name,code,folder,updated_at) VALUES(?,?,?,?,?)",
        (bot_id, name, t["code"], t["folder"], time.time()))
    run("DELETE FROM trash_commands WHERE id=?", (trash_id,))
    invalidate(bot_id)
    return name


def trash_bot(bot_id):
    b = one("SELECT * FROM bots WHERE id=?", (bot_id,))
    if not b:
        return False
    snap = bot_snapshot(b, True, True)
    run("INSERT OR REPLACE INTO trash_bots(id,name,username,deleted_at,n_commands,n_users,snapshot) VALUES(?,?,?,?,?,?,?)",
        (bot_id, b["name"], b["username"], time.time(), len(snap["commands"]), len(snap.get("users", [])),
         json.dumps(snap, default=str, ensure_ascii=False)))
    stop_bot(bot_id)
    for t in BOT_TABLES:
        run(f"DELETE FROM {t} WHERE bot_id=?", (bot_id,))
    run("DELETE FROM bots WHERE id=?", (bot_id,))
    invalidate(bot_id)
    return True


def restore_trashed_bot(bot_id, new_token=None):
    t = one("SELECT * FROM trash_bots WHERE id=?", (bot_id,))
    if not t:
        raise ValueError("That bot is not in the recycle bin.")
    snap = json.loads(t["snapshot"])
    if new_token:
        tg(new_token, "getMe")
        snap["token"] = new_token.strip()
    other = one("SELECT id, username FROM bots WHERE token=?", (snap.get("token"),))
    if other:
        raise ValueError(f"Bot @{other['username']} (id {other['id']}) in the panel already uses this token.")
    free = not one("SELECT 1 FROM bots WHERE id=?", (bot_id,))
    if free:      # keep the same bot id
        bid = run("INSERT INTO bots(id, name, username, token, version) VALUES(?,?,?,?,?)",
                  (bot_id, snap["name"], snap["username"], snap["token"], snap.get("version") or "1.0.0"))
        bid = bot_id
    else:
        bid = run("INSERT INTO bots(name, username, token, version) VALUES(?,?,?,?)",
                  (snap["name"], snap["username"], snap["token"], snap.get("version") or "1.0.0"))
    restore_snapshot(bid, snap)
    run("DELETE FROM trash_bots WHERE id=?", (bot_id,))
    if bid != bot_id:
        run("UPDATE trash_commands SET bot_id=? WHERE bot_id=?", (bid, bot_id))
    return bid


def create_bot(token, name=None, username=None):
    token = (token or "").strip()
    other = one("SELECT id, username FROM bots WHERE token=?", (token,))
    if other:
        raise ValueError(f"That token is already used by bot @{other['username']} (id {other['id']}).")
    me = tg(token, "getMe")                      # raises with Telegram's own message for a bad token
    return run("INSERT INTO bots(name, username, token) VALUES(?,?,?)",
               (name or me.get("first_name"), username or me.get("username"), token))


def clone_bot(template_id, token):
    t = one("SELECT * FROM bots WHERE id=?", (template_id,))
    if not t:
        raise ValueError("The bot to clone was not found.")
    bid = create_bot(token)
    now = time.time()
    for cm in rows("SELECT name, code, folder FROM commands WHERE bot_id=?", (template_id,)):
        run("INSERT OR REPLACE INTO commands(bot_id,name,code,folder,updated_at) VALUES(?,?,?,?,?)",
            (bid, cm["name"], cm["code"], cm["folder"], now))
    for f in rows("SELECT name FROM folders WHERE bot_id=?", (template_id,)):
        run("INSERT OR IGNORE INTO folders(bot_id,name) VALUES(?,?)", (bid, f["name"]))
    for e in rows("SELECT k, v, raw FROM env WHERE bot_id=?", (template_id,)):
        run("INSERT OR REPLACE INTO env(bot_id,k,v,raw) VALUES(?,?,?,?)", (bid, e["k"], e["v"], e["raw"]))
    mini = one("SELECT html FROM miniapps WHERE bot_id=?", (template_id,))
    if mini:
        run("INSERT OR REPLACE INTO miniapps(bot_id, html) VALUES(?,?)", (bid, mini["html"]))
    invalidate(bid)
    start_bot(bid)
    return bid


def import_bot(commands, token):
    bid = create_bot(token)
    now = time.time()
    for cm in commands:
        run("INSERT OR REPLACE INTO commands(bot_id,name,code,updated_at) VALUES(?,?,?,?)", (bid, cm["name"], cm.get("code", ""), now))
    invalidate(bid)
    start_bot(bid)
    return bid


def replace_token(new_token):
    """Give an existing bot a new token (after @BotFather revoked the old one). The bot is found by its Telegram ID."""
    new_token = (new_token or "").strip()
    me = tg(new_token, "getMe")
    row = one("SELECT * FROM bots WHERE token LIKE ?", (new_token.split(":")[0] + ":%",))
    if not row:
        raise ValueError("No bot with that Telegram ID is in this panel.")
    was = is_running(row["id"])
    stop_bot(row["id"])
    run("UPDATE bots SET token=?, name=?, username=? WHERE id=?", (new_token, me.get("first_name"), me.get("username"), row["id"]))
    if was:
        time.sleep(0.3)
        start_bot(row["id"])
    return row["id"], me.get("username")


def purge_trashed_bot(bot_id):
    run("DELETE FROM trash_bots WHERE id=?", (bot_id,))
    run("DELETE FROM trash_commands WHERE bot_id=?", (bot_id,))


def cleanup_trash():
    cut = time.time() - TRASH_DAYS * 86400
    run("DELETE FROM trash_commands WHERE deleted_at<?", (cut,))
    for r in rows("SELECT id FROM trash_bots WHERE deleted_at<?", (cut,)):
        purge_trashed_bot(r["id"])


# ----- private backup links (the "database URL") -----
def backup_token(regenerate=False):
    t = get_setting("backup_token")
    if regenerate or not t:
        t = _secrets.token_urlsafe(24)
        set_setting("backup_token", t)
    return t


# ----- automatic backup to a Telegram chat (free, off-site) -----
def auto_cfg():
    d = {"enabled": False, "bot_id": None, "chat_id": "", "every_hours": 24, "with_data": True, "last_ts": 0, "last_status": ""}
    try:
        d.update(json.loads(get_setting("autobackup") or "{}"))
    except Exception:
        pass
    return d


def set_auto(cfg):
    set_setting("autobackup", json.dumps(cfg))


def tg_send_file(token, chat_id, filename, data, caption=""):
    r = SESSION.post(f"{TG_BASE}/bot{token}/sendDocument", data={"chat_id": chat_id, "caption": caption},
                     files={"document": (filename, data)}, timeout=180)
    j = r.json()
    if not j.get("ok"):
        raise RuntimeError(j.get("description", "Telegram error"))
    return j["result"]


def run_auto_backup():
    cfg = auto_cfg()
    bot = one("SELECT * FROM bots WHERE id=?", (cfg.get("bot_id"),)) if cfg.get("bot_id") else None
    if not bot or not str(cfg.get("chat_id")).strip():
        raise RuntimeError("Choose a bot and a chat ID first.")
    with_data = bool(cfg.get("with_data"))
    data = gzip.compress(json.dumps(backup_doc(with_data, True), ensure_ascii=False, default=str).encode("utf-8"))
    note = ""
    if len(data) > 49 * 1024 * 1024 and with_data:      # Telegram bots can send files up to 50 MB
        data = gzip.compress(json.dumps(backup_doc(False, True), ensure_ascii=False, default=str).encode("utf-8"))
        note = " (too big with data: commands only)"
    n = one("SELECT COUNT(*) c FROM bots")["c"]
    fn = f"botbench_backup_{time.strftime('%Y%m%d_%H%M')}.json.gz"
    tg_send_file(bot["token"], str(cfg["chat_id"]).strip(), fn, data,
                 f"Botbench backup · {n} bot(s) · {time.strftime('%Y-%m-%d %H:%M')}{note}")
    cfg["last_ts"], cfg["last_status"] = time.time(), f"ok · {max(1, len(data) // 1024)} KB{note}"
    set_auto(cfg)
    return cfg


def _autobackup_tick():
    cfg = auto_cfg()
    if cfg["enabled"] and time.time() - cfg["last_ts"] >= float(cfg["every_hours"]) * 3600:
        try:
            run_auto_backup()
        except Exception as e:
            cfg = auto_cfg()
            cfg["last_status"] = f"failed: {e}"[:200]
            cfg["last_ts"] = time.time() - float(cfg["every_hours"]) * 3600 + 600     # try again in 10 minutes
            set_auto(cfg)


BG = ThreadPoolExecutor(max_workers=8)


def _control():
    """Requests from commands (Account.start_bot, delete_bot, clone_bot ...). A request with _wait gets its answer
    written to the row, which the command is polling; others are fire-and-forget."""
    for r in rows("SELECT * FROM ctl WHERE COALESCE(done,0)=0 ORDER BY id LIMIT 20"):
        try:
            args = json.loads(r.get("args") or "{}")
        except Exception:
            args = {}
        act, bid = r["action"], r["bot_id"]
        res = {"ok": True, "result": "done"}
        try:
            known = one("SELECT 1 FROM bots WHERE id=?", (bid,))
            if act in ("start", "stop", "restart"):
                if not known:
                    raise ValueError("Bot not found")
                if act in ("stop", "restart"):
                    stop_bot(bid)
                if act == "restart":
                    time.sleep(0.4)
                if act in ("start", "restart"):
                    start_bot(bid)
                res["result"] = {"start": "Bot started successfully", "stop": "Bot stopped successfully",
                                 "restart": "Bot restarted successfully"}[act]
            elif act == "delete_bot":
                if args.get("permanent"):
                    if known:
                        trash_bot(bid)
                    purge_trashed_bot(bid)
                elif not trash_bot(bid):
                    raise ValueError("Bot not found")
                res["result"] = "Bot deleted successfully"
            elif act == "recover_bot":
                res.update(result="Bot recovered successfully", botid=str(restore_trashed_bot(bid, args.get("new_token"))))
            elif act == "permanent_delete_bot":
                purge_trashed_bot(bid)
                res["result"] = "Bot permanently deleted"
            elif act == "clear_expired_bots":
                n = one("SELECT COUNT(*) c FROM trash_bots WHERE deleted_at<?", (time.time() - TRASH_DAYS * 86400,))["c"]
                cleanup_trash()
                res["result"] = f"Removed {n} expired bot(s)"
            elif act == "create_bot":
                nb = create_bot(args.get("token"), args.get("name"), args.get("username"))
                start_bot(nb)
                res.update(result="Bot created successfully", botid=str(nb))
            elif act == "clone_bot":
                if not args.get("token"):
                    raise ValueError("A bot token is required to clone a bot here.")
                nb = clone_bot(bid, args["token"])
                res.update(result="Bot cloned successfully", botid=str(nb))
            elif act == "import_bot":
                if not args.get("token"):
                    raise ValueError("A bot token is required to import a bot here.")
                nb = import_bot(args.get("commands") or [], args["token"])
                res.update(result="Bot imported successfully", botid=str(nb))
            elif act == "replace_token":
                nb, un = replace_token(args.get("token"))
                res.update(result=f"Replaced 1 token(s): new token saved for @{un}", botid=str(nb), username=un)
            elif act == "reindex":
                invalidate(bid)
            elif act == "backup_now":
                run_auto_backup()
        except Exception as e:
            res = {"ok": False, "result": str(e), "message": str(e)}
            log(bid or 0, "error", f"{act} failed: {e}")
        if args.get("_wait"):
            run("UPDATE ctl SET done=1, result=? WHERE id=?", (json.dumps(res), r["id"]))
        else:
            run("DELETE FROM ctl WHERE id=?", (r["id"],))
    run("DELETE FROM ctl WHERE COALESCE(done,0)=1 AND id<(SELECT COALESCE(MAX(id),0)-200 FROM ctl)")


def _control_loop():
    while True:
        time.sleep(0.15)
        try:
            _control()
        except Exception:
            pass


def _scheduler():
    n = 0
    while True:
        time.sleep(1)
        n += 1
        try:
            if n % 60 == 0:
                _autobackup_tick()
            if n % 3600 == 30:
                cleanup_trash()
            for r in rows("SELECT * FROM scheduled WHERE run_at<=? ORDER BY run_at LIMIT 20", (time.time(),)):
                bot = one("SELECT * FROM bots WHERE id=?", (r["bot_id"],))
                if bot and not is_running(bot["id"]):
                    continue                      # keep it until the bot is running again
                run("DELETE FROM scheduled WHERE id=?", (r["id"],))
                if bot:
                    opts = json.loads(r["options"]) if r.get("options") else None
                    BG.submit(fire_hook, bot, r["command"], r["user_id"], opts, r.get("user_json"))
        except Exception:
            pass


def _camel(n):
    p = n.split("_")
    return p[0] + "".join(x[:1].upper() + x[1:] for x in p[1:])


def _run_broadcast(job):
    from urllib.parse import urlparse, parse_qs
    bot = one("SELECT * FROM bots WHERE id=?", (job["bot_id"],))
    if not bot:
        run("UPDATE bcjobs SET status='stopped' WHERE id=?", (job["id"],))
        return
    kw = json.loads(job["kwargs"] or "{}")
    users = [r["user_id"] for r in rows("SELECT user_id FROM users WHERE bot_id=? AND COALESCE(blocked,0)=0", (bot["id"],))]
    run("UPDATE bcjobs SET total=? WHERE id=?", (len(users), job["id"]))
    ok = bad = 0
    delay, stopped = 0.05, False
    for n_, uid in enumerate(users):
        if n_ % 10 == 0:
            st = one("SELECT status, speed FROM bcjobs WHERE id=?", (job["id"],))
            while st and st["status"] == "paused":
                time.sleep(1)
                st = one("SELECT status, speed FROM bcjobs WHERE id=?", (job["id"],))
            if not st or st["status"] in ("stopped", "deleted"):
                stopped = True
                break
            delay = 1.0 / max(1.0, st["speed"] or 20.0)
        try:
            if job.get("code") and not job["function"]:
                res_ = run_code_for_user(bot, job["code"], uid, kw or None)
                if res_.get("errors"):
                    raise RuntimeError(_short_of(res_["errors"][0]))
            elif job["function"]:
                fn = _camel(job["function"])
                from sandbox import fix_html
                kw = {k: (fix_html(v) if k in ("text", "caption") and isinstance(v, str) and kw.get("parse_mode", "HTML") == "HTML" else v)
                      for k, v in kw.items()}
                extra = {"parse_mode": "HTML"} if fn in ("sendMessage", "sendPhoto", "sendVideo", "sendAnimation",
                         "sendDocument", "sendAudio", "sendVoice") and "parse_mode" not in kw and "entities" not in kw else {}
                tg(bot["token"], fn, **{**extra, **kw, "chat_id": uid})
            else:
                fire_hook(bot, job["command"], uid, kw)
            ok += 1
        except Exception as e:
            bad += 1
            if _is_blocked(e):
                run("UPDATE users SET blocked=1 WHERE bot_id=? AND user_id=?", (bot["id"], uid))
        if (ok + bad) % 20 == 0:
            run("UPDATE bcjobs SET sent=?, failed=? WHERE id=?", (ok, bad, job["id"]))
        time.sleep(delay)
    run("UPDATE bcjobs SET sent=?, failed=?, status=? WHERE id=?", (ok, bad, "stopped" if stopped else "done", job["id"]))
    if job.get("callback_url") and not stopped:
        q = parse_qs(urlparse(job["callback_url"]).query)
        owner = one("SELECT * FROM bots WHERE id=?", (job.get("owner_bot") or job["bot_id"],)) or bot
        if q.get("c") and q.get("u"):
            fire_hook(owner, q["c"][0], int(q["u"][0]), {"json": {
                "status": "completed", "broadcast_id": job["id"], "total": len(users),
                "total_success": ok, "total_errors": bad}, "method": "POST"})


def _bc_loop():
    while True:
        time.sleep(2)
        try:
            job = one("SELECT * FROM bcjobs WHERE status='queued' ORDER BY ts LIMIT 1")
            if job:
                run("UPDATE bcjobs SET status='running' WHERE id=?", (job["id"],))
                _run_broadcast(job)
        except Exception:
            pass


# ---------- runners ----------
RUNNERS = {}
ALLOWED = ["message", "edited_message", "channel_post", "edited_channel_post", "callback_query", "inline_query",
           "chosen_inline_result", "my_chat_member", "chat_member", "chat_join_request", "pre_checkout_query",
           "shipping_query", "poll", "poll_answer", "message_reaction"]
_bg_started = []


class Runner(threading.Thread):
    def __init__(self, bot, previous=None):
        super().__init__(daemon=True)
        self.bot, self.previous, self.started = bot, previous, time.time()
        self.stop_event = threading.Event()
        self.pool = ThreadPoolExecutor(max_workers=16)

    def _safe(self, upd):
        try:
            handle_update(self.bot, upd)
        except Exception as e:
            record_error(self.bot["id"], "", f"Update failed: {e}")

    def run(self):
        bid, token = self.bot["id"], self.bot["token"]
        if self.previous:
            self.previous.join()
        self.started = time.time()
        log(bid, "info", "Bot started")
        offset = None
        try:
            tg(token, "deleteWebhook")
        except Exception as e:
            log(bid, "error", f"deleteWebhook failed: {e}")
        while not self.stop_event.is_set():
            try:
                for u in tg(token, "getUpdates", offset=offset, timeout=25,
                            allowed_updates=ALLOWED):
                    offset = u["update_id"] + 1
                    self.pool.submit(self._safe, u)
            except Exception as e:
                log(bid, "error", f"Polling error: {e}")
                self.stop_event.wait(3)
        self.pool.shutdown(wait=False)
        log(bid, "info", "Bot stopped")


def is_running(bot_id):
    r = RUNNERS.get(bot_id)
    return bool(r and r.is_alive() and not r.stop_event.is_set())


def uptime(bot_id):
    r = RUNNERS.get(bot_id)
    return int(time.time() - r.started) if is_running(bot_id) else 0


def start_bot(bot_id):
    if is_running(bot_id):
        return
    bot = one("SELECT * FROM bots WHERE id=?", (bot_id,))
    r = Runner(bot, previous=RUNNERS.get(bot_id))
    RUNNERS[bot_id] = r
    r.start()
    run("UPDATE bots SET running=1 WHERE id=?", (bot_id,))


def stop_bot(bot_id):
    r = RUNNERS.get(bot_id)
    if r:
        r.stop_event.set()
    run("UPDATE bots SET running=0 WHERE id=?", (bot_id,))


def autostart():
    if not _bg_started:
        _bg_started.append(1)
        for fn in (warm_up, _scheduler, _bc_loop, _control_loop):
            threading.Thread(target=fn, daemon=True).start()
    for b in rows("SELECT id FROM bots WHERE running=1"):
        start_bot(b["id"])
