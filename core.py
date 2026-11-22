import os, json, sqlite3, threading, subprocess, sys, time
from concurrent.futures import ThreadPoolExecutor
import requests

BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("PANEL_DB", os.path.join(BASE, "panel.db"))
SANDBOX = os.path.join(BASE, "sandbox.py")
EXEC_TIMEOUT = int(os.environ.get("EXEC_TIMEOUT", "10"))
MARK = "\x00RESULT\x00"

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
CREATE TABLE IF NOT EXISTS logs(id INTEGER PRIMARY KEY AUTOINCREMENT, bot_id INTEGER, ts REAL, level TEXT, text TEXT);
"""

DEFAULT_START = '''# Runs when someone sends /start
name = user.first_name or "there"
count = user.get("visits", 0) + 1
user.save("visits", count)

bot.send(
    f"Hi {name}! You've started me {count} time(s).",
    inline=[[("Say hello", "hello"), ("Ask me something", "ask")]],
)
'''

_lock = threading.Lock()


def _conn():
    c = sqlite3.connect(DB_PATH, timeout=30)
    c.row_factory = sqlite3.Row
    return c


def run(sql, args=()):
    with _lock:
        c = _conn()
        try:
            cur = c.execute(sql, args)
            c.commit()
            return cur.lastrowid
        finally:
            c.close()


def rows(sql, args=()):
    with _lock:
        c = _conn()
        try:
            return [dict(r) for r in c.execute(sql, args).fetchall()]
        finally:
            c.close()


def one(sql, args=()):
    r = rows(sql, args)
    return r[0] if r else None


def init_db():
    with _lock:
        c = _conn()
        c.executescript(SCHEMA)
        c.commit()
        c.close()
    for sql in ["ALTER TABLE users ADD COLUMN first_name TEXT", "ALTER TABLE users ADD COLUMN username TEXT",
                "ALTER TABLE users ADD COLUMN first_seen REAL", "ALTER TABLE users ADD COLUMN last_seen REAL",
                "ALTER TABLE users ADD COLUMN blocked INTEGER DEFAULT 0",
                "ALTER TABLE bots ADD COLUMN translate INTEGER DEFAULT 0"]:
        try:
            run(sql)
        except sqlite3.OperationalError:
            pass


def touch_user(bid, u):
    now = time.time()
    run("INSERT OR IGNORE INTO users(bot_id, user_id, first_seen) VALUES(?,?,?)", (bid, u["id"], now))
    run("UPDATE users SET first_name=COALESCE(?,first_name), username=COALESCE(?,username), last_seen=?, "
        "blocked=0, first_seen=COALESCE(first_seen,?) WHERE bot_id=? AND user_id=?",
        (u.get("first_name"), u.get("username"), now, now, bid, u["id"]))


def _is_blocked(err):
    e = str(err).lower()
    return "blocked" in e or "deactivated" in e or "chat not found" in e


def send_broadcast(bot, text, parse_mode="HTML"):
    uids = [r["user_id"] for r in rows(
        "SELECT user_id FROM users WHERE bot_id=? AND COALESCE(blocked,0)=0", (bot["id"],))]
    bc = run("INSERT INTO broadcasts(bot_id, ts, text, total, sent, failed, status) VALUES(?,?,?,?,0,0,'sending')",
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
            time.sleep(0.05)
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


# ---------- Telegram ----------
def tg(token, method, **params):
    r = requests.post(f"https://api.telegram.org/bot{token}/{method}", json=params, timeout=40)
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(data.get("description", "Telegram error"))
    return data["result"]


def log(bot_id, level, text):
    run("INSERT INTO logs(bot_id, ts, level, text) VALUES(?,?,?,?)",
        (bot_id, time.time(), level, str(text)[:3000]))
    run("DELETE FROM logs WHERE bot_id=? AND id NOT IN "
        "(SELECT id FROM logs WHERE bot_id=? ORDER BY id DESC LIMIT 500)", (bot_id, bot_id))


# ---------- storage ----------
def kv_all(bot_id, scope):
    return {r["k"]: json.loads(r["v"]) for r in
            rows("SELECT k, v FROM kv WHERE bot_id=? AND scope=?", (bot_id, scope))}


def kv_apply(bot_id, scope, changes):
    for k, v in (changes or {}).items():
        if v is None:
            run("DELETE FROM kv WHERE bot_id=? AND scope=? AND k=?", (bot_id, scope, k))
        else:
            run("INSERT OR REPLACE INTO kv(bot_id, scope, k, v) VALUES(?,?,?,?)",
                (bot_id, scope, k, json.dumps(v, default=str)))


# ---------- execution ----------
def execute(bot, code, ctx):
    uid = ctx["user"]["id"]
    payload = {
        "code": code, "ctx": ctx,
        "bot_info": {"username": bot.get("username"), "name": bot.get("name")},
        "bot_data": kv_all(bot["id"], "bot"),
        "user_data": kv_all(bot["id"], f"user:{uid}"),
    }
    empty = {"actions": [], "bot_save": {}, "user_save": {}, "pending": None, "stdout": ""}
    try:
        p = subprocess.run([sys.executable, SANDBOX], input=json.dumps(payload),
                           capture_output=True, text=True, timeout=EXEC_TIMEOUT)
    except subprocess.TimeoutExpired:
        return {**empty, "error": f"Stopped: the command ran longer than {EXEC_TIMEOUT} seconds."}
    if MARK in p.stdout:
        return {**empty, **json.loads(p.stdout.rsplit(MARK, 1)[1])}
    return {**empty, "error": (p.stderr or "The sandbox exited without a result.").strip()[-2000:]}


def apply_result(bot, uid, res):
    for a in res["actions"]:
        try:
            tg(bot["token"], a["method"], **a["params"])
        except Exception as e:
            log(bot["id"], "error", f'{a["method"]} failed: {e}')
            cid = a["params"].get("chat_id")
            if cid and _is_blocked(e):
                run("UPDATE users SET blocked=1 WHERE bot_id=? AND user_id=?", (bot["id"], cid))
    kv_apply(bot["id"], "bot", res["bot_save"])
    kv_apply(bot["id"], f"user:{uid}", res["user_save"])
    if res.get("pending"):
        run("INSERT OR REPLACE INTO pending(bot_id, user_id, command) VALUES(?,?,?)",
            (bot["id"], uid, res["pending"]))
    if res.get("stdout"):
        log(bot["id"], "print", res["stdout"].rstrip())
    if res.get("error"):
        log(bot["id"], "error", res["error"].rstrip())


def find_command(bot_id, *names):
    for n in names:
        c = one("SELECT * FROM commands WHERE bot_id=? AND name=?", (bot_id, n))
        if c:
            return c
    return None


def build_ctx(user, chat, message, text, data=None, cq_id=None):
    parts = text.split()
    return {"user": user, "chat": chat, "message": message, "text": text,
            "args": parts[1:] if text.startswith("/") else parts,
            "data": data, "callback_query_id": cq_id,
            "message_id": message.get("message_id")}


def handle_update(bot, upd):
    bid = bot["id"]
    if "callback_query" in upd:
        cq = upd["callback_query"]
        msg = cq.get("message") or {"chat": {"id": cq["from"]["id"]}}
        data = cq.get("data", "")
        cmd = find_command(bid, f"cb:{data}", "cb:*")
        ctx = build_ctx(cq["from"], msg["chat"], msg, "", data, cq["id"])
        user = cq["from"]
    elif "message" in upd and upd["message"].get("from"):
        msg = upd["message"]
        user = msg["from"]
        text = msg.get("text") or msg.get("caption") or ""
        ctx = build_ctx(user, msg["chat"], msg, text)
        cmd = None
        pend = one("SELECT command FROM pending WHERE bot_id=? AND user_id=?", (bid, user["id"]))
        if pend and not text.startswith("/"):
            run("DELETE FROM pending WHERE bot_id=? AND user_id=?", (bid, user["id"]))
            cmd = find_command(bid, pend["command"])
        else:
            if pend:
                run("DELETE FROM pending WHERE bot_id=? AND user_id=?", (bid, user["id"]))
            if text.startswith("/"):
                name = text.split()[0].split("@")[0].lower()
                cmd = find_command(bid, name, "*")
            else:
                cmd = find_command(bid, "*")
    elif "my_chat_member" in upd:
        m = upd["my_chat_member"]
        if m.get("chat", {}).get("type") == "private" and m["new_chat_member"]["status"] == "kicked":
            run("UPDATE users SET blocked=1 WHERE bot_id=? AND user_id=?", (bid, m["from"]["id"]))
        return
    else:
        return
    touch_user(bid, user)
    if not cmd:
        return
    res = execute(bot, cmd["code"], ctx)
    apply_result(bot, user["id"], res)


# ---------- runners ----------
RUNNERS = {}


class Runner(threading.Thread):
    def __init__(self, bot, previous=None):
        super().__init__(daemon=True)
        self.bot, self.previous = bot, previous
        self.stop_event = threading.Event()
        self.pool = ThreadPoolExecutor(max_workers=4)

    def _safe(self, upd):
        try:
            handle_update(self.bot, upd)
        except Exception as e:
            log(self.bot["id"], "error", f"Update failed: {e}")

    def run(self):
        bid, token = self.bot["id"], self.bot["token"]
        if self.previous:
            self.previous.join()
        log(bid, "info", "Bot started")
        offset = None
        try:
            tg(token, "deleteWebhook")
        except Exception as e:
            log(bid, "error", f"deleteWebhook failed: {e}")
        while not self.stop_event.is_set():
            try:
                updates = tg(token, "getUpdates", offset=offset, timeout=25,
                             allowed_updates=["message", "callback_query", "my_chat_member"])
                for u in updates:
                    offset = u["update_id"] + 1
                    self.pool.submit(self._safe, u)
            except Exception as e:
                log(bid, "error", f"Polling error: {e}")
                self.stop_event.wait(5)
        self.pool.shutdown(wait=False)
        log(bid, "info", "Bot stopped")


def is_running(bot_id):
    r = RUNNERS.get(bot_id)
    return bool(r and r.is_alive() and not r.stop_event.is_set())


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
    for b in rows("SELECT id FROM bots WHERE running=1"):
        start_bot(b["id"])
