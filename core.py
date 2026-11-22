import os, re, json, sqlite3, threading, subprocess, sys, time, queue
from concurrent.futures import ThreadPoolExecutor
import requests
from requests.adapters import HTTPAdapter

BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("PANEL_DB", os.path.join(BASE, "panel.db"))
SANDBOX = os.path.join(BASE, "sandbox.py")
EXEC_TIMEOUT = int(os.environ.get("EXEC_TIMEOUT", "10"))
WORKERS = int(os.environ.get("WORKERS", "4"))
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
CREATE TABLE IF NOT EXISTS logs(id INTEGER PRIMARY KEY AUTOINCREMENT, bot_id INTEGER, ts REAL, level TEXT, text TEXT);
CREATE INDEX IF NOT EXISTS idx_users_bot ON users(bot_id, last_seen);
"""
MIGRATIONS = [
    "ALTER TABLE users ADD COLUMN first_name TEXT", "ALTER TABLE users ADD COLUMN username TEXT",
    "ALTER TABLE users ADD COLUMN first_seen REAL", "ALTER TABLE users ADD COLUMN last_seen REAL",
    "ALTER TABLE users ADD COLUMN blocked INTEGER DEFAULT 0",
    "ALTER TABLE bots ADD COLUMN translate INTEGER DEFAULT 0",
    "ALTER TABLE bots ADD COLUMN version TEXT DEFAULT '1.0.0'",
    "ALTER TABLE commands ADD COLUMN folder TEXT", "ALTER TABLE pending ADD COLUMN options TEXT",
]

DEFAULT_START = '''# Runs when someone sends /start (or just "start")
name = user.first_name or "there"
count = user.get("visits", 0) + 1
user.save("visits", count)

bot.send(
    f"Hi {name}! You've started me {count} time(s).",
    inline=[[("Say hello", "hello"), ("Ask me something", "ask")]],
)
'''

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


def send_broadcast(bot, text, parse_mode="HTML"):
    uids = [r["user_id"] for r in rows("SELECT user_id FROM users WHERE bot_id=? AND COALESCE(blocked,0)=0", (bot["id"],))]
    bc = run("INSERT INTO broadcasts(bot_id,ts,text,total,sent,failed,status) VALUES(?,?,?,?,0,0,'sending')",
             (bot["id"], time.time(), text, len(uids)))

    def work():
        s = f = 0
        for uid in uids:
            try:
                tg(bot["token"], "sendMessage", chat_id=uid, text=text, parse_mode=parse_mode)
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


class Index(dict):
    """exact-case keys first, case-insensitive fallback"""
    def __init__(self, items):
        super().__init__()
        self.lower = {}
        for name, cid in items:
            self[nkc(name)] = cid
            self.lower.setdefault(nk(name), cid)

    def lookup(self, text):
        k = nkc(text)
        return self.get(k) or self.lower.get(k.lower())

    get_ci = lookup


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
        return {**EMPTY, "error": msg, "errors": [{"command": cmd["name"], "message": msg, "line": None}]}
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
    if utype in ("message", "callback_query") and chat.get("type") == "private":
        touch_user(bid, user)
    if not cid:
        return
    cmd = one("SELECT id, name, code FROM commands WHERE id=?", (cid,))
    if cmd:
        apply_result(bot, execute(bot, cmd, ctx, gate=True, options=options))


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
    res = execute(bot, cmd, make_ctx("webhook", msg, u, chat, "", args, None, None, 0), options=options)
    apply_result(bot, res)
    return res


BG = ThreadPoolExecutor(max_workers=8)


def _scheduler():
    while True:
        time.sleep(1)
        try:
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
    kw = json.loads(job["kwargs"] or "{}")
    users = [r["user_id"] for r in rows("SELECT user_id FROM users WHERE bot_id=? AND COALESCE(blocked,0)=0", (bot["id"],))]
    run("UPDATE bcjobs SET total=? WHERE id=?", (len(users), job["id"]))
    ok = bad = 0
    for uid in users:
        try:
            if job["function"]:
                tg(bot["token"], _camel(job["function"]), **{**kw, "chat_id": uid})
            else:
                fire_hook(bot, job["command"], uid, kw)
            ok += 1
        except Exception as e:
            bad += 1
            if _is_blocked(e):
                run("UPDATE users SET blocked=1 WHERE bot_id=? AND user_id=?", (bot["id"], uid))
        if (ok + bad) % 20 == 0:
            run("UPDATE bcjobs SET sent=?, failed=? WHERE id=?", (ok, bad, job["id"]))
        time.sleep(0.05)
    run("UPDATE bcjobs SET sent=?, failed=?, status='done' WHERE id=?", (ok, bad, job["id"]))
    if job.get("callback_url"):
        q = parse_qs(urlparse(job["callback_url"]).query)
        if q.get("c") and q.get("u"):
            fire_hook(bot, q["c"][0], int(q["u"][0]), {"json": {
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
        for fn in (warm_up, _scheduler, _bc_loop):
            threading.Thread(target=fn, daemon=True).start()
    for b in rows("SELECT id FROM bots WHERE running=1"):
        start_bot(b["id"])
