"""Worker process: runs one bot command per job (many jobs per process).

core.py keeps a few of these warm. Each job is one JSON line in, one JSON line out.
The runtime speaks two dialects at once:
  * the panel's own API:   bot.send(...), user.get(...), wait_next(...)
  * Telebot Creator (TPY): bot.sendMessage(...), Bot.getData(...), User.saveData(...),
    libs.Resources..., handleNextCommand, runCommand, @ ~ ! special commands, telebot types.
Command code is ordinary Python (imports allowed).
"""
import sys, os, json, io, re, time, math, random, hashlib, hmac, base64, binascii, traceback, contextlib, sqlite3, uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, unquote, parse_qs
import requests

TG_BASE = os.environ.get("TG_API_BASE", "https://api.telegram.org").rstrip("/")
SESSION = requests.Session()
SESSION.mount("https://", requests.adapters.HTTPAdapter(pool_connections=4, pool_maxsize=16))


class ReturnCommand(Exception):
    """raise ReturnCommand  -> stop this command quietly."""


class TelegramError(RuntimeError):
    pass


def _wrap(v):
    if isinstance(v, dict):
        return v if isinstance(v, D) else D(v)
    if isinstance(v, list):
        return [_wrap(x) for x in v]
    return v


class D(dict):
    """dict with attribute access; missing attributes are None (like telebot objects)."""
    def __getattr__(self, k):
        if k.startswith("__"):
            raise AttributeError(k)
        return _wrap(self.get(k))

    def __getitem__(self, k):
        return _wrap(dict.__getitem__(self, k))


def bunchify(x):
    return _wrap(x)


def _alias_from(o):
    """Telegram sends "from"; telebot exposes it as from_user."""
    if isinstance(o, dict):
        if "from" in o and "from_user" not in o:
            o["from_user"] = o["from"]
        for v in list(o.values()):
            _alias_from(v)
    elif isinstance(o, list):
        for v in o:
            _alias_from(v)
    return o


# ---------------- telebot-style types ----------------
def _plain(o):
    if hasattr(o, "to_dict") and not isinstance(o, dict):
        return o.to_dict()
    if isinstance(o, dict):
        return {k: _plain(v) for k, v in o.items() if not str(k).startswith("_")}
    if isinstance(o, (list, tuple)):
        return [_plain(v) for v in o]
    return o


class _T:
    def to_dict(self):
        return {k: _plain(v) for k, v in self.__dict__.items() if v is not None and not k.startswith("_")}

    def to_json(self):
        return json.dumps(self.to_dict())


class InlineKeyboardButton(_T):
    def __init__(self, text, url=None, callback_data=None, web_app=None, **kw):
        self.text, self.url, self.callback_data, self.web_app = text, url, callback_data, web_app
        self.__dict__.update(kw)


class KeyboardButton(_T):
    def __init__(self, text, request_contact=None, request_location=None, request_poll=None, web_app=None, **kw):
        self.text, self.request_contact, self.request_location = text, request_contact, request_location
        self.request_poll, self.web_app = request_poll, web_app
        self.__dict__.update(kw)


class InlineKeyboardMarkup(_T):
    def __init__(self, keyboard=None, row_width=3):
        self.keyboard, self._rw = keyboard or [], row_width

    def add(self, *buttons, row_width=None):
        rw = row_width or self._rw
        for i in range(0, len(buttons), rw):
            self.keyboard.append(list(buttons[i:i + rw]))
        return self

    def row(self, *buttons):
        self.keyboard.append(list(buttons))
        return self

    def to_dict(self):
        return {"inline_keyboard": [[_plain(b) for b in r] for r in self.keyboard]}


class ReplyKeyboardMarkup(_T):
    def __init__(self, resize_keyboard=None, one_time_keyboard=None, selective=None, row_width=3,
                 input_field_placeholder=None, is_persistent=None):
        self.keyboard, self._rw = [], row_width
        self.resize_keyboard, self.one_time_keyboard, self.selective = resize_keyboard, one_time_keyboard, selective
        self.input_field_placeholder, self.is_persistent = input_field_placeholder, is_persistent

    def add(self, *buttons, row_width=None):
        rw = row_width or self._rw
        for i in range(0, len(buttons), rw):
            self.keyboard.append(list(buttons[i:i + rw]))
        return self

    def row(self, *buttons):
        self.keyboard.append(list(buttons))
        return self

    def to_dict(self):
        d = super().to_dict()
        d["keyboard"] = [[{"text": b} if isinstance(b, str) else _plain(b) for b in r] for r in self.keyboard]
        return d


class ReplyKeyboardRemove(_T):
    def __init__(self, selective=None):
        self.remove_keyboard, self.selective = True, selective


class ForceReply(_T):
    def __init__(self, selective=None, input_field_placeholder=None):
        self.force_reply, self.selective, self.input_field_placeholder = True, selective, input_field_placeholder


class LinkPreviewOptions(_T):
    def __init__(self, is_disabled=None, url=None, prefer_small_media=None, prefer_large_media=None, show_above_text=None):
        self.is_disabled, self.url, self.prefer_small_media = is_disabled, url, prefer_small_media
        self.prefer_large_media, self.show_above_text = prefer_large_media, show_above_text


def apply_html_entities(text, entities, custom_subs=None):
    """Turn Telegram message entities back into HTML (UTF-16 offsets)."""
    if not entities:
        return text
    u16 = text.encode("utf-16-le")
    esc = lambda s: s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    tags = {"bold": "b", "italic": "i", "underline": "u", "strikethrough": "s", "code": "code",
            "spoiler": "tg-spoiler", "blockquote": "blockquote"}
    opens, closes = {}, {}
    for e in entities:
        g = (lambda k: e.get(k)) if isinstance(e, dict) else (lambda k: getattr(e, k, None))
        t, off, ln = g("type"), g("offset"), g("length")
        if t in tags:
            o, c = f"<{tags[t]}>", f"</{tags[t]}>"
        elif t == "pre":
            lang = g("language")
            o, c = (f'<pre><code class="language-{lang}">', "</code></pre>") if lang else ("<pre>", "</pre>")
        elif t == "text_link":
            o, c = f'<a href="{g("url")}">', "</a>"
        elif t == "text_mention":
            o, c = f'<a href="tg://user?id={(g("user") or {}).get("id")}">', "</a>"
        elif t == "custom_emoji":
            o, c = f'<tg-emoji emoji-id="{g("custom_emoji_id")}">', "</tg-emoji>"
        else:
            continue
        opens.setdefault(off, []).append((ln, o))
        closes.setdefault(off + ln, []).insert(0, c)
    out, i, n = [], 0, len(u16) // 2
    for pos in range(n + 1):
        for c in closes.get(pos, []):
            out.append(c)
        for ln, o in sorted(opens.get(pos, []), key=lambda x: -x[0]):
            out.append(o)
        if pos < n:
            ch = u16[pos * 2:pos * 2 + 2]
            if 0xD800 <= int.from_bytes(ch, "little") <= 0xDBFF and pos + 1 < n:
                out.append(esc(u16[pos * 2:pos * 2 + 4].decode("utf-16-le", "ignore")))
                u16 = u16[:pos * 2 + 2] + b"\x00\x00" + u16[pos * 2 + 4:] if False else u16
                continue
            out.append(esc(ch.decode("utf-16-le", "ignore")) if not (0xDC00 <= int.from_bytes(ch, "little") <= 0xDFFF) else "")
    return "".join(out)


# ---------------- helpers available to commands ----------------
def isNumeric(x):
    try:
        float(str(x))
        return True
    except Exception:
        return False


md5 = lambda s: hashlib.md5(s.encode() if isinstance(s, str) else s).hexdigest()
rawurlencode = lambda s: quote(str(s), safe="")
encodeURIComponent = lambda s: quote(str(s), safe="~()*!.'-_")
decodeURIComponent = lambda s: unquote(str(s))
jsondumps = encodejson = lambda x, **k: json.dumps(x, ensure_ascii=False, default=str, **k)


def _api_url(url):
    """Telegram URLs follow TG_API_BASE (a local Bot API server, or a test server)."""
    u = str(url)
    return TG_BASE + u[len("https://api.telegram.org"):] if u.startswith("https://api.telegram.org") else u


class _HTTP:
    def get(self, url, **kw):
        kw.setdefault("timeout", 8)
        return requests.get(_api_url(url), **kw)

    def post(self, url, **kw):
        kw.setdefault("timeout", 8)
        return requests.post(_api_url(url), **kw)


def _tzinfo(name):
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception:
        return timezone(timedelta(hours=5, minutes=30)) if name in ("Asia/Kolkata", "Asia/Calcutta", "IST") else timezone.utc


class _DateAndTime:
    def now(self, tz="UTC"):
        d = datetime.now(_tzinfo(tz))
        return D({"date": d.strftime("%Y-%m-%d"), "time": d.strftime("%H:%M:%S"), "datetime": d.isoformat(),
                  "timestamp": d.timestamp(), "year": d.year, "month": d.month, "day": d.day, "hour": d.hour,
                  "minute": d.minute, "second": d.second, "weekday": d.strftime("%A"), "timezone": tz})


class _Random:
    randomInt = staticmethod(lambda a, b: random.randint(int(a), int(b)))
    randomFloat = staticmethod(lambda a, b: random.uniform(a, b))
    randomChoice = staticmethod(lambda seq: random.choice(seq))
    randomWeightedChoice = staticmethod(lambda items, weights: random.choices(items, weights=weights)[0])
    randomAscii = staticmethod(lambda n: "".join(random.choice("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ") for _ in range(int(n))))

    @staticmethod
    def randomStr(n, char_set=None):
        cs = char_set or "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
        return "".join(random.choice(cs) for _ in range(int(n)))


class _Namespace:
    def __init__(self, **kw):
        self.__dict__.update(kw)


# ---------------- one job ----------------
_DB = {}


def run_job(p):
    ctx = p["ctx"]
    DRY = bool(p.get("dry"))
    BOT = p["bot"]
    BID = BOT["id"]
    TOKEN = p["token"]
    actions, errors, overlay = [], [], {}
    state = {"pending": None, "scheduled": [], "depth": 0, "in_error": False}
    UID = ctx["user"]["id"]
    CHAT = (ctx.get("chat") or {}).get("id", UID)

    def db():
        c = _DB.get(p["db_path"])
        if c is None:
            c = sqlite3.connect(p["db_path"], timeout=30, isolation_level=None, check_same_thread=False)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA busy_timeout=30000")
            _DB[p["db_path"]] = c
        return c

    # ----- storage (live, shared with the panel) -----
    MISSING = object()

    def kv_get(scope, key):
        if (scope, key) in overlay:
            v = overlay[(scope, key)]
            return MISSING if v is MISSING else v
        r = db().execute("SELECT v FROM kv WHERE bot_id=? AND scope=? AND k=?", (BID, scope, key)).fetchone()
        return json.loads(r["v"]) if r else MISSING

    def kv_set(scope, key, value):
        if DRY:
            overlay[(scope, key)] = value
            return
        db().execute("INSERT OR REPLACE INTO kv(bot_id,scope,k,v) VALUES(?,?,?,?)",
                     (BID, scope, key, json.dumps(value, default=str)))

    def kv_del(scope, key):
        if DRY:
            overlay[(scope, key)] = MISSING
            return
        db().execute("DELETE FROM kv WHERE bot_id=? AND scope=? AND k=?", (BID, scope, key))

    def kv_all(scope):
        out = {r["k"]: json.loads(r["v"]) for r in db().execute("SELECT k,v FROM kv WHERE bot_id=? AND scope=?", (BID, scope))}
        for (s, k), v in overlay.items():
            if s == scope:
                out.pop(k, None) if v is MISSING else out.__setitem__(k, v)
        return out

    class Store:
        def __init__(self, scope):
            self._scope = scope

        def get(self, key, default=None):
            v = kv_get(self._scope, key)
            return default if v is MISSING else v

        def save(self, key, value):
            kv_set(self._scope, key, value)

        def delete(self, key):
            kv_del(self._scope, key)

        def all(self):
            return kv_all(self._scope)

    def cross_bot(bot_id, api_key):
        if bot_id or api_key:
            raise NotImplementedError("Reading another bot's data (bot_id/api_key) only works inside Telebot Creator.")

    # ----- Telegram -----
    def fake(method, params):
        cid = params.get("chat_id", CHAT)
        if method.startswith("send") or method.startswith("copy") or method.startswith("forward"):
            return D({"message_id": 1, "chat": {"id": cid, "type": "private"}, "date": int(time.time()),
                      "text": params.get("text"), "from": {"id": 0, "is_bot": True}})
        if method == "getChatMember":
            return D({"status": "member", "user": {"id": params.get("user_id")}})
        if method == "getChat":
            return D({"id": cid, "type": "private", "first_name": "Test"})
        if method in ("getChatMemberCount", "getChatMembersCount"):
            return 2
        if method in ("exportChatInviteLink",):
            return "https://t.me/+test"
        return True

    def tgcall(method, **params):
        params = {k: _plain(v) for k, v in params.items() if v is not None}
        if params.get("parse_mode"):
            params["parse_mode"] = {"html": "HTML", "markdown": "Markdown", "markdownv2": "MarkdownV2"}.get(
                str(params["parse_mode"]).lower(), params["parse_mode"])
        if DRY:
            actions.append({"method": method, "params": params})
            return _wrap(fake(method, params))
        try:
            data = SESSION.post(f"{TG_BASE}/bot{TOKEN}/{method}", json=params, timeout=25).json()
        except Exception as e:
            raise TelegramError(f"Telegram request failed: {e}")
        if not data.get("ok"):
            desc = data.get("description", "error")
            if "not modified" in desc and method.startswith("edit"):
                return True
            cid = params.get("chat_id")
            if cid and any(w in desc.lower() for w in ("blocked", "deactivated", "chat not found")):
                try:
                    db().execute("UPDATE users SET blocked=1 WHERE bot_id=? AND user_id=?", (BID, cid))
                except Exception:
                    pass
            raise TelegramError(f"Telegram API error: {data.get('error_code')}. Description: {desc}")
        return _wrap(data["result"])

    POS = {"sendMessage": ["text"], "sendPhoto": ["photo"], "sendVideo": ["video"], "sendAnimation": ["animation"],
           "sendDocument": ["document"], "sendAudio": ["audio"], "sendVoice": ["voice"], "sendSticker": ["sticker"],
           "sendVideoNote": ["video_note"], "sendContact": ["phone_number", "first_name"],
           "sendLocation": ["latitude", "longitude"], "sendPoll": ["question", "options"], "sendChatAction": ["action"],
           "sendMediaGroup": ["media"], "editMessageText": ["text"], "editMessageCaption": ["caption"],
           "deleteMessage": ["chat_id", "message_id"], "deleteMessages": ["chat_id", "message_ids"],
           "copyMessage": ["chat_id", "from_chat_id", "message_id"], "forwardMessage": ["chat_id", "from_chat_id", "message_id"],
           "getChatMember": ["chat_id", "user_id"], "getChat": ["chat_id"], "getChatMemberCount": ["chat_id"],
           "exportChatInviteLink": ["chat_id"], "createChatInviteLink": ["chat_id"], "answerCallbackQuery": ["callback_query_id", "text"],
           "banChatMember": ["chat_id", "user_id"], "unbanChatMember": ["chat_id", "user_id"],
           "restrictChatMember": ["chat_id", "user_id"], "promoteChatMember": ["chat_id", "user_id"],
           "approveChatJoinRequest": ["chat_id", "user_id"], "declineChatJoinRequest": ["chat_id", "user_id"],
           "pinChatMessage": ["chat_id", "message_id"], "unpinChatMessage": ["chat_id", "message_id"],
           "leaveChat": ["chat_id"], "getFile": ["file_id"], "setMyCommands": ["commands"],
           "getUserProfilePhotos": ["user_id"], "setMessageReaction": ["chat_id", "message_id", "reaction"],
           "getChatAdministrators": ["chat_id"]}
    ALIAS = {"replyText": "sendMessage", "replyPhoto": "sendPhoto", "getChatMembersCount": "getChatMemberCount",
             "kickChatMember": "banChatMember"}
    DEFAULT_CHAT = {"sendChatAction", "sendDice", "editMessageText", "editMessageCaption", "editMessageReplyMarkup",
                    "sendMediaGroup", "stopPoll"}

    def camel(name):
        parts = name.split("_")
        return parts[0] + "".join(x[:1].upper() + x[1:] for x in parts[1:]) if len(parts) > 1 else name

    def markup(inline=None, keyboard=None, remove_keyboard=False):
        if inline:
            if not isinstance(inline[0], list):
                inline = [inline]
            return {"inline_keyboard": [[b if isinstance(b, dict) else (
                {"text": str(b[0]), "url": str(b[1])} if str(b[1]).startswith(("http://", "https://", "tg://"))
                else {"text": str(b[0]), "callback_data": str(b[1])[:64]}) for b in row] for row in inline]}
        if keyboard:
            return {"keyboard": [[{"text": str(k)} for k in row] for row in keyboard], "resize_keyboard": True}
        return {"remove_keyboard": True} if remove_keyboard else None

    class BotAPI:
        """Low-level Telegram wrapper (bot.*) plus the panel's friendly helpers."""
        username, name = BOT.get("username"), BOT.get("name")
        id = BID
        token = TOKEN

        def __getattr__(self, name):
            if name.startswith("_"):
                raise AttributeError(name)
            method = camel(name)

            def call(*args, **kw):
                return self._invoke(method, args, kw)
            return call

        def _invoke(self, method, args, kw):
            orig, method = method, ALIAS.get(method, method)
            names = list(POS.get(method, []))
            if orig in ("replyText", "replyPhoto"):
                names = ["chat_id"] + names
                if len(args) == 1:
                    names = names[1:]
            elif method.startswith("send") and len(args) == 2 and names:
                names = ["chat_id"] + names
            for n, v in zip(names, args):
                kw[n] = v
            if method == "getFileUrl":
                return f"{TG_BASE}/file/bot{TOKEN}/{tgcall('getFile', file_id=kw.get('file_id'))['file_path']}"
            if method in DEFAULT_CHAT or method.startswith("send") or method.startswith("copy"):
                kw.setdefault("chat_id", CHAT) if method != "copyMessage" else None
            if method in ("editMessageText", "editMessageCaption", "editMessageReplyMarkup") and "inline_message_id" not in kw:
                kw.setdefault("message_id", ctx.get("message_id"))
            if method == "answerCallbackQuery":
                kw.setdefault("callback_query_id", ctx.get("callback_query_id"))
            if "text" in kw and kw["text"] is not None:
                kw["text"] = str(kw["text"])
            return tgcall(method, **kw)

        def info(self, *a, **k):
            return D({"id": BID, "bot_id": str(BID), "name": BOT.get("name"), "first_name": BOT.get("name"),
                      "username": BOT.get("username"), "token": TOKEN, "status": "working"})

        # --- panel-native helpers ---
        def send(self, text, chat_id=None, parse_mode=None, inline=None, keyboard=None, remove_keyboard=False,
                 reply=False, **extra):
            if reply and ctx.get("message_id"):
                extra["reply_to_message_id"] = ctx["message_id"]
            return self.sendMessage(text=text, chat_id=chat_id or CHAT, parse_mode=parse_mode,
                                    reply_markup=markup(inline, keyboard, remove_keyboard), **extra)

        def reply(self, text, **kw):
            return self.send(text, reply=True, **kw)

        def photo(self, photo, caption=None, chat_id=None, inline=None, **extra):
            return self.sendPhoto(photo=photo, caption=caption, chat_id=chat_id or CHAT, reply_markup=markup(inline), **extra)

        def document(self, document, caption=None, chat_id=None, inline=None, **extra):
            return self.sendDocument(document=document, caption=caption, chat_id=chat_id or CHAT, reply_markup=markup(inline), **extra)

        def edit(self, text, inline=None, parse_mode=None):
            return self.editMessageText(text=text, parse_mode=parse_mode, reply_markup=markup(inline))

        def answer(self, text=None, alert=False):
            return self.answerCallbackQuery(text=text, show_alert=alert or None)

        def call(self, method, **params):
            return tgcall(method, **params)

        def get(self, key, default=None):
            return botstore.get(key, default)

        def save(self, key, value):
            botstore.save(key, value)

        def delete(self, key):
            botstore.delete(key)

        def all(self):
            return botstore.all()

        def wait_next(self, command):
            wait_next(command)

    # ----- pending / scheduling / sub-commands -----
    def wait_next(command, options=None, cancel_at_command=False):
        state["pending"] = command
        if not DRY:
            db().execute("INSERT OR REPLACE INTO pending(bot_id,user_id,command,options) VALUES(?,?,?,?)",
                         (BID, UID, command, json.dumps(options, default=str) if options is not None else None))
        return {"ok": True}

    def nk(s):
        s = re.sub(r"\s+", " ", (s or "").strip().lower())
        return s[1:].strip() if s.startswith("/") else s

    def nkc(s):
        s = re.sub(r"\s+", " ", (s or "").strip())
        return s[1:].strip() if s.startswith("/") else s

    def find_cmd(name):
        rows_ = db().execute("SELECT name, code FROM commands WHERE bot_id=?", (BID,)).fetchall()
        exact = nkc(name)
        for r in rows_:
            if nkc(r["name"]) == exact:
                return r["name"], r["code"]
        for r in rows_:
            if nk(r["name"]) == nk(name):
                return r["name"], r["code"]
        return None

    def split_command(text):
        """'/Cmd a b' or 'Link Wallet 5' -> (name, code, rest) using the longest matching prefix."""
        words = str(text).split()
        for k in range(len(words), 0, -1):
            hit = find_cmd(" ".join(words[:k]))
            if hit:
                return hit[0], hit[1], " ".join(words[k:])
        return None

    def runCommand(command, options=None):
        if state["in_error"]:
            raise RuntimeError("runCommand is disabled inside the ! error handler")
        if state["depth"] >= 8:
            raise RuntimeError("runCommand is nested too deeply")
        hit = split_command(command)
        if not hit:
            raise RuntimeError(f"runCommand: no command named {command!r}")
        name, code, rest = hit
        state["depth"] += 1
        try:
            run_code(name, code, options=options, params=rest or "None")
        except ReturnCommand:
            pass
        finally:
            state["depth"] -= 1
        return {"ok": True}

    def runCommandAfter(timeout, command, options=None, id=None):
        secs = timeout.timestamp() - time.time() if hasattr(timeout, "timestamp") else float(timeout)
        jid = id or uuid.uuid4().hex[:12]
        job = {"id": jid, "command": command, "timeout": secs}
        if DRY:
            state["scheduled"].append(job)
            return job
        db().execute("INSERT OR REPLACE INTO scheduled(id,bot_id,user_id,chat_id,run_at,command,options,user_json) "
                     "VALUES(?,?,?,?,?,?,?,?)", (jid, BID, UID, CHAT, time.time() + max(1, secs), command,
                                                  json.dumps(options, default=str), json.dumps(ctx["user"])))
        return job

    def cancelScheduledTask(job_id):
        if not DRY:
            db().execute("DELETE FROM scheduled WHERE id=? AND bot_id=?", (job_id, BID))
        return {"ok": True}

    def webhook_url(command=None, user_id=None, *a):
        uid = user_id if user_id is not None else UID
        sig = hmac.new(p["secret"].encode(), f"{BID}|{command}|{uid}".encode(), "sha256").hexdigest()[:24]
        return f"{p['base_url'].rstrip('/')}/hook/{BID}?c={quote(str(command), safe='')}&u={uid}&s={sig}"

    def broadcast(function=None, command=None, callback_url=None, **kw):
        for k in ("mode", "bot_ids", "speed", "code", "api_key", "bot_id", "warnings"):
            kw.pop(k, None)
        if not (function or command):
            return {"status": "error", "message": "function or command is required"}
        jid = uuid.uuid4().hex[:12]
        if not DRY:
            db().execute("INSERT INTO bcjobs(id,bot_id,ts,function,command,kwargs,callback_url,status,total,sent,failed) "
                         "VALUES(?,?,?,?,?,?,?,'queued',0,0,0)", (jid, BID, time.time(), function, command,
                                                                 json.dumps(_plain(kw), default=str), callback_url))
        return {"status": "success", "broadcast_id": jid}

    botstore = Store("bot")
    userstore = Store(f"user:{UID}")

    _rc, _rca, _cst, _bc = runCommand, runCommandAfter, cancelScheduledTask, broadcast

    class BotHigh:
        getData = staticmethod(lambda name, bot_id=None, api_key=None: (cross_bot(bot_id, api_key), botstore.get(name))[1])
        saveData = staticmethod(lambda name, data, bot_id=None, api_key=None: (cross_bot(bot_id, api_key), botstore.save(name, data), {"ok": True})[2])
        deleteData = staticmethod(lambda name, bot_id=None: (botstore.delete(name), {"ok": True})[1])
        handleNextCommand = staticmethod(wait_next)
        runCommand = staticmethod(_rc)
        runCommandAfter = staticmethod(_rca)
        cancelScheduledTask = staticmethod(_cst)
        broadcast = staticmethod(_bc)
        genId = genRandomId = gen_random_id = staticmethod(lambda: uuid.uuid4().hex[:16])
        genRandomErrorId = staticmethod(lambda: "ERR" + uuid.uuid4().hex[:8])

        @staticmethod
        def deleteAllData(except_data=None):
            for k in list(botstore.all()):
                if k not in (except_data or []):
                    botstore.delete(k)

        @staticmethod
        def info(bot_id=None, api_key=None):
            cross_bot(bot_id, api_key)
            return D({"bot_id": str(BID), "id": BID, "name": BOT.get("name"), "username": BOT.get("username"),
                      "status": "working", "token": TOKEN})

        @staticmethod
        def sendMessage(*a, **k):
            return bot._invoke("sendMessage", a, k)

        # panel-style: Bot.get / Bot.save
        get, save, delete = staticmethod(botstore.get), staticmethod(botstore.save), staticmethod(botstore.delete)

    class UserHigh:
        @staticmethod
        def _s(user):
            return Store(f"user:{user}") if user is not None else userstore

        @staticmethod
        def getData(name, user=None, data_type=None, bot_id=None, api_key=None):
            cross_bot(bot_id, api_key)
            return UserHigh._s(user).get(name)

        @staticmethod
        def saveData(name, data, user=None, data_type=None, bot_id=None, api_key=None):
            cross_bot(bot_id, api_key)
            UserHigh._s(user).save(name, data)
            return {"ok": True}

        @staticmethod
        def deleteData(name, user=None, data_type=None, bot_id=None, api_key=None):
            UserHigh._s(user).delete(name)
            return {"ok": True}

        @staticmethod
        def deleteAllData(name=None, old_data=False, except_data=None):
            for k in list(userstore.all()):
                if k not in (except_data or []) and (name is None or k == name):
                    userstore.delete(k)

        @staticmethod
        def getAllDataOfUser(user, output_format="json"):
            return io.BytesIO(json.dumps(Store(f"user:{user}").all(), indent=2, default=str).encode())

        @staticmethod
        def deleteAllDataOfUser(user):
            for k in list(Store(f"user:{user}").all()):
                Store(f"user:{user}").delete(k)

    class Res:
        def __init__(self, scope, name):
            self.s, self.k = Store(scope), "__res__" + name

        def value(self):
            return self.s.get(self.k, 0)

        def add(self, n):
            self.s.save(self.k, self.value() + n)
            return self.value()

        def cut(self, n):
            self.s.save(self.k, self.value() - n)
            return self.value()
        remove = cut

        def set(self, n):
            self.s.save(self.k, n)
            return n

        def reset(self):
            self.s.save(self.k, 0)

    class Resources:
        userRes = staticmethod(lambda name, user=None: Res(f"user:{user if user is not None else UID}", name))
        anotherRes = staticmethod(lambda name, user=None: Res(f"user:{user if user is not None else UID}", name))
        globalRes = staticmethod(lambda name: Res("bot", name))

    class Ads:
        """Rewarded ads are a Telebot Creator monetisation feature; here the reward is granted straight away."""
        reward_ad = staticmethod(lambda text=None, then=None, **k: True)
        claim = staticmethod(lambda *a, **k: True)

    class Account:
        def __getattr__(self, name):
            def na(*a, **k):
                raise NotImplementedError(f"Account.{name}() is a Telebot Creator platform function and isn't available here.")
            return na

    def MembershipCheck(chats, user):
        for c in (chats if isinstance(chats, (list, tuple)) else [chats]):
            try:
                if tgcall("getChatMember", chat_id=c, user_id=user).status not in ("member", "administrator", "creator"):
                    return False
            except Exception:
                return False
        return True

    bot = BotAPI()
    libs = _Namespace(DateAndTime=_DateAndTime(), Random=_Random(), Resources=Resources, tbcads=Ads,
                      Webhook=_Namespace(getUrlFor=staticmethod(webhook_url)))

    def make_ns(options=None, params=None):
        msgobj = ctx["message"]
        m = _alias_from(json.loads(json.dumps(msgobj)))
        m["update_type"] = ctx.get("update_type", "message")
        message = D(m)
        text = ctx.get("text", "")
        uinfo = ctx["user"]
        user = _UserObj(uinfo)
        ns = {
            "__name__": "__command__", "bot": bot, "Bot": BotHigh, "User": UserHigh, "user": user,
            "message": message, "call": message, "chat": _wrap(msgobj.get("chat") or {}), "msg": text, "text": text,
            "params": params if params is not None else (" ".join(ctx["args"]) if ctx.get("args") else "None"),
            "args": ctx["args"], "data": ctx.get("data"), "u": UID, "options": _opts(options),
            "update_type": ctx.get("update_type", "message"), "bot_id": str(BID), "bot_token": TOKEN,
            "left_points": 10 ** 9, "wait_next": wait_next, "handleNextCommand": wait_next, "runCommand": runCommand,
            "libs": libs, "HTTP": _HTTP(), "requests": requests, "Account": Account(), "MembershipCheck": MembershipCheck,
            "ReturnCommand": ReturnCommand, "returncommand": ReturnCommand, "returnCommand": ReturnCommand,
            "bunchify": bunchify, "isNumeric": isNumeric, "md5": md5, "rawurlencode": rawurlencode,
            "encodeURIComponent": encodeURIComponent, "decodeURIComponent": decodeURIComponent, "parse_qs": parse_qs,
            "jsondumps": jsondumps, "encodejson": encodejson, "bf_json": json, "json": json, "re": re, "regex": re,
            "time": time, "base64": base64, "binascii": binascii, "hashlib": hashlib, "random": random, "math": math,
            "apply_html_entities": apply_html_entities, "InlineKeyboardButton": InlineKeyboardButton,
            "InlineKeyboardMarkup": InlineKeyboardMarkup, "KeyboardButton": KeyboardButton,
            "ReplyKeyboardMarkup": ReplyKeyboardMarkup, "ReplyKeyboardRemove": ReplyKeyboardRemove,
            "ForceReply": ForceReply, "LinkPreviewOptions": LinkPreviewOptions, "TelegramError": TelegramError,
        }
        ns.update(p.get("env") or {})
        return ns

    def _opts(o):
        return _wrap(o) if isinstance(o, (dict, list)) else o

    class _UserObj(Store):
        def __init__(self, info):
            super().__init__(f"user:{info['id']}")
            for k in ("id", "first_name", "last_name", "username", "language_code"):
                setattr(self, k, info.get(k))

    def run_code(name, code, options=None, params=None, preface=True):
        pre = ""
        if preface:
            tilde = find_cmd("~")
            if tilde and nk(name) != "~":
                pre = tilde[1].rstrip("\n") + "\n"
        offset = pre.count("\n")
        try:
            exec(compile(pre + code, "<command>", "exec"), make_ns(options, params))
        except ReturnCommand:
            raise
        except BaseException as e:
            line = None
            for fr in traceback.extract_tb(e.__traceback__):
                if fr.filename == "<command>":
                    line = fr.lineno
            if isinstance(e, SyntaxError) and e.filename == "<command>":
                line = e.lineno
            e._cmd_info = (name, max(1, line - offset) if line else None)
            raise

    # ----- run: gate -> command -> error handler -----
    buf, first_error = io.StringIO(), None

    def note(name, e, line):
        errors.append({"command": name, "message": "".join(traceback.format_exception(type(e), e, e.__traceback__)).strip(), "line": line})

    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        name, code = p["cmd"]["name"], p["cmd"]["code"]
        try:
            stop = False
            if p.get("gate"):
                gate = find_cmd("@")
                if gate and nk(name) != "@":
                    try:
                        run_code("@", gate[1], options=p.get("options"), params=None, preface=False)
                    except ReturnCommand:
                        stop = True
                    except BaseException as e:
                        note("@", e, getattr(e, "_cmd_info", (0, None))[1])
            if not stop:
                run_code(name, code, options=p.get("options"))
        except ReturnCommand:
            pass
        except BaseException as e:
            cname, line = getattr(e, "_cmd_info", (name, None))
            note(cname, e, line)
            handler = find_cmd("!") if nk(name) != "!" else None
            if handler:
                state["in_error"] = True
                try:
                    run_code("!", handler[1], options=str(e), params="None", preface=False)
                except ReturnCommand:
                    pass
                except BaseException as e2:
                    note("!", e2, getattr(e2, "_cmd_info", (0, None))[1])
    first = errors[0] if errors else None
    return {"actions": actions, "pending": state["pending"], "scheduled": state["scheduled"],
            "stdout": buf.getvalue()[-4000:], "errors": errors,
            "error": first["message"] if first else None, "error_line": first["line"] if first else None}


def serve():
    proto_in = os.fdopen(os.dup(0), "r", encoding="utf-8")
    proto_out = os.fdopen(os.dup(1), "w", encoding="utf-8")
    os.dup2(os.open(os.devnull, os.O_RDWR), 0)
    os.dup2(2, 1)
    sys.stdin = open(os.devnull)
    for line in proto_in:
        try:
            res = run_job(json.loads(line))
        except BaseException:
            res = {"actions": [], "pending": None, "scheduled": [], "stdout": "", "errors": [], "error_line": None,
                   "error": traceback.format_exc()}
        proto_out.write(json.dumps(res, default=str) + "\n")
        proto_out.flush()


if __name__ == "__main__":
    serve()
