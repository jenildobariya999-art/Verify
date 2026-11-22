import os, secrets, hmac, re, json, time, threading
import yaml
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


# ---------- admin: stats, users, broadcasts ----------
def _bot(bid):
    return core.one("SELECT * FROM bots WHERE id=?", (bid,))


@app.get("/api/bots/<int:bid>/stats")
@auth
def stats(bid):
    now = time.time()
    day = 86400

    def n(where, *args):
        return core.one(f"SELECT COUNT(*) c FROM users WHERE bot_id=? AND {where}", (bid, *args))["c"]
    cmds = core.rows("SELECT name, code FROM commands WHERE bot_id=? ORDER BY name", (bid,))
    return jsonify(
        total=n("1=1"), blocked=n("COALESCE(blocked,0)=1"),
        active={d: n("last_seen>=?", now - d * day) for d in (1, 7, 30)},
        new={d: n("first_seen>=?", now - d * day) for d in (1, 7, 30)},
        commands=[{"name": c["name"], "lines": len(c["code"].splitlines())} for c in cmds])


@app.get("/api/bots/<int:bid>/users")
@auth
def users(bid):
    q = "%" + (request.args.get("q") or "") + "%"
    return jsonify(core.rows(
        "SELECT user_id, first_name, username, last_seen, COALESCE(blocked,0) blocked FROM users "
        "WHERE bot_id=? AND (CAST(user_id AS TEXT) LIKE ? OR first_name LIKE ? OR username LIKE ?) "
        "ORDER BY last_seen DESC LIMIT 200", (bid, q, q, q)))


@app.get("/api/bots/<int:bid>/broadcasts")
@auth
def broadcasts(bid):
    return jsonify(core.rows("SELECT * FROM broadcasts WHERE bot_id=? ORDER BY id DESC LIMIT 30", (bid,)))


@app.post("/api/bots/<int:bid>/broadcasts")
@auth
def broadcast(bid):
    text = (request.json.get("text") or "").strip()
    if not text:
        return jsonify(error="Write a message first."), 400
    core.send_broadcast(_bot(bid), text)
    return jsonify(ok=True)


@app.post("/api/bots/<int:bid>/import_users")
@auth
def import_users(bid):
    raw = request.json.get("content") or ""
    ids = sorted({int(x) for x in re.findall(r"\b\d{5,15}\b", raw)})
    if not ids:
        return jsonify(error="No Telegram user IDs found in that file."), 400
    core.import_users(_bot(bid), ids)
    return jsonify(count=len(ids))


# ---------- admin: bot data / user data ----------
@app.get("/api/bots/<int:bid>/kv")
@auth
def kv_list(bid):
    scope = request.args.get("scope", "bot")
    return jsonify(core.kv_all(bid, scope))


@app.get("/api/bots/<int:bid>/kv_users")
@auth
def kv_users(bid):
    return jsonify(core.rows(
        "SELECT REPLACE(k.scope,'user:','') user_id, u.first_name, u.username, COUNT(*) n FROM kv k "
        "LEFT JOIN users u ON u.bot_id=k.bot_id AND CAST(u.user_id AS TEXT)=REPLACE(k.scope,'user:','') "
        "WHERE k.bot_id=? AND k.scope LIKE 'user:%' GROUP BY k.scope ORDER BY n DESC LIMIT 200", (bid,)))


@app.put("/api/bots/<int:bid>/kv")
@auth
def kv_put(bid):
    j = request.json
    try:
        val = json.loads(j.get("value"))
    except Exception:
        val = j.get("value")
    core.kv_apply(bid, j["scope"], {j["key"]: val})
    return jsonify(ok=True)


@app.delete("/api/bots/<int:bid>/kv")
@auth
def kv_del(bid):
    core.kv_apply(bid, request.args["scope"], {request.args["key"]: None})
    return jsonify(ok=True)


# ---------- settings ----------
@app.get("/api/bots/<int:bid>/token")
@auth
def get_token(bid):
    return jsonify(token=_bot(bid)["token"])


@app.post("/api/bots/<int:bid>/token")
@auth
def update_token(bid):
    token = (request.json.get("token") or "").strip()
    b = _bot(bid)
    try:
        me = core.tg(token, "getMe")
    except Exception as e:
        return jsonify(error=f"Telegram rejected this token: {e}"), 400
    if token.split(":")[0] != b["token"].split(":")[0]:
        return jsonify(error="That token belongs to a different bot."), 400
    was = core.is_running(bid)
    core.stop_bot(bid)
    core.run("UPDATE bots SET token=?, name=?, username=? WHERE id=?",
             (token, me.get("first_name"), me.get("username"), bid))
    if was:
        core.start_bot(bid)
    return jsonify(ok=True)


@app.post("/api/bots/<int:bid>/settings")
@auth
def settings(bid):
    core.run("UPDATE bots SET translate=? WHERE id=?", (1 if request.json.get("translate") else 0, bid))
    return jsonify(ok=True)


@app.get("/api/bots/<int:bid>/translate")
@auth
def get_translate(bid):
    return jsonify(translate=bool(_bot(bid)["translate"]))


@app.post("/api/bots/<int:bid>/transfer")
@auth
def transfer(bid):
    return jsonify(error="Transfers need separate panel accounts, and this panel has a single owner login."), 400


@app.post("/api/bots/<int:bid>/clone")
@auth
def clone(bid):
    token = (request.json.get("token") or "").strip()
    if core.one("SELECT 1 FROM bots WHERE token=?", (token,)):
        return jsonify(error="That bot is already in the panel."), 400
    try:
        me = core.tg(token, "getMe")
    except Exception as e:
        return jsonify(error=f"Telegram rejected this token: {e}"), 400
    nb = core.run("INSERT INTO bots(name, username, token) VALUES(?,?,?)",
                  (me.get("first_name"), me.get("username"), token))
    for c in core.rows("SELECT name, code FROM commands WHERE bot_id=?", (bid,)):
        core.run("INSERT INTO commands(bot_id, name, code) VALUES(?,?,?)", (nb, c["name"], c["code"]))
    core.kv_apply(nb, "bot", core.kv_all(bid, "bot"))
    return jsonify(id=nb)


# ---------- export / import commands ----------
def _str_rep(d, s):
    return d.represent_scalar("tag:yaml.org,2002:str", s, style="|" if "\n" in s else None)


yaml.add_representer(str, _str_rep, Dumper=yaml.SafeDumper)


@app.get("/api/bots/<int:bid>/export")
@auth
def export(bid):
    fmt = request.args.get("format", "json")
    b = _bot(bid)
    cmds = [{"name": c["name"], "code": c["code"]} for c in
            core.rows("SELECT name, code FROM commands WHERE bot_id=? ORDER BY name", (bid,))]
    doc = {"commands": cmds}
    if request.args.get("commands_only") != "1":
        doc = {"bot": {"name": b["name"], "username": b["username"]}, **doc}
    if request.args.get("bot_data") == "1":
        doc["bot_data"] = core.kv_all(bid, "bot")
    if fmt == "yaml":
        body = yaml.safe_dump(doc, sort_keys=False, allow_unicode=True)
    elif fmt == "txt":
        body = "".join(f"### {c['name']}\n{c['code'].rstrip()}\n\n" for c in cmds)
        if "bot_data" in doc:
            body += "### @bot_data\n" + json.dumps(doc["bot_data"], indent=2) + "\n"
    else:
        fmt, body = "json", json.dumps(doc, indent=2, ensure_ascii=False)
    return app.response_class(body, mimetype="text/plain", headers={
        "Content-Disposition": f'attachment; filename="{b["username"]}_export.{fmt}"'})


@app.post("/api/bots/<int:bid>/import")
@auth
def import_cmds(bid):
    j = request.json
    fmt, text = j.get("format", "json"), j.get("content") or ""
    try:
        if fmt == "txt":
            parts = re.split(r"(?m)^### (.+)$", text)
            data = {"commands": [], "bot_data": None}
            for name, code in zip(parts[1::2], parts[2::2]):
                if name.strip() == "@bot_data":
                    data["bot_data"] = json.loads(code)
                else:
                    data["commands"].append({"name": name.strip(), "code": code.strip("\n") + "\n"})
        else:
            data = yaml.safe_load(text) if fmt == "yaml" else json.loads(text)
        cmds = data["commands"]
    except Exception as e:
        return jsonify(error=f"Couldn't read that file as {fmt.upper()}: {e}"), 400
    if j.get("replace"):
        core.run("DELETE FROM commands WHERE bot_id=?", (bid,))
    added = updated = 0
    for c in cmds:
        name = norm_name(c.get("name"))
        if not name:
            continue
        if core.one("SELECT 1 FROM commands WHERE bot_id=? AND name=?", (bid, name)):
            core.run("UPDATE commands SET code=? WHERE bot_id=? AND name=?", (c.get("code", ""), bid, name))
            updated += 1
        else:
            core.run("INSERT INTO commands(bot_id, name, code) VALUES(?,?,?)", (bid, name, c.get("code", "")))
            added += 1
    if data.get("bot_data"):
        core.kv_apply(bid, "bot", data["bot_data"])
    return jsonify(added=added, updated=updated)


if __name__ == "__main__":
    core.autostart()
    # Hosting platforms (Render, Railway...) set PORT and need 0.0.0.0
    default_host = "0.0.0.0" if os.environ.get("PORT") else "127.0.0.1"
    app.run(host=os.environ.get("HOST", default_host),
            port=int(os.environ.get("PORT", "5000")), threaded=True)
