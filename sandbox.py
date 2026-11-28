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


class BcResult(D):
    """What Bot.broadcast returns. Usable as a dict, and as a name or key (it stands for its broadcast_id)."""
    def __str__(self):
        return str(self.get("broadcast_id"))

    def __hash__(self):
        return hash(self.get("broadcast_id"))


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


_TG_TAGS = {"b", "strong", "i", "em", "u", "ins", "s", "strike", "del", "a", "code", "pre", "tg-spoiler",
            "tg-emoji", "blockquote", "span"}
_TAG_RE = re.compile(r"<(/?)([A-Za-z][A-Za-z0-9-]*)((?:\s+[^<>]*?)?)\s*(/?)>")


def _esc_text(t):
    t = re.sub(r"&(?!(?:#\d+|#x[0-9a-fA-F]+|[A-Za-z][A-Za-z0-9]*);)", "&amp;", t)
    return t.replace("<", "&lt;").replace(">", "&gt;")


def fix_html(text):
    """Make HTML safe for Telegram: close tags that were left open, drop stray closing tags, turn <br> into a
    new line, and escape a bare < > & or an unsupported tag so it shows as text instead of causing a 400 error."""
    s, out, stack, pos = str(text), [], [], 0
    for m in _TAG_RE.finditer(s):
        out.append(_esc_text(s[pos:m.start()]))
        pos = m.end()
        closing, name, attrs, selfclose = m.group(1) == "/", m.group(2).lower(), m.group(3), m.group(4)
        if name == "br":
            out.append("\n")
        elif name not in _TG_TAGS or (name == "span" and "tg-spoiler" not in attrs):
            out.append(_esc_text(m.group(0)))
        elif closing:
            if name in stack:
                while stack:
                    top = stack.pop()
                    out.append(f"</{top}>")
                    if top == name:
                        break
        elif not selfclose:
            stack.append(name)
            out.append(f"<{name}{attrs}>")
    out.append(_esc_text(s[pos:]))
    while stack:
        out.append(f"</{stack.pop()}>")
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


class _Resp(requests.Response):
    pass


def _fake_response(url, body, status=200, headers=None):
    r = _Resp()
    r.status_code, r.url = status, url
    r._content = body.encode("utf-8") if isinstance(body, str) else body
    r.headers.update({"Content-Type": "application/json", **(headers or {})})
    return r


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
    state = {"pending": None, "scheduled": [], "depth": 0, "in_error": False, "answered": False}
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

    def kv_get(scope, key, bid=None):
        bid = BID if bid is None else bid
        if (bid, scope, key) in overlay:
            v = overlay[(bid, scope, key)]
            return MISSING if v is MISSING else v
        r = db().execute("SELECT v FROM kv WHERE bot_id=? AND scope=? AND k=?", (bid, scope, key)).fetchone()
        return json.loads(r["v"]) if r else MISSING

    def kv_set(scope, key, value, bid=None):
        bid = BID if bid is None else bid
        if DRY:
            overlay[(bid, scope, key)] = value
            return
        db().execute("INSERT OR REPLACE INTO kv(bot_id,scope,k,v) VALUES(?,?,?,?)",
                     (bid, scope, key, json.dumps(value, default=str)))

    def kv_del(scope, key, bid=None):
        bid = BID if bid is None else bid
        if DRY:
            overlay[(bid, scope, key)] = MISSING
            return
        db().execute("DELETE FROM kv WHERE bot_id=? AND scope=? AND k=?", (bid, scope, key))

    def kv_all(scope, bid=None):
        bid = BID if bid is None else bid
        out = {r["k"]: json.loads(r["v"]) for r in db().execute("SELECT k,v FROM kv WHERE bot_id=? AND scope=?", (bid, scope))}
        for (b_, s_, k), v in overlay.items():
            if b_ == bid and s_ == scope:
                out.pop(k, None) if v is MISSING else out.__setitem__(k, v)
        return out

    class Store:
        def __init__(self, scope, bid=None):
            self._scope, self._bid = scope, bid

        def get(self, key, default=None):
            v = kv_get(self._scope, str(key), self._bid)
            return default if v is MISSING else v

        def save(self, key, value):
            kv_set(self._scope, str(key), value, self._bid)

        def delete(self, key):
            kv_del(self._scope, str(key), self._bid)

        def all(self):
            return kv_all(self._scope, self._bid)

    def _tbid(bot_id):
        """bot_id= on Bot/User/Resources calls: any bot of this panel (api_key is not needed here)."""
        if bot_id in (None, ""):
            return BID
        try:
            t = int(str(bot_id).strip())
        except ValueError:
            raise ValueError(f"{bot_id!r} is not a bot ID")
        if t != BID and not db().execute("SELECT 1 FROM bots WHERE id=?", (t,)).fetchone():
            raise ValueError(f"Bot {bot_id} was not found in this panel")
        return t

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
        if params.get("parse_mode") not in (None, "HTML", "Markdown", "MarkdownV2"):
            raise TelegramError("Telegram API error: 400. Description: Bad Request: unsupported parse_mode "
                                f"({str(params['parse_mode'])[:40]!r})")
        if params.get("parse_mode") == "HTML":
            for k in ("text", "caption"):
                if isinstance(params.get(k), str):
                    params[k] = fix_html(params[k])
        if method == "answerCallbackQuery":
            state["answered"] = True
        if DRY:
            actions.append({"method": method, "params": params})
            return _wrap(fake(method, params))
        files = {}
        for k, v in list(params.items()):
            if hasattr(v, "read") or isinstance(v, (bytes, bytearray)):
                files[k] = (getattr(v, "name", None) or "file", v)
            elif isinstance(v, tuple) and len(v) == 2 and isinstance(v[1], (bytes, bytearray)) or hasattr(v, "read"):
                files[k] = v
        try:
            if files:      # file upload: multipart, other values JSON-encoded
                form = {k: (v if isinstance(v, (str, int, float)) else json.dumps(v)) for k, v in params.items() if k not in files}
                data = SESSION.post(f"{TG_BASE}/bot{TOKEN}/{method}", data=form, files=files, timeout=90).json()
            else:
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
    SIG = {k: v.split() for k, v in {
        "sendMessage": "text parse_mode entities disable_web_page_preview disable_notification protect_content reply_to_message_id allow_sending_without_reply reply_markup timeout message_thread_id reply_parameters link_preview_options",
        "replyText": "chat_id text parse_mode entities disable_web_page_preview disable_notification protect_content reply_to_message_id allow_sending_without_reply reply_markup timeout message_thread_id reply_parameters link_preview_options",
        "replyPhoto": "chat_id photo caption parse_mode caption_entities disable_notification protect_content reply_to_message_id allow_sending_without_reply reply_markup",
        "sendPhoto": "photo caption parse_mode caption_entities disable_web_page_preview disable_notification protect_content reply_to_message_id allow_sending_without_reply reply_markup timeout message_thread_id has_spoiler",
        "sendVideo": "video duration width height thumbnail caption parse_mode caption_entities supports_streaming disable_notification protect_content reply_to_message_id allow_sending_without_reply reply_markup",
        "sendAudio": "audio caption duration performer title reply_to_message_id reply_markup parse_mode",
        "sendVoice": "voice caption duration reply_to_message_id reply_markup parse_mode",
        "sendDocument": "document reply_to_message_id caption reply_markup parse_mode",
        "sendSticker": "sticker reply_to_message_id reply_markup",
        "sendAnimation": "animation duration width height thumbnail caption parse_mode caption_entities",
        "sendVideoNote": "video_note duration length reply_to_message_id reply_markup",
        "sendMediaGroup": "media", "sendDice": "emoji", "sendChatAction": "action",
        "sendLocation": "latitude longitude live_period reply_to_message_id reply_markup",
        "sendVenue": "latitude longitude title address", "sendContact": "phone_number first_name last_name vcard",
        "sendPoll": "question options is_anonymous type allows_multiple_answers correct_option_id explanation explanation_parse_mode open_period close_date is_closed",
        "editMessageText": "text chat_id message_id inline_message_id parse_mode entities disable_web_page_preview reply_markup link_preview_options",
        "editMessageCaption": "caption chat_id message_id inline_message_id parse_mode caption_entities reply_markup",
        "editMessageReplyMarkup": "chat_id message_id inline_message_id reply_markup",
        "editMessageMedia": "media chat_id message_id inline_message_id reply_markup",
        "deleteMessage": "chat_id message_id", "deleteMessages": "chat_id message_ids",
        "forwardMessage": "chat_id from_chat_id message_id disable_notification protect_content",
        "copyMessage": "chat_id from_chat_id message_id caption parse_mode caption_entities disable_notification protect_content reply_to_message_id allow_sending_without_reply reply_markup",
        "forwardMessages": "chat_id from_chat_id message_ids", "copyMessages": "chat_id from_chat_id message_ids",
        "getChat": "chat_id", "getChatMember": "chat_id user_id", "getChatMemberCount": "chat_id",
        "getChatAdministrators": "chat_id return_bots", "leaveChat": "chat_id", "getFile": "file_id",
        "getFileUrl": "file_id", "getUserProfilePhotos": "user_id offset limit",
        "banChatMember": "chat_id user_id until_date revoke_messages", "unbanChatMember": "chat_id user_id only_if_banned",
        "restrictChatMember": "chat_id user_id until_date", "promoteChatMember": "chat_id user_id",
        "approveChatJoinRequest": "chat_id user_id", "declineChatJoinRequest": "chat_id user_id",
        "pinChatMessage": "chat_id message_id disable_notification", "unpinChatMessage": "chat_id message_id",
        "unpinAllChatMessages": "chat_id", "createChatInviteLink": "chat_id name expire_date member_limit creates_join_request",
        "revokeChatInviteLink": "chat_id invite_link", "exportChatInviteLink": "chat_id",
        "setMessageReaction": "chat_id message_id reaction is_big", "setChatTitle": "chat_id title",
        "setChatDescription": "chat_id description", "stopPoll": "chat_id message_id reply_markup",
        "answerCallbackQuery": "callback_query_id text show_alert url cache_time",
        "answerInlineQuery": "inline_query_id results cache_time is_personal next_offset",
        "answerPreCheckoutQuery": "pre_checkout_query_id ok error_message",
        "answerShippingQuery": "shipping_query_id ok shipping_options error_message",
        "answerWebAppQuery": "web_app_query_id result", "setMyCommands": "commands scope language_code",
        "sendInvoice": "title description invoice_payload provider_token currency prices",
        "createInvoiceLink": "title description payload provider_token currency prices",
        "getChatMenuButton": "chat_id", "setChatMenuButton": "chat_id menu_button",
    }.items()}
    HTML_DEFAULT = {"sendMessage", "editMessageText", "editMessageCaption", "sendPhoto", "sendVideo", "sendAnimation",
                    "sendDocument", "sendAudio", "sendVoice", "copyMessage"}
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

    def _chat_like(v, nargs):
        """Is this positional argument a chat (123456, -100123, "@channel") rather than the message content?"""
        if isinstance(v, bool):
            return False
        if isinstance(v, int):
            return True
        return isinstance(v, str) and nargs >= 2 and bool(re.fullmatch(r"-?\d{5,}|@\w{3,}", v.strip()))

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
            if orig == "replyTo":
                m_, t_ = args[0], (args[1] if len(args) > 1 else kw.pop("text"))
                return self._invoke("sendMessage", (t_,), {**kw, "chat_id": m_.chat.id, "reply_to_message_id": m_.message_id})
            if orig == "getMe":
                return D({"id": BID, "is_bot": True, "first_name": BOT.get("name"), "username": BOT.get("username"),
                          "can_join_groups": True})
            if orig == "downloadFile":
                return requests.get(f"{TG_BASE}/file/bot{TOKEN}/{args[0] if args else kw['file_path']}", timeout=60).content
            names = list(SIG.get(orig) or SIG.get(method) or POS.get(method, []))
            if orig in ("replyText", "replyPhoto") and len(args) == 1 and names[1] not in kw:
                names = names[1:]          # bot.replyText("hello"): just the text, sent to the current chat
            elif (method.startswith("send") and names and names[0] != "chat_id" and "chat_id" not in kw
                  and args and _chat_like(args[0], len(args))):
                names = ["chat_id"] + names    # bot.sendMessage(u, "text"): chat first, like telebot's send_message
            for n, v in zip(names, args):
                if n in kw:
                    raise TypeError(f"{orig}() got multiple values for argument '{n}'")
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
            if kw.get("parse_mode") == "":          # TBC: parse_mode="" means plain text
                kw["parse_mode"] = None
            # Like Telebot Creator: HTML is the default. Pass parse_mode=None for plain text.
            if method in HTML_DEFAULT and ("text" in kw or "caption" in kw) and "parse_mode" not in kw \
                    and "entities" not in kw and "caption_entities" not in kw:
                kw["parse_mode"] = "HTML"
            return tgcall(method, **kw)

        def info(self, *a, **k):
            return D({"id": BID, "bot_id": str(BID), "name": BOT.get("name"), "first_name": BOT.get("name"),
                      "username": BOT.get("username"), "token": TOKEN, "status": "working"})

        # --- panel-native helpers ---
        def send(self, text, chat_id=None, parse_mode="HTML", inline=None, keyboard=None, remove_keyboard=False,
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

        def edit(self, text, inline=None, parse_mode="HTML"):
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
        raw = re.sub(r"\s+", " ", (name or "").strip())
        for test in (lambda r: re.sub(r"\s+", " ", r["name"].strip()) == raw,      # exactly as typed
                     lambda r: nkc(r["name"]) == nkc(name),                      # without the slash
                     lambda r: nk(r["name"]) == nk(name)):                       # ignoring case
            for r in rows_:
                if test(r):
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
        if state["depth"] >= 20:
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
        """Send a Telegram call (or run a command) for every user of this bot, or of another bot of the panel
        (bot_id=… or bot_ids=[…]). The callback_url command runs in the bot that started the broadcast."""
        bot_ids, single, speed = kw.pop("bot_ids", None), kw.pop("bot_id", None), kw.pop("speed", None)
        mode, code = kw.pop("mode", None), kw.pop("code", None)
        for k in ("api_key", "warnings"):
            kw.pop(k, None)
        if not (function or command or code):
            return {"status": "error", "message": "function, command or code is required"}
        if mode == "all":
            targets = [r["id"] for r in db().execute("SELECT id FROM bots")]
        else:
            targets = list(bot_ids) if bot_ids else ([single] if single else [BID])
        ids = []
        for t in targets:
            try:
                t = int(str(t))
            except ValueError:
                return {"status": "error", "message": f"{t!r} is not a bot ID"}
            if not db().execute("SELECT 1 FROM bots WHERE id=?", (t,)).fetchone():
                return {"status": "error", "message": f"Bot {t} was not found in this panel"}
            jid = uuid.uuid4().hex[:12]
            ids.append(jid)
            if not DRY:
                db().execute("INSERT INTO bcjobs(id,bot_id,ts,function,command,kwargs,callback_url,status,total,sent,failed,speed,owner_bot,code) "
                             "VALUES(?,?,?,?,?,?,?,'queued',0,0,0,?,?,?)", (jid, t, time.time(), function, command,
                                                                          json.dumps(_plain(kw), default=str), callback_url,
                                                                          float(speed) if speed else None, BID, code))
        return BcResult({"status": "success", "broadcast_id": ids[0], "broadcast_ids": ids})

    botstore = Store("bot")
    userstore = Store(f"user:{UID}")

    _rc, _rca, _cst, _bc = runCommand, runCommandAfter, cancelScheduledTask, broadcast

    def _file(name, data):
        f = io.BytesIO(data if isinstance(data, bytes) else str(data).encode("utf-8"))
        f.name = name
        return f

    def _users(extra=""):
        return [dict(r) for r in db().execute(
            f"SELECT user_id,first_name,username,first_seen,last_seen,blocked,COALESCE(banned,0) banned FROM users WHERE bot_id=?{extra}", (BID,))]

    def _ctl(action, bot_id=None):
        if not DRY:
            db().execute("INSERT INTO ctl(bot_id, action) VALUES(?,?)", (bot_id or BID, action))
        return {"ok": True}

    def _bcs(jid, bot_id=None, api_key=None):
        r = db().execute("SELECT * FROM bcjobs WHERE id=?", (jid,)).fetchone()
        if not r:
            return None
        st = {"done": "completed"}.get(r["status"], r["status"])
        tot = r["total"] or 0
        done = (r["sent"] or 0) + (r["failed"] or 0)
        return D({"broadcast_id": r["id"], "id": r["id"], "status": st, "total": tot, "total_users": tot,
                  "total_success": r["sent"] or 0, "total_errors": r["failed"] or 0, "sent": r["sent"] or 0,
                  "failed": r["failed"] or 0, "speed": r["speed"] or 20,
                  "progress": 100.0 if st == "completed" else (round(done * 100.0 / tot, 1) if tot else 0.0)})

    def _bc_set(jid, **cols):
        if not DRY:
            sets = ",".join(f"{k}=?" for k in cols)
            db().execute(f"UPDATE bcjobs SET {sets} WHERE id=?", (*cols.values(), jid))
        return {"ok": True}

    nraw_ = lambda t: re.sub(r"\s+", " ", (t or "").strip())

    def _cmd_row(botid, name):
        rows_ = db().execute("SELECT * FROM commands WHERE bot_id=?", (int(botid),)).fetchall()
        return next((r for r in rows_ if nraw_(r["name"]) == nraw_(name)), None) or \
            next((r for r in rows_ if nraw_(r["name"]).lstrip("/").lower() == nraw_(name).lstrip("/").lower()), None)

    def _trash_row(botid, key):
        for r in db().execute("SELECT * FROM trash_commands WHERE bot_id=? ORDER BY deleted_at DESC", (int(botid),)):
            if str(r["id"]) == str(key) or nraw_(r["name"]) == nraw_(str(key)):
                return r
        return None

    def deleted_commands(botid):
        return [D({"id": r["id"], "name": r["name"], "deleted_at": r["deleted_at"], "folder": r["folder"],
                   "lines": (r["code"] or "").count("\n") + 1})
                for r in db().execute("SELECT * FROM trash_commands WHERE bot_id=? ORDER BY deleted_at DESC", (int(botid),))]

    def delete_command(botid, name):
        """Move a command to the bot's recycle bin."""
        r = _cmd_row(botid, name)
        if not r:
            return {"ok": False, "error": f"no command named {name!r}"}
        if not DRY:
            db().execute("INSERT INTO trash_commands(bot_id,name,code,folder,deleted_at) VALUES(?,?,?,?,?)",
                         (r["bot_id"], r["name"], r["code"], r["folder"], time.time()))
            db().execute("DELETE FROM commands WHERE id=?", (r["id"],))
            _ctl("reindex", int(botid))
        return {"ok": True, "name": r["name"]}

    def restore_command(botid, key):
        """Bring a command back from the recycle bin (by name or by the id from getDeletedCommands)."""
        t = _trash_row(botid, key)
        if not t:
            return {"ok": False, "error": f"{key!r} is not in the recycle bin"}
        have = {nraw_(r["name"]) for r in db().execute("SELECT name FROM commands WHERE bot_id=?", (int(botid),))}
        name, i = t["name"], 1
        while nraw_(name) in have:
            i += 1
            name = f'{t["name"]} (restored{"" if i == 2 else " " + str(i - 1)})'
        if not DRY:
            db().execute("INSERT INTO commands(bot_id,name,code,folder,updated_at) VALUES(?,?,?,?,?)",
                         (t["bot_id"], name, t["code"], t["folder"], time.time()))
            db().execute("DELETE FROM trash_commands WHERE id=?", (t["id"],))
            _ctl("reindex", int(botid))
        return {"ok": True, "name": name}

    def purge_command(botid, key=None):
        """Delete one command from the recycle bin for good, or empty the whole bin (key=None)."""
        if not DRY:
            if key is None:
                db().execute("DELETE FROM trash_commands WHERE bot_id=?", (int(botid),))
            else:
                t = _trash_row(botid, key)
                t and db().execute("DELETE FROM trash_commands WHERE id=?", (t["id"],))
        return {"ok": True}

    def backup_url(kind="all.json"):
        t = db().execute("SELECT v FROM settings WHERE k='backup_token'").fetchone()
        if not t:
            import secrets as _s
            tok = _s.token_urlsafe(24)
            db().execute("INSERT OR REPLACE INTO settings(k,v) VALUES('backup_token',?)", (tok,))
        else:
            tok = t["v"]
        return f"{p['base_url'].rstrip('/')}/backup/{tok}/{kind}"

    _dc, _rc_cmd, _pc = delete_command, restore_command, purge_command

    class _BotMeta(type):
        def __getattr__(cls, name):          # Bot.replyText(...), Bot.getMe() ... fall through to the bot API
            if name.startswith("_"):
                raise AttributeError(name)
            return getattr(bot, name)

    class BotHigh(metaclass=_BotMeta):
        setData = staticmethod(lambda name, data, bot_id=None, **k: (Store("bot", _tbid(bot_id)).save(name, data), {"ok": True})[1])
        log = staticmethod(lambda *t: db().execute("INSERT INTO logs(bot_id,ts,level,text) VALUES(?,?,?,?)",
                                                   (BID, time.time(), "print", " ".join(map(str, t))[:3000])) and None)
        getAllData = staticmethod(lambda output_format="json": _file("bot_data." + output_format, json.dumps(botstore.all(), indent=2, default=str, ensure_ascii=False)))
        getDataFile = staticmethod(lambda name, output_format="txt": _file(f"{name}.{output_format}", (lambda v: v if isinstance(v, str) else json.dumps(v, default=str, ensure_ascii=False))(botstore.get(name))))
        stop = staticmethod(lambda bot_id=None, api_key=None: (cross_bot(bot_id, api_key), _ctl("stop"))[1])
        start = staticmethod(lambda bot_id=None, api_key=None: (cross_bot(bot_id, api_key), _ctl("start"))[1])
        status = staticmethod(lambda bot_id=None, api_key=None: "working" if (db().execute("SELECT running FROM bots WHERE id=?", (BID,)).fetchone() or {"running": 0})["running"] else "stopped")
        setAutoTranslate = staticmethod(lambda enabled=True: (db().execute("UPDATE bots SET translate=? WHERE id=?", (1 if enabled else 0, BID)), {"ok": True})[1])
        getAutoTranslate = staticmethod(lambda: bool((db().execute("SELECT translate FROM bots WHERE id=?", (BID,)).fetchone() or {"translate": 0})["translate"]))
        getDeletedCommands = staticmethod(lambda: deleted_commands(BID))
        deleteCommand = staticmethod(lambda name: delete_command(BID, name))
        restoreCommand = staticmethod(lambda key: restore_command(BID, key))
        purgeCommand = staticmethod(lambda key=None: purge_command(BID, key))
        getBroadcastStatus = getBroadcastProgress = staticmethod(_bcs)
        stopBroadcast = staticmethod(lambda jid, **k: _bc_set(jid, status="stopped"))
        pauseBroadcast = staticmethod(lambda jid, **k: _bc_set(jid, status="paused"))
        resumeBroadcast = staticmethod(lambda jid, speed=None, **k: _bc_set(jid, status="running", **({"speed": speed} if speed else {})))
        setBroadcastSpeed = staticmethod(lambda jid, speed, **k: _bc_set(jid, speed=float(speed)))
        rerunBroadcast = staticmethod(lambda jid, speed=None, **k: _bc_set(jid, status="queued", sent=0, failed=0, **({"speed": speed} if speed else {})))
        clearBroadcast = staticmethod(lambda jid=None: (db().execute("DELETE FROM bcjobs WHERE bot_id=?" + (" AND id=?" if jid else ""), (BID, jid) if jid else (BID,)), {"ok": True})[1])
        listBroadcasts = staticmethod(lambda status=None, limit=20: [_bcs(r["id"]) for r in db().execute("SELECT id FROM bcjobs WHERE bot_id=? OR owner_bot=? ORDER BY ts DESC LIMIT ?", (BID, BID, int(limit))).fetchall() if status is None or _bcs(r["id"]).status == status])
        blockUser = staticmethod(lambda user_id: (db().execute("UPDATE users SET banned=1 WHERE bot_id=? AND user_id=?", (BID, user_id)), {"ok": True})[1])
        unblockUser = staticmethod(lambda user_id: (db().execute("UPDATE users SET banned=0 WHERE bot_id=? AND user_id=?", (BID, user_id)), {"ok": True})[1])
        getBlockedUsers = staticmethod(lambda: [r["user_id"] for r in _users(" AND banned=1")])
        getBlockedUsersCount = staticmethod(lambda: len(_users(" AND banned=1")))
        getBlockedUsersFile = staticmethod(lambda: _file("blocked_users.json", json.dumps([r["user_id"] for r in _users(" AND banned=1")])))

        @staticmethod
        def getBotUsersFile(output_format="json", include_creation_date=False, include_last_active_date=False):
            cols = ["user_id", "first_name", "username"] + (["first_seen"] if include_creation_date else []) + (["last_seen"] if include_last_active_date else [])
            data = [{c: r[c] for c in cols} for r in _users()]
            if output_format == "csv":
                import csv
                b = io.StringIO()
                w = csv.DictWriter(b, fieldnames=cols)
                w.writeheader()
                w.writerows(data)
                return _file("users.csv", b.getvalue())
            return _file("users." + ("txt" if output_format == "txt" else "json"),
                         "\n".join(str(r["user_id"]) for r in data) if output_format == "txt" else json.dumps(data, indent=2))

        @staticmethod
        def getStats(time_frames=None, bot_id=None, api_key=None):
            now = time.time()
            n = lambda w, *a: db().execute(f"SELECT COUNT(*) c FROM users WHERE bot_id=? AND {w}", (BID, *a)).fetchone()["c"]
            frames = time_frames or [1, 7, 30]
            return D({"total_users": n("1=1"), "blocked": n("COALESCE(blocked,0)=1"),
                      "active": {str(d): n("last_seen>=?", now - d * 86400) for d in frames},
                      "new": {str(d): n("first_seen>=?", now - d * 86400) for d in frames}})

        getData = staticmethod(lambda name, bot_id=None, api_key=None: Store("bot", _tbid(bot_id)).get(name))
        saveData = staticmethod(lambda name, data, bot_id=None, api_key=None: (Store("bot", _tbid(bot_id)).save(name, data), {"ok": True})[1])
        deleteData = staticmethod(lambda name, bot_id=None, api_key=None: (Store("bot", _tbid(bot_id)).delete(name), {"ok": True})[1])
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
            r = db().execute("SELECT * FROM bots WHERE id=?", (_tbid(bot_id),)).fetchone()
            return D({"bot_id": str(r["id"]), "botid": str(r["id"]), "id": r["id"], "name": r["name"], "username": r["username"],
                      "bot_username": r["username"], "bot_name": r["name"], "status": "working" if r["running"] else "stopped",
                      "token": r["token"], "creation_date": ts_fmt(r["created_at"])})

        @staticmethod
        def sendMessage(*a, **k):
            return bot._invoke("sendMessage", a, k)

        # panel-style: Bot.get / Bot.save
        get, save, delete = staticmethod(botstore.get), staticmethod(botstore.save), staticmethod(botstore.delete)

    class UserHigh:
        @staticmethod
        def _s(user, bot_id=None):
            return Store(f"user:{user if user is not None else UID}", _tbid(bot_id))

        @staticmethod
        def getData(name, user=None, data_type=None, bot_id=None, api_key=None):
            return UserHigh._s(user, bot_id).get(name)

        @staticmethod
        def saveData(name, data, user=None, data_type=None, bot_id=None, api_key=None):
            UserHigh._s(user, bot_id).save(name, data)
            return {"ok": True}

        @staticmethod
        def deleteData(name, user=None, data_type=None, bot_id=None, api_key=None):
            UserHigh._s(user, bot_id).delete(name)
            return {"ok": True}

        @staticmethod
        def deleteAllData(name=None, old_data=False, except_data=None):
            for k in list(userstore.all()):
                if k not in (except_data or []) and (name is None or k == name):
                    userstore.delete(k)

        @staticmethod
        def getDataFile(name, user=None, output_format="txt"):
            v = UserHigh._s(user).get(name)
            return _file(f"{name}.{output_format}", v if isinstance(v, str) else json.dumps(v, default=str, ensure_ascii=False))

        @staticmethod
        def getAllData(name=None, output_format="json", old_data=False):
            out = {}
            for r in db().execute("SELECT scope,k,v FROM kv WHERE bot_id=? AND scope LIKE 'user:%'", (BID,)):
                if name is None or r["k"] == name:
                    out.setdefault(r["scope"][5:], {})[r["k"]] = json.loads(r["v"])
            return _file("user_data." + output_format, json.dumps(out, indent=2, default=str, ensure_ascii=False))

        @staticmethod
        def getAllDataOfUser(user, output_format="json"):
            return io.BytesIO(json.dumps(Store(f"user:{user}").all(), indent=2, default=str).encode())

        @staticmethod
        def deleteAllDataOfUser(user):
            for k in list(Store(f"user:{user}").all()):
                Store(f"user:{user}").delete(k)

    class Res:
        def __init__(self, scope, name, bid=None):
            self.s, self.k = Store(scope, bid), "__res__" + name

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
        userRes = staticmethod(lambda name, user=None, bot_id=None, api_key=None: Res(f"user:{user if user is not None else UID}", name, _tbid(bot_id)))
        anotherRes = staticmethod(lambda name, user=None, bot_id=None, api_key=None: Res(f"user:{user if user is not None else UID}", name, _tbid(bot_id)))
        globalRes = staticmethod(lambda name, bot_id=None, api_key=None: Res("bot", name, _tbid(bot_id)))
        accountRes = staticmethod(lambda name: Res("account", name, 0))

    class Ads:
        """Rewarded ads are a Telebot Creator monetisation feature; here the reward is granted straight away."""
        reward_ad = staticmethod(lambda text=None, then=None, **k: True)
        claim = staticmethod(lambda *a, **k: True)

    # ---- Account (same names and answers as Telebot Creator: {"ok": …, "result": …}) ----
    def _okr(result=None, **extra):
        return D({"ok": True, "result": result, **extra})

    def _errr(msg):
        return D({"ok": False, "result": str(msg), "message": str(msg)})

    def _bid(x):
        try:
            return int(str(x).strip())
        except Exception:
            return None

    def ctl_call(action, bot_id=None, args=None, timeout=8):
        """Ask the panel to do something only it can (start a bot, clone one ...) and wait for the answer."""
        if DRY:
            return D({"ok": True, "result": "(test run: nothing was changed)"})
        a_ = dict(args or {})
        a_["_wait"] = True
        rid = db().execute("INSERT INTO ctl(bot_id,action,args,done) VALUES(?,?,?,0)",
                           (bot_id if bot_id is not None else BID, action, json.dumps(a_, default=str))).lastrowid
        t0 = time.time()
        while time.time() - t0 < timeout:
            r = db().execute("SELECT done,result FROM ctl WHERE id=?", (rid,)).fetchone()
            if r and r["done"]:
                db().execute("DELETE FROM ctl WHERE id=?", (rid,))
                return D(json.loads(r["result"]))
            time.sleep(0.04)
        return _errr("The panel did not answer in time")

    ts_fmt = lambda t: time.strftime("%H:%M:%S %d:%m:%Y", time.gmtime(t or time.time()))

    def _binfo(r):
        return {"botid": str(r["id"]), "name": r["name"], "username": r["username"],
                "status": "working" if r["running"] else "stopped", "creation_date": ts_fmt(r["created_at"]), "has_token": True}

    def _bot_row(botid):
        b_ = _bid(botid)
        return db().execute("SELECT * FROM bots WHERE id=?", (b_,)).fetchone() if b_ is not None else None

    def acc_bots_list():
        return _okr([_binfo(r) for r in db().execute("SELECT * FROM bots ORDER BY id")])

    def acc_bot_info(botid):
        r = _bot_row(botid)
        if not r:
            return _errr("Bot not found")
        d = _binfo(r)
        d["command_count"] = db().execute("SELECT COUNT(*) c FROM commands WHERE bot_id=?", (r["id"],)).fetchone()["c"]
        d["user_count"] = db().execute("SELECT COUNT(*) c FROM users WHERE bot_id=?", (r["id"],)).fetchone()["c"]
        d["points_used"] = 0
        return _okr(d)

    def acc_bot_status(botid):
        r = _bot_row(botid)
        return _okr({"status": "working" if r["running"] else "stopped"}) if r else _errr("Bot not found")

    def acc_get_bot_data(botid, name):
        r = _bot_row(botid)
        return _okr(Store("bot", r["id"]).get(name)) if r else _errr("Bot not found")

    def acc_set_bot_data(botid, name, data):
        r = _bot_row(botid)
        if not r:
            return _errr("Bot not found")
        Store("bot", r["id"]).save(name, data)
        return _okr("Data saved")

    def acc_command_list(botid):
        r = _bot_row(botid)
        if not r:
            return _errr("Bot not found")
        return _okr([{"command": c_["name"], "code_length": len(c_["code"] or ""), "has_code": bool((c_["code"] or "").strip())}
                     for c_ in db().execute("SELECT name, code FROM commands WHERE bot_id=? ORDER BY name", (r["id"],))])

    def acc_command_info(botid, command):
        c_ = _cmd_row(botid, command)
        return _okr({"command": c_["name"], "code": c_["code"], "code_length": len(c_["code"] or "")}) if c_ else _errr("Command not found")

    def acc_create_command(botid, command, code=""):
        r = _bot_row(botid)
        if not r:
            return _errr("Bot not found")
        if _cmd_row(botid, command):
            return _errr("A command with that name already exists")
        if not DRY:
            db().execute("INSERT INTO commands(bot_id,name,code,updated_at) VALUES(?,?,?,?)", (r["id"], command, code, time.time()))
            _ctl("reindex", r["id"])
        return _okr("Command created")

    def acc_edit_command(botid, command, code):
        c_ = _cmd_row(botid, command)
        if not c_:
            return _errr("Command not found")
        if not DRY:
            db().execute("UPDATE commands SET code=?, updated_at=? WHERE id=?", (code, time.time(), c_["id"]))
        return _okr("Command updated")

    def acc_delete_command(botid, command):
        r = delete_command(botid, command)
        return _okr("Command moved to the recycle bin", name=r.get("name")) if r.get("ok") else _errr(r.get("error"))

    def acc_restore_command(botid, key):
        r = restore_command(botid, key)
        return _okr("Command restored", name=r.get("name")) if r.get("ok") else _errr(r.get("error"))

    def acc_purge_command(botid, key=None):
        purge_command(botid, key)
        return _okr("Removed for good")

    def acc_deleted_commands(botid):
        return _okr([{"id": d["id"], "command": d["name"], "name": d["name"], "deleted_at": d["deleted_at"], "lines": d["lines"]}
                     for d in deleted_commands(botid)])

    def acc_deleted_bots():
        out = []
        for r in db().execute("SELECT id,name,username,deleted_at,n_commands,n_users FROM trash_bots ORDER BY deleted_at DESC"):
            left = max(0, int(p.get("trash_days", 30) - (time.time() - r["deleted_at"]) / 86400))
            out.append({"botid": str(r["id"]), "name": r["name"], "username": r["username"], "deleted_at": ts_fmt(r["deleted_at"]),
                        "commands_count": r["n_commands"], "users_count": r["n_users"], "days_remaining": left, "recoverable": True})
        return _okr(out)

    def acc_deleted_bots_stats():
        rows_ = db().execute("SELECT COUNT(*) c, COALESCE(SUM(n_commands),0) n FROM trash_bots").fetchone()
        return _okr({"total": rows_["c"], "total_commands": rows_["n"]})

    def _read_import(x):
        if not isinstance(x, (dict, list)) and hasattr(x, "read"):
            x = x.read()
        if isinstance(x, (bytes, bytearray)):
            if x[:2] == b"\x1f\x8b":
                import gzip
                x = gzip.decompress(x)
            x = x.decode("utf-8", "replace")
        if isinstance(x, str):
            t = x.strip()
            if t[:1] in "{[":
                data = json.loads(t)
            else:                                         # "=== /name ===" plain text export
                parts = re.split(r"(?m)^=== (.+?) ===$", t)
                data = {"commands": [{"command": n.strip(), "code": c_.strip("\n") + "\n"} for n, c_ in zip(parts[1::2], parts[2::2])]}
        else:
            data = x
        if isinstance(data, list):
            data = {"commands": data}
        out = dict(data)
        out["commands"] = [{"command": c_.get("command") or c_.get("name"), "name": c_.get("name") or c_.get("command"),
                            "code": c_.get("code", "")} for c_ in data.get("commands", [])]
        return out

    def acc_parse_import_data(import_file, file_format="json"):
        try:
            return D(_read_import(import_file))
        except Exception as e:
            return _errr(f"Could not read the import data: {e}")

    def acc_import_commands(import_data, botid, remove_old_command=False, format="json"):
        r = _bot_row(botid)
        if not r:
            return _errr("Bot not found")
        try:
            data = _read_import(import_data)
        except Exception as e:
            return _errr(f"Could not read the import data: {e}")
        if DRY:
            return _okr(f"Would import {len(data['commands'])} commands", command_count=len(data["commands"]))
        if remove_old_command:
            for c_ in db().execute("SELECT * FROM commands WHERE bot_id=?", (r["id"],)).fetchall():
                db().execute("INSERT INTO trash_commands(bot_id,name,code,folder,deleted_at) VALUES(?,?,?,?,?)",
                             (r["id"], c_["name"], c_["code"], c_["folder"], time.time()))
                db().execute("DELETE FROM commands WHERE id=?", (c_["id"],))
        n = 0
        for c_ in data["commands"]:
            if not c_["name"]:
                continue
            hit = _cmd_row(r["id"], c_["name"])
            if hit and nraw_(hit["name"]) == nraw_(c_["name"]):
                db().execute("UPDATE commands SET code=?, updated_at=? WHERE id=?", (c_["code"], time.time(), hit["id"]))
            else:
                db().execute("INSERT INTO commands(bot_id,name,code,updated_at) VALUES(?,?,?,?)", (r["id"], c_["name"], c_["code"], time.time()))
            n += 1
        _ctl("reindex", r["id"])
        return _okr(f"Imported {n} commands", command_count=n, imported=n)

    def acc_export_bot(botid, format="json", include_bot_data=False):
        r = _bot_row(botid)
        if not r:
            return _errr("Bot not found")
        doc = {"bot": {"name": r["name"], "username": r["username"], "version": r["version"] or "1.0.0"},
               "commands": [{"command": c_["name"], "name": c_["name"], "code": c_["code"]}
                            for c_ in db().execute("SELECT name, code FROM commands WHERE bot_id=? ORDER BY name", (r["id"],))],
               "export_info": {"source": "Botbench", "exported_at": ts_fmt(time.time())}}
        if include_bot_data:
            doc["global_data"] = [{"name": k, "data": v} for k, v in kv_all("bot", r["id"]).items()]
        return _file(f"{r['username']}_export.json", json.dumps(doc, indent=2, ensure_ascii=False, default=str))

    def acc_clone_bot(botid, new_token=None):
        return ctl_call("clone_bot", _bid(botid), {"token": new_token}) if _bot_row(botid) else _errr("Bot not found")

    def acc_import_bot(import_data, new_token=None, format="json"):
        try:
            data = _read_import(import_data)
        except Exception as e:
            return _errr(f"Could not read the import data: {e}")
        return ctl_call("import_bot", 0, {"token": new_token, "commands": data["commands"]})

    class _AccMeta(type):
        def __getattr__(cls, name):
            if name.startswith("_"):
                raise AttributeError(name)

            def na(*a, **k):
                raise NotImplementedError(f"Account.{name}() is a Telebot Creator platform function and isn't available here.")
            return na

    class Account(metaclass=_AccMeta):
        """Telebot Creator's Account functions, working on the bots of this panel."""
        # account data (shared by all bots)
        saveData = staticmethod(lambda name, data: (Store("account", 0).save(name, data), _okr("Added new data", ok="true"))[1])
        getData = staticmethod(lambda name=None: Store("account", 0).get(name) if name is not None else Store("account", 0).all())
        deleteData = staticmethod(lambda name: (Store("account", 0).delete(name), _okr("deleted"))[1])
        getAllData = staticmethod(lambda name=None, output_format="json": _file("account_data." + output_format, json.dumps(Store("account", 0).all(), indent=2, default=str, ensure_ascii=False)))
        getDataFile = staticmethod(lambda name, output_format="txt": _file(f"{name}.{output_format}", (lambda v: v if isinstance(v, str) else json.dumps(v, default=str, ensure_ascii=False))(Store("account", 0).get(name))))
        deleteAllData = staticmethod(lambda except_data=None, include_bot_data=False: ([Store("account", 0).delete(k) for k in list(Store("account", 0).all()) if k not in (except_data or [])], _okr("deleted"))[1])
        info = staticmethod(lambda: D({"ok": True, "email": "owner@botbench", "plan": "Self-hosted", "points_left": 10 ** 9,
                                      "result": {"email": "owner@botbench", "plan": "Self-hosted", "points_left": 10 ** 9}}))
        # bots
        get_bots_list = staticmethod(acc_bots_list)
        get_bot_info = staticmethod(acc_bot_info)
        get_bot_status = staticmethod(acc_bot_status)
        get_bot_data = staticmethod(acc_get_bot_data)
        set_bot_data = staticmethod(acc_set_bot_data)
        start_bot = staticmethod(lambda botid: ctl_call("start", _bid(botid)))
        stop_bot = staticmethod(lambda botid: ctl_call("stop", _bid(botid)))
        restart_bot = staticmethod(lambda botid: ctl_call("restart", _bid(botid)))
        create_bot = staticmethod(lambda bot_token, bot_name=None, bot_username=None, **k: ctl_call("create_bot", 0, {"token": bot_token, "name": bot_name, "username": bot_username}))
        clone_bot = staticmethod(acc_clone_bot)
        import_bot = staticmethod(acc_import_bot)
        export_bot = staticmethod(acc_export_bot)
        parse_import_data = staticmethod(acc_parse_import_data)
        import_commands = staticmethod(acc_import_commands)
        # recycle bins
        delete_bot = staticmethod(lambda botid, permanent=False: ctl_call("delete_bot", _bid(botid), {"permanent": bool(permanent)}))
        recover_bot = staticmethod(lambda botid, new_token=None: ctl_call("recover_bot", _bid(botid), {"new_token": new_token}))
        permanent_delete_bot = staticmethod(lambda botid: ctl_call("permanent_delete_bot", _bid(botid)))
        clear_expired_bots = staticmethod(lambda: ctl_call("clear_expired_bots", 0))
        get_deleted_bots = staticmethod(acc_deleted_bots)
        get_deleted_bots_stats = staticmethod(acc_deleted_bots_stats)
        restore_bot = recover_bot
        purge_bot = permanent_delete_bot
        # commands
        get_command_list = staticmethod(acc_command_list)
        get_command_info = staticmethod(acc_command_info)
        create_command = staticmethod(acc_create_command)
        edit_command = staticmethod(acc_edit_command)
        delete_command = staticmethod(acc_delete_command)
        restore_command = staticmethod(acc_restore_command)
        purge_command = staticmethod(acc_purge_command)
        get_deleted_commands = staticmethod(acc_deleted_commands)
        get_bot_commands = staticmethod(lambda botid: [c_["name"] for c_ in db().execute("SELECT name FROM commands WHERE bot_id=? ORDER BY name", (_bid(botid) or 0,))])
        # helpers
        gen_random_id = staticmethod(lambda: uuid.uuid4().hex[:16])
        get_backup_url = staticmethod(backup_url)
        send_backup = staticmethod(lambda: _ctl("backup_now"))
        revoke_api = staticmethod(lambda: _errr("API keys are a Telebot Creator feature. Everything works without one here."))

    def MembershipCheck(chats, user):
        for c in (chats if isinstance(chats, (list, tuple)) else [chats]):
            try:
                if tgcall("getChatMember", chat_id=c, user_id=user).status not in ("member", "administrator", "creator"):
                    return False
            except Exception:
                return False
        return True

    bot = BotAPI()

    class Api:
        """Pages API: the command's HTTP answer when it was started by a webhook link."""
        setWebhookResult = staticmethod(lambda obj: state.__setitem__("webhook_result", obj))
        setResponse = setWebhookResult
    libs = _Namespace(DateAndTime=_DateAndTime(), Random=_Random(), Resources=Resources, tbcads=Ads,
                      Webhook=_Namespace(getUrlFor=staticmethod(webhook_url)))

    def mcp_emulate(url, payload):
        """Commands written for Telebot Creator call its MCP endpoint to replace a revoked bot token.
        Here that request is answered by the panel itself."""
        m_, rid = payload.get("method"), payload.get("id")
        if m_ == "initialize":
            body = {"jsonrpc": "2.0", "id": rid, "result": {"protocolVersion": "2025-03-26", "capabilities": {"tools": {}},
                                                              "serverInfo": {"name": "botbench", "version": "1"}}}
            return _fake_response(url, json.dumps(body), headers={"Mcp-Session-Id": uuid.uuid4().hex})
        if m_ and m_.startswith("notifications/"):
            return _fake_response(url, "", 202)
        if m_ == "tools/call" and (payload.get("params") or {}).get("name") == "replace_bot_tokens":
            toks = (payload["params"].get("arguments") or {}).get("tokens")
            toks = toks if isinstance(toks, list) else [t for t in re.split(r"[\s,]+", str(toks or "")) if t]
            outs, ok_all = [], bool(toks)
            for t in toks:
                r_ = ctl_call("replace_token", 0, {"token": t}, timeout=8)
                ok_all = ok_all and bool(r_.get("ok"))
                outs.append(r_.get("result") if r_.get("ok") else f"Error: {r_.get('result')}")
            text = "\n".join(outs) or "Error: no token given"
            body = {"jsonrpc": "2.0", "id": rid, "result": {"content": [{"type": "text", "text": text}], "isError": not ok_all}}
            return _fake_response(url, json.dumps(body, ensure_ascii=False))
        body = {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "This Telebot Creator tool is not available in Botbench"}}
        return _fake_response(url, json.dumps(body))

    class _JobHTTP(_HTTP):
        def post(self, url, **kw):
            if str(url).startswith("https://api.telebotcreator.com/v2/mcp/"):
                return mcp_emulate(str(url), kw.get("json") or {})
            return super().post(url, **kw)

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
            "left_points": 10 ** 9, "Api": Api, "callback_query": message if ctx.get("update_type") == "callback_query" else None, "wait_next": wait_next, "handleNextCommand": wait_next, "runCommand": runCommand,
            "libs": libs, "HTTP": _JobHTTP(), "requests": requests, "Account": Account, "MembershipCheck": MembershipCheck,
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

    def short_msg(e, line):
        if isinstance(e, SyntaxError):
            return f"Syntax Error: {e.msg} (<unknown>, line {line or e.lineno})"
        base = str(e) if isinstance(e, TelegramError) else f"{type(e).__name__}: {e}"
        return base.strip()[:500] + (f" (line {line})" if line else "")

    def note(name, e, line):
        errors.append({"command": name, "line": line, "short": short_msg(e, line),
                       "message": "".join(traceback.format_exception(type(e), e, e.__traceback__)).strip()})

    handled = [False]

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
                    handled[0] = True
                except ReturnCommand:
                    handled[0] = True
                except BaseException as e2:
                    note("!", e2, getattr(e2, "_cmd_info", (0, None))[1])
    if ctx.get("update_type") == "callback_query" and ctx.get("callback_query_id") and not state["answered"] and not DRY:
        try:                      # like Telebot Creator: a button press is always answered, so it stops "loading"
            tgcall("answerCallbackQuery", callback_query_id=ctx["callback_query_id"])
        except Exception:
            pass
        state["answered"] = True
    first = errors[0] if errors else None
    return {"answered": state["answered"], "handled": handled[0], "webhook_result": state.get("webhook_result"), "actions": actions, "pending": state["pending"], "scheduled": state["scheduled"],
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
            tb = traceback.format_exc()
            res = {"actions": [], "pending": None, "scheduled": [], "stdout": "", "error_line": None, "error": tb,
                   "errors": [{"command": "(runtime)", "message": tb, "short": tb.strip().splitlines()[-1][:300], "line": None}]}
        proto_out.write(json.dumps(res, default=str) + "\n")
        proto_out.flush()


if __name__ == "__main__":
    serve()
