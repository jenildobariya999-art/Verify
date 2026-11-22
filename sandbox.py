"""Runs ONE command's Python code in its own process.

Reads a JSON payload on stdin, executes the code with the Botbench helpers
in scope, and prints a JSON result after a marker line. The parent process
(core.py) performs the Telegram API calls and saves the data.
"""
import sys, json, io, traceback, contextlib

MARK = "\x00RESULT\x00"


class D(dict):
    """dict with attribute access: message.text == message["text"]"""
    def __getattr__(self, k):
        v = self.get(k)
        return D(v) if isinstance(v, dict) else v


def main():
    p = json.loads(sys.stdin.read())
    ctx = p["ctx"]
    actions, state = [], {"pending": None}
    CHAT = ctx["chat"]["id"]

    def add(method, params):
        actions.append({"method": method,
                        "params": {k: v for k, v in params.items() if v is not None}})

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
            return {"keyboard": [[{"text": str(k)} for k in row] for row in keyboard],
                    "resize_keyboard": True}
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
        state["pending"] = command if command.startswith(("/", "*", "cb:")) else "/" + command

    class Bot(Store):
        username = p["bot_info"].get("username")
        name = p["bot_info"].get("name")

        def send(self, text, chat_id=None, parse_mode=None, inline=None,
                 keyboard=None, remove_keyboard=False, reply=False, **extra):
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
            add("sendDocument", dict(chat_id=chat_id or CHAT, document=document,
                                     caption=caption, reply_markup=markup(inline), **extra))

        def edit(self, text, inline=None, parse_mode=None):
            """Edit the message a button was attached to (callback commands)."""
            add("editMessageText", dict(chat_id=CHAT, message_id=ctx.get("message_id"),
                                        text=str(text), parse_mode=parse_mode,
                                        reply_markup=markup(inline)))

        def answer(self, text=None, alert=False):
            """Answer a button press (stops the loading spinner)."""
            add("answerCallbackQuery", dict(callback_query_id=ctx.get("callback_query_id"),
                                            text=text, show_alert=alert or None))

        def call(self, method, **params):
            """Raw Telegram Bot API call, e.g. bot.call("sendDice", chat_id=chat.id)."""
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

    ns = {
        "__name__": "__command__",
        "bot": bot, "user": user,
        "message": D(ctx["message"]), "chat": D(ctx["chat"]),
        "text": ctx["text"], "args": ctx["args"], "data": ctx.get("data"),
        "wait_next": wait_next,
    }
    try:
        import requests
        ns["requests"] = requests
    except Exception:
        pass

    buf, error = io.StringIO(), None
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        try:
            exec(compile(p["code"], "<command>", "exec"), ns)
        except BaseException:
            error = traceback.format_exc()

    result = {"actions": actions, "bot_save": bot.saved, "user_save": user.saved,
              "pending": state["pending"], "stdout": buf.getvalue()[-4000:], "error": error}
    sys.stdout.write(MARK + json.dumps(result, default=str))


main()
