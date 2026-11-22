"""Long-lived worker: runs command code for many jobs, one JSON job per line.

core.py keeps a small pool of these processes warm, so a command starts in
milliseconds instead of paying Python start-up cost on every message.
A worker that times out or crashes is killed and replaced by core.py.
"""
import sys, os, json, io, traceback, contextlib


class D(dict):
    """dict with attribute access: message.text == message["text"]"""
    def __getattr__(self, k):
        v = self.get(k)
        return D(v) if isinstance(v, dict) else v


def run_job(p):
    ctx = p["ctx"]
    actions, state = [], {"pending": None}
    CHAT = ctx["chat"]["id"]

    def add(method, params):
        actions.append({"method": method, "params": {k: v for k, v in params.items() if v is not None}})

    def button(b):
        if isinstance(b, dict):
            return b
        label, val = b[0], b[1]
        if str(val).startswith(("http://", "https://", "tg://")):
            return {"text": str(label), "url": str(val)}
        return {"text": str(label), "callback_data": str(val)[:64]}

    def markup(inline=None, keyboard=None, remove_keyboard=False):
        if inline:
            if not isinstance(inline[0], list):
                inline = [inline]
            return {"inline_keyboard": [[button(b) for b in row] for row in inline]}
        if keyboard:
            return {"keyboard": [[{"text": str(k)} for k in row] for row in keyboard], "resize_keyboard": True}
        if remove_keyboard:
            return {"remove_keyboard": True}
        return None

    class Store:
        def __init__(self, data):
            self._data, self.saved = data, {}

        def get(self, key, default=None):
            return self._data.get(key, default)

        def save(self, key, value):
            self._data[key] = value
            self.saved[key] = value

        def delete(self, key):
            self._data.pop(key, None)
            self.saved[key] = None

        def all(self):
            return dict(self._data)

    def wait_next(command):
        """The user's next message runs this command instead of normal matching."""
        state["pending"] = command

    class Bot(Store):
        username = p["bot_info"].get("username")
        name = p["bot_info"].get("name")

        def send(self, text, chat_id=None, parse_mode=None, inline=None, keyboard=None,
                 remove_keyboard=False, reply=False, **extra):
            params = dict(chat_id=chat_id or CHAT, text=str(text), parse_mode=parse_mode,
                          reply_markup=markup(inline, keyboard, remove_keyboard), **extra)
            if reply and ctx.get("message_id"):
                params["reply_to_message_id"] = ctx["message_id"]
            add("sendMessage", params)

        def reply(self, text, **kw):
            self.send(text, reply=True, **kw)

        def photo(self, photo, caption=None, chat_id=None, inline=None, **extra):
            add("sendPhoto", dict(chat_id=chat_id or CHAT, photo=photo, caption=caption,
                                  reply_markup=markup(inline), **extra))

        def document(self, document, caption=None, chat_id=None, inline=None, **extra):
            add("sendDocument", dict(chat_id=chat_id or CHAT, document=document, caption=caption,
                                     reply_markup=markup(inline), **extra))

        def edit(self, text, inline=None, parse_mode=None):
            add("editMessageText", dict(chat_id=CHAT, message_id=ctx.get("message_id"), text=str(text),
                                        parse_mode=parse_mode, reply_markup=markup(inline)))

        def answer(self, text=None, alert=False):
            add("answerCallbackQuery", dict(callback_query_id=ctx.get("callback_query_id"), text=text,
                                            show_alert=alert or None))

        def call(self, method, **params):
            add(method, params)

        def wait_next(self, command):
            wait_next(command)

    class User(Store):
        pass

    uinfo = ctx["user"]
    user = User(p["user_data"])
    for k in ("id", "first_name", "last_name", "username", "language_code"):
        setattr(user, k, uinfo.get(k))
    bot = Bot(p["bot_data"])
    ns = {"__name__": "__command__", "bot": bot, "user": user, "message": D(ctx["message"]),
          "chat": D(ctx["chat"]), "text": ctx["text"], "args": ctx["args"], "data": ctx.get("data"),
          "wait_next": wait_next, "requests": _requests}
    ns.update(p.get("env") or {})

    buf, error, line = io.StringIO(), None, None
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        try:
            exec(compile(p["code"], "<command>", "exec"), ns)
        except BaseException as e:
            error = traceback.format_exc()
            for fr in traceback.extract_tb(e.__traceback__):
                if fr.filename == "<command>":
                    line = fr.lineno
            if isinstance(e, SyntaxError) and e.filename == "<command>":
                line = e.lineno
    return {"actions": actions, "bot_save": bot.saved, "user_save": user.saved, "pending": state["pending"],
            "stdout": buf.getvalue()[-4000:], "error": error, "error_line": line}


def serve():
    proto_in = os.fdopen(os.dup(0), "r", encoding="utf-8")
    proto_out = os.fdopen(os.dup(1), "w", encoding="utf-8")
    os.dup2(os.open(os.devnull, os.O_RDWR), 0)   # command code can't read our protocol
    os.dup2(2, 1)                                 # stray prints can't corrupt it either
    sys.stdin = open(os.devnull)
    for line in proto_in:
        try:
            res = run_job(json.loads(line))
        except BaseException:
            res = {"actions": [], "bot_save": {}, "user_save": {}, "pending": None, "stdout": "",
                   "error": traceback.format_exc(), "error_line": None}
        proto_out.write(json.dumps(res, default=str) + "\n")
        proto_out.flush()


try:
    import requests as _requests
except Exception:
    _requests = None

if __name__ == "__main__":
    serve()
