import os, secrets, hmac
import jinja2
from functools import wraps
from flask import Flask, request, jsonify, session, render_template, redirect, send_from_directory
import core

core.init_db()

PASSWORD = os.environ.get("PANEL_PASSWORD", "admin")
secret_file = os.path.join(core.BASE, ".secret")
if not os.path.exists(secret_file):
    with open(secret_file, "w") as f:
        f.write(secrets.token_hex(32))
app = Flask(__name__)
# Pages live in templates/, but also work if the HTML files sit next to app.py
PAGE_DIRS = [os.path.join(core.BASE, "templates"), core.BASE]
app.jinja_loader = jinja2.FileSystemLoader(PAGE_DIRS)
app.secret_key = open(secret_file).read().strip()


def auth(f):
    @wraps(f)
    def wrapper(*a, **kw):
        if not session.get("ok"):
            return jsonify(error="Sign in required"), 401
        return f(*a, **kw)
    return wrapper


def norm_name(name):
    name = (name or "").strip().lower().replace(" ", "_")
    if not name:
        return ""
    return name if name.startswith(("/", "*", "cb:")) else "/" + name


def bot_view(b):
    n = core.one("SELECT COUNT(*) c FROM users WHERE bot_id=?", (b["id"],))["c"]
    return {"id": b["id"], "name": b["name"], "username": b["username"],
            "running": core.is_running(b["id"]), "users": n, "token_hint": b["token"][-4:]}


@app.get("/")
def index():
    if not session.get("ok"):
        return render_template("login.html", error=None)
    for d in PAGE_DIRS:
        if os.path.exists(os.path.join(d, "panel.html")):
            return send_from_directory(d, "panel.html")
    return "panel.html is missing from the deployment.", 500


@app.post("/login")
def login():
    if hmac.compare_digest(request.form.get("password", ""), PASSWORD):
        session["ok"] = True
        return redirect("/")
    return render_template("login.html", error="That password isn't right."), 401


@app.get("/logout")
def logout():
    session.clear()
    return redirect("/")


# ---------- bots ----------
@app.get("/api/bots")
@auth
def bots():
    return jsonify([bot_view(b) for b in core.rows("SELECT * FROM bots ORDER BY id")])


@app.post("/api/bots")
@auth
def add_bot():
    token = (request.json.get("token") or "").strip()
    if core.one("SELECT 1 FROM bots WHERE token=?", (token,)):
        return jsonify(error="This bot is already in the panel."), 400
    try:
        me = core.tg(token, "getMe")
    except Exception as e:
        return jsonify(error=f"Telegram rejected this token: {e}"), 400
    bid = core.run("INSERT INTO bots(name, username, token) VALUES(?,?,?)",
                   (me.get("first_name"), me.get("username"), token))
    core.run("INSERT INTO commands(bot_id, name, code) VALUES(?,?,?)",
             (bid, "/start", core.DEFAULT_START))
    core.run("INSERT INTO commands(bot_id, name, code) VALUES(?,?,?)",
             (bid, "cb:hello", 'bot.answer("Hello!")\nbot.edit("Hello right back 👋")\n'))
    core.run("INSERT INTO commands(bot_id, name, code) VALUES(?,?,?)",
             (bid, "cb:ask", 'bot.answer()\nbot.send("What is your name?")\nwait_next("/got_name")\n'))
    core.run("INSERT INTO commands(bot_id, name, code) VALUES(?,?,?)",
             (bid, "/got_name", 'user.save("name", text)\nbot.send(f"Nice to meet you, {text}!")\n'))
    return jsonify(bot_view(core.one("SELECT * FROM bots WHERE id=?", (bid,))))


@app.delete("/api/bots/<int:bid>")
@auth
def del_bot(bid):
    core.stop_bot(bid)
    for t in ("commands", "kv", "pending", "users", "logs"):
        core.run(f"DELETE FROM {t} WHERE bot_id=?", (bid,))
    core.run("DELETE FROM bots WHERE id=?", (bid,))
    return jsonify(ok=True)


@app.post("/api/bots/<int:bid>/<action>")
@auth
def bot_action(bid, action):
    if action == "start":
        core.start_bot(bid)
    elif action == "stop":
        core.stop_bot(bid)
    else:
        return jsonify(error="Unknown action"), 404
    return jsonify(bot_view(core.one("SELECT * FROM bots WHERE id=?", (bid,))))


# ---------- commands ----------
@app.get("/api/bots/<int:bid>/commands")
@auth
def commands(bid):
    return jsonify(core.rows("SELECT * FROM commands WHERE bot_id=? ORDER BY name", (bid,)))


@app.post("/api/bots/<int:bid>/commands")
@auth
def add_command(bid):
    name = norm_name(request.json.get("name"))
    if not name:
        return jsonify(error="Give the command a name."), 400
    if core.one("SELECT 1 FROM commands WHERE bot_id=? AND name=?", (bid, name)):
        return jsonify(error=f"{name} already exists."), 400
    cid = core.run("INSERT INTO commands(bot_id, name, code) VALUES(?,?,?)",
                   (bid, name, request.json.get("code") or "bot.send(\"Hello!\")\n"))
    return jsonify(core.one("SELECT * FROM commands WHERE id=?", (cid,)))


@app.put("/api/commands/<int:cid>")
@auth
def save_command(cid):
    cmd = core.one("SELECT * FROM commands WHERE id=?", (cid,))
    name = norm_name(request.json.get("name")) or cmd["name"]
    clash = core.one("SELECT 1 FROM commands WHERE bot_id=? AND name=? AND id<>?",
                     (cmd["bot_id"], name, cid))
    if clash:
        return jsonify(error=f"{name} already exists."), 400
    core.run("UPDATE commands SET name=?, code=? WHERE id=?",
             (name, request.json.get("code", cmd["code"]), cid))
    return jsonify(core.one("SELECT * FROM commands WHERE id=?", (cid,)))


@app.delete("/api/commands/<int:cid>")
@auth
def del_command(cid):
    core.run("DELETE FROM commands WHERE id=?", (cid,))
    return jsonify(ok=True)


@app.post("/api/commands/<int:cid>/test")
@auth
def test_command(cid):
    """Dry run: executes the code but sends nothing to Telegram and saves nothing."""
    cmd = core.one("SELECT * FROM commands WHERE id=?", (cid,))
    bot = core.one("SELECT * FROM bots WHERE id=?", (cmd["bot_id"],))
    code = request.json.get("code", cmd["code"])
    text = request.json.get("text") or cmd["name"]
    user = {"id": 0, "first_name": "Tester", "username": "tester"}
    chat = {"id": 0, "type": "private"}
    msg = {"message_id": 1, "from": user, "chat": chat, "text": text}
    data = text if cmd["name"].startswith("cb:") else None
    ctx = core.build_ctx(user, chat, msg, text, data, "test")
    res = core.execute(bot, code, ctx)
    return jsonify(res)


@app.get("/api/bots/<int:bid>/logs")
@auth
def logs(bid):
    return jsonify(core.rows("SELECT ts, level, text FROM logs WHERE bot_id=? "
                             "ORDER BY id DESC LIMIT 150", (bid,)))


if __name__ == "__main__":
    core.autostart()
    # Hosting platforms (Render, Railway...) set PORT and need 0.0.0.0
    default_host = "0.0.0.0" if os.environ.get("PORT") else "127.0.0.1"
    app.run(host=os.environ.get("HOST", default_host),
            port=int(os.environ.get("PORT", "5000")), threaded=True)
