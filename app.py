import os, secrets, hmac, re, json, time, threading, base64
import yaml
import requests
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
    """Names are kept as typed ('hii', '/start', 'Link Wallet', 'cb:ok', '*')."""
    return " ".join((name or "").split())


def _exists(bid, name, exclude=None):
    k = core.nraw(name)
    return any(core.nraw(r["name"]) == k for r in core.rows("SELECT id, name FROM commands WHERE bot_id=?", (bid,))
               if r["id"] != exclude)


def bot_view(b):
    n = core.one("SELECT COUNT(*) c FROM users WHERE bot_id=?", (b["id"],))["c"]
    return {"id": b["id"], "name": b["name"], "username": b["username"],
            "running": core.is_running(b["id"]), "users": n, "token_hint": b["token"][-4:]}


@app.before_request
def remember_public_url():
    if not os.environ.get("PUBLIC_URL") and request.path in ("/", "/login"):
        proto = request.headers.get("X-Forwarded-Proto", request.scheme)
        core.set_setting("base_url", f"{proto}://{request.host}")


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
    core.start_bot(bid)
    return jsonify(bot_view(core.one("SELECT * FROM bots WHERE id=?", (bid,))))


@app.delete("/api/bots/<int:bid>")
@auth
def del_bot(bid):
    """Moves the bot (commands, data, users, token) to the recycle bin; it can be restored for TRASH_DAYS days."""
    if not core.trash_bot(bid):
        return jsonify(error="No such bot."), 404
    return jsonify(ok=True, days=core.TRASH_DAYS)


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
    if _exists(bid, name):
        return jsonify(error=f"{name} already exists."), 400
    cid = core.run("INSERT INTO commands(bot_id, name, code, folder, updated_at) VALUES(?,?,?,?,?)",
                   (bid, name, request.json.get("code") or "", request.json.get("folder"), time.time()))
    core.invalidate(bid)
    return jsonify(core.one("SELECT * FROM commands WHERE id=?", (cid,)))


@app.put("/api/commands/<int:cid>")
@auth
def save_command(cid):
    cmd = core.one("SELECT * FROM commands WHERE id=?", (cid,))
    name = norm_name(request.json.get("name")) or cmd["name"]
    if _exists(cmd["bot_id"], name, cid):
        return jsonify(error=f"{name} already exists."), 400
    core.run("UPDATE commands SET name=?, code=?, updated_at=? WHERE id=?",
             (name, request.json.get("code", cmd["code"]), time.time(), cid))
    core.invalidate(cmd["bot_id"])
    return jsonify(core.one("SELECT * FROM commands WHERE id=?", (cid,)))


@app.delete("/api/commands/<int:cid>")
@auth
def del_command(cid):
    c = core.one("SELECT bot_id FROM commands WHERE id=?", (cid,))
    if c:
        core.trash_commands(c["bot_id"], [cid])
    return jsonify(ok=True)


@app.post("/api/commands/<int:cid>/test")
@auth
def test_command(cid):
    """Dry run: nothing is sent to Telegram and nothing is saved."""
    cmd = core.one("SELECT * FROM commands WHERE id=?", (cid,))
    bot = core.one("SELECT * FROM bots WHERE id=?", (cmd["bot_id"],))
    code = request.json.get("code", cmd["code"])
    text = request.json.get("text") or cmd["name"]
    user = {"id": int(request.json.get("user_id") or 0), "first_name": "Tester", "username": "tester"}
    chat = {"id": user["id"], "type": "private"}
    msg = {"message_id": 1, "from": user, "chat": chat, "text": text}
    words, n = text.split(), len(cmd["name"].split())
    args = words[n:] if core.nk(" ".join(words[:n])) == core.nk(cmd["name"]) else words
    is_cb = cmd["name"].startswith("cb:") or bool(request.json.get("callback"))
    if is_cb:
        msg = {"id": "test", "from": user, "data": text, "message": msg, "chat": chat, "message_id": 1}
    ctx = core.make_ctx("callback_query" if is_cb else "message", msg, user, chat, text, args,
                        text if is_cb else None, "test", 1)
    return jsonify(core.execute(bot, {"name": cmd["name"], "code": code}, ctx, dry=True))


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
    if j.get("replace"):          # the old commands go to the recycle bin, not into thin air
        core.trash_commands(bid, [r["id"] for r in core.rows("SELECT id FROM commands WHERE bot_id=?", (bid,))])
    added = updated = 0
    for c in cmds:
        name = norm_name(c.get("name"))
        if not name:
            continue
        hit = next((r for r in core.rows("SELECT id, name FROM commands WHERE bot_id=?", (bid,))
                    if core.nraw(r["name"]) == core.nraw(name)), None)
        if hit:
            core.run("UPDATE commands SET code=?, updated_at=? WHERE id=?", (c.get("code", ""), time.time(), hit["id"]))
            updated += 1
        else:
            core.run("INSERT INTO commands(bot_id, name, code, updated_at) VALUES(?,?,?,?)", (bid, name, c.get("code", ""), time.time()))
            added += 1
    if data.get("bot_data"):
        core.kv_apply(bid, "bot", data["bot_data"])
    core.invalidate(bid)
    return jsonify(added=added, updated=updated)


# ---------- intro, errors, version, env ----------
def _count(sql, *args):
    return core.one(sql, args)["c"]


@app.get("/api/bots/<int:bid>/intro")
@auth
def intro(bid):
    b = _bot(bid)
    return jsonify(name=b["name"], username=b["username"], bot_id=str(b["id"]),
                   running=core.is_running(bid), uptime=core.uptime(bid), version=b.get("version") or "1.0.0",
                   users=_count("SELECT COUNT(*) c FROM users WHERE bot_id=?", bid),
                   commands=_count("SELECT COUNT(*) c FROM commands WHERE bot_id=?", bid),
                   errors=_count("SELECT COUNT(*) c FROM errors WHERE bot_id=?", bid),
                   last_active=core.one("SELECT MAX(last_seen) t FROM users WHERE bot_id=?", (bid,))["t"])


@app.post("/api/bots/<int:bid>/version")
@auth
def set_version(bid):
    v = (request.json.get("version") or "").strip()[:20]
    if not v:
        return jsonify(error="Enter a version, like 1.0.1."), 400
    core.run("UPDATE bots SET version=? WHERE id=?", (v, bid))
    return jsonify(ok=True)


@app.get("/api/bots/<int:bid>/errors")
@auth
def get_errors(bid):
    return jsonify(core.rows("SELECT * FROM errors WHERE bot_id=? ORDER BY id DESC LIMIT 100", (bid,)))


@app.delete("/api/bots/<int:bid>/errors")
@auth
def clear_errors(bid):
    core.run("DELETE FROM errors WHERE bot_id=?", (bid,))
    return jsonify(ok=True)


@app.get("/api/bots/<int:bid>/env")
@auth
def get_env(bid):
    return jsonify(core.rows("SELECT k, v, raw FROM env WHERE bot_id=? ORDER BY k", (bid,)))


@app.put("/api/bots/<int:bid>/env")
@auth
def put_env(bid):
    items = request.json.get("items") or []
    for it in items:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", it.get("k", "")):
            return jsonify(error=f"'{it.get('k')}' isn't a valid variable name (letters, digits, underscore)."), 400
        if it.get("raw"):
            try:
                json.loads(it["v"])
            except Exception:
                return jsonify(error=f"{it['k']} is marked raw but isn't valid JSON."), 400
    core.run("DELETE FROM env WHERE bot_id=?", (bid,))
    for it in items:
        core.run("INSERT OR REPLACE INTO env(bot_id,k,v,raw) VALUES(?,?,?,?)",
                 (bid, it["k"], it.get("v", ""), 1 if it.get("raw") else 0))
    return jsonify(ok=True)


# ---------- folders, bulk actions, search ----------
@app.get("/api/bots/<int:bid>/folders")
@auth
def folders(bid):
    names = {r["name"] for r in core.rows("SELECT name FROM folders WHERE bot_id=?", (bid,))}
    names |= {r["folder"] for r in core.rows("SELECT DISTINCT folder FROM commands WHERE bot_id=? AND folder<>''", (bid,))
              if r["folder"]}
    return jsonify(sorted(names, key=str.lower))


@app.post("/api/bots/<int:bid>/folders")
@auth
def add_folder(bid):
    j = request.json
    name = (j.get("name") or "").strip()[:40]
    if not name:
        return jsonify(error="Give the folder a name."), 400
    if j.get("old"):
        core.run("UPDATE commands SET folder=? WHERE bot_id=? AND folder=?", (name, bid, j["old"]))
        core.run("DELETE FROM folders WHERE bot_id=? AND name=?", (bid, j["old"]))
    core.run("INSERT OR IGNORE INTO folders(bot_id,name) VALUES(?,?)", (bid, name))
    return jsonify(ok=True)


@app.delete("/api/bots/<int:bid>/folders")
@auth
def del_folder(bid):
    name = request.args.get("name", "")
    core.run("UPDATE commands SET folder=NULL WHERE bot_id=? AND folder=?", (bid, name))
    core.run("DELETE FROM folders WHERE bot_id=? AND name=?", (bid, name))
    return jsonify(ok=True)


@app.post("/api/bots/<int:bid>/commands/bulk")
@auth
def bulk(bid):
    j = request.json
    ids = [int(i) for i in j.get("ids", [])]
    ph = ",".join("?" * len(ids)) or "NULL"
    if j.get("action") == "delete":
        core.trash_commands(bid, ids)
    elif j.get("action") == "move":
        core.run(f"UPDATE commands SET folder=? WHERE bot_id=? AND id IN ({ph})", (j.get("folder") or None, bid, *ids))
    core.invalidate(bid)
    return jsonify(ok=True)


def _pattern(j):
    q = j.get("q") or ""
    p = q if j.get("regex") else re.escape(q)
    if j.get("word"):
        p = r"\b" + p + r"\b"
    return re.compile(p, 0 if j.get("case") else re.I)


@app.post("/api/bots/<int:bid>/search")
@auth
def search_code(bid):
    try:
        pat = _pattern(request.json)
    except re.error as e:
        return jsonify(error=f"That pattern isn't valid: {e}"), 400
    out = []
    for c in core.rows("SELECT id, name, code FROM commands WHERE bot_id=? ORDER BY name", (bid,)):
        hits = [{"line": i, "text": ln.strip()[:200]} for i, ln in enumerate(c["code"].splitlines(), 1) if pat.search(ln)]
        if hits:
            out.append({"id": c["id"], "name": c["name"], "matches": hits[:20], "total": len(hits)})
    return jsonify(out)


@app.post("/api/bots/<int:bid>/replace")
@auth
def replace_code(bid):
    j = request.json
    try:
        pat = _pattern(j)
    except re.error as e:
        return jsonify(error=f"That pattern isn't valid: {e}"), 400
    r = j.get("replace") or ""
    n = files = 0
    for c in core.rows("SELECT id, code FROM commands WHERE bot_id=?", (bid,)):
        new, k = pat.subn(r if j.get("regex") else (lambda m: r), c["code"])
        if k:
            core.run("UPDATE commands SET code=? WHERE id=?", (new, c["id"]))
            n += k
            files += 1
    return jsonify(replaced=n, commands=files)


# ---------- users export ----------
@app.get("/api/bots/<int:bid>/export_users")
@auth
def export_users(bid):
    fmt = "csv" if request.args.get("format") == "csv" else "json"
    cols = ["user_id", "first_name", "username"]
    if request.args.get("last_active") == "1":
        cols.append("last_seen")
    if request.args.get("created") == "1":
        cols.append("first_seen")
    data = core.rows(f"SELECT {','.join(cols)} FROM users WHERE bot_id=? ORDER BY first_seen", (bid,))
    if fmt == "csv":
        import csv, io
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=cols)
        w.writeheader()
        w.writerows(data)
        body = buf.getvalue()
    else:
        body = json.dumps(data, indent=2, ensure_ascii=False)
    return app.response_class(body, mimetype="text/plain", headers={
        "Content-Disposition": f'attachment; filename="{_bot(bid)["username"]}_users.{fmt}"'})


# ---------- backups (More menu) ----------
def _download(body, name, mime="application/octet-stream"):
    return app.response_class(body, mimetype=mime, headers={"Content-Disposition": f'attachment; filename="{name}"',
                                                            "Cache-Control": "no-store"})


BACKUP_KINDS = ("all.json", "commands.json", "users.json", "users.csv", "commands.zip", "panel.db")


def _backup_response(kind):
    stamp = time.strftime("%Y%m%d_%H%M")
    if kind in ("all.json", "commands.json"):
        with_data = kind == "all.json"
        body = json.dumps(core.backup_doc(with_data, True), indent=1, ensure_ascii=False, default=str)
        return _download(body, f"botbench_backup_{stamp}{'' if with_data else '_commands_only'}.json", "application/json")
    if kind in ("users.json", "users.csv"):
        data = core.rows("SELECT b.username bot, u.user_id, u.first_name, u.username, u.first_seen, u.last_seen, "
                         "COALESCE(u.blocked,0) blocked FROM users u JOIN bots b ON b.id=u.bot_id ORDER BY b.id, u.first_seen")
        if kind == "users.csv":
            import csv, io
            buf = io.StringIO()
            w = csv.DictWriter(buf, fieldnames=["bot", "user_id", "first_name", "username", "first_seen", "last_seen", "blocked"])
            w.writeheader()
            w.writerows(data)
            return _download(buf.getvalue(), "all_bots_users.csv", "text/csv")
        return _download(json.dumps(data, indent=1, ensure_ascii=False), "all_bots_users.json", "application/json")
    if kind == "commands.zip":
        import zipfile, io
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for b in core.rows("SELECT * FROM bots ORDER BY id"):
                seen = set()
                for c in core.rows("SELECT name, code FROM commands WHERE bot_id=?", (b["id"],)):
                    base = re.sub(r"[^\w\-]+", "_", c["name"]).strip("_") or "command"
                    fn, i = base, 1
                    while fn.lower() in seen:
                        i += 1
                        fn = f"{base}_{i}"
                    seen.add(fn.lower())
                    z.writestr(f"{b['username']}/{fn}.py", f"# command: {c['name']}\n{c['code']}")
        return _download(buf.getvalue(), "all_commands_py.zip", "application/zip")
    if kind == "panel.db":
        import sqlite3, tempfile
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
        tmp.close()
        with core._lock:
            dst = sqlite3.connect(tmp.name)
            core._db.backup(dst)
            dst.close()
        body = open(tmp.name, "rb").read()
        os.unlink(tmp.name)
        return _download(body, f"botbench_{stamp}.db")
    return None


@app.get("/api/backup/all")
@auth
def backup_all():
    return _backup_response("all.json" if request.args.get("data", "1") == "1" else "commands.json")


@app.get("/api/backup/users")
@auth
def backup_users():
    return _backup_response("users.csv" if request.args.get("format") == "csv" else "users.json")


@app.get("/api/backup/commands.zip")
@auth
def backup_commands_zip():
    return _backup_response("commands.zip")


@app.get("/api/backup/db")
@auth
def backup_db():
    return _backup_response("panel.db")


@app.get("/api/backup/link")
@auth
def backup_link():
    t, base = core.backup_token(), core.base_url().rstrip("/")
    return jsonify(token=t, urls={k: f"{base}/backup/{t}/{k}" for k in BACKUP_KINDS})


@app.post("/api/backup/link/regenerate")
@auth
def backup_link_regen():
    core.backup_token(regenerate=True)
    return backup_link()


@app.get("/backup/<token>/<kind>")
def backup_by_link(token, kind):
    """The private 'database URL': anyone who has the link can download that backup, so treat it like a password."""
    if not hmac.compare_digest(token, core.backup_token()) or kind not in BACKUP_KINDS:
        return jsonify(error="not found"), 404
    return _backup_response(kind)


@app.post("/api/restore")
@auth
def restore():
    j = request.json
    try:
        raw = base64.b64decode(j["content_b64"]) if j.get("content_b64") else (j.get("content") or "")
        doc = core.load_backup(raw)
    except Exception as e:
        return jsonify(error=str(e) if isinstance(e, ValueError) else "That isn't a Botbench backup file."), 400
    return jsonify(**core.restore_doc(doc))


@app.post("/api/restore_url")
@auth
def restore_url():
    url = (request.json.get("url") or "").strip()
    if not re.match(r"^https?://", url):
        return jsonify(error="Paste a link that starts with http:// or https://"), 400
    try:
        r = requests.get(url, timeout=90)
        r.raise_for_status()
        if len(r.content) > 200 * 1024 * 1024:
            return jsonify(error="That file is bigger than 200 MB."), 400
        doc = core.load_backup(r.content)
    except ValueError as e:
        return jsonify(error=str(e)), 400
    except Exception as e:
        return jsonify(error=f"Couldn't read a backup from that link: {e}"), 400
    return jsonify(**core.restore_doc(doc))


@app.get("/api/backup/auto")
@auth
def auto_get():
    return jsonify(core.auto_cfg())


@app.put("/api/backup/auto")
@auth
def auto_put():
    j, cfg = request.json, core.auto_cfg()
    bid = j.get("bot_id")
    if j.get("enabled") and not (bid and core.one("SELECT 1 FROM bots WHERE id=?", (bid,)) and str(j.get("chat_id", "")).strip()):
        return jsonify(error="Choose a bot and enter your Telegram chat ID first."), 400
    cfg.update(enabled=bool(j.get("enabled")), bot_id=bid, chat_id=str(j.get("chat_id", "")).strip(),
               every_hours=max(1, min(168, int(j.get("every_hours") or 24))), with_data=bool(j.get("with_data", True)))
    core.set_auto(cfg)
    return jsonify(cfg)


@app.post("/api/backup/auto/run")
@auth
def auto_run():
    try:
        return jsonify(core.run_auto_backup())
    except Exception as e:
        cfg = core.auto_cfg()
        cfg["last_status"] = f"failed: {e}"[:200]
        core.set_auto(cfg)
        return jsonify(error=f"Couldn't send the backup: {e}"), 400


# ---------- recycle bins ----------
@app.get("/api/bots/<int:bid>/trash")
@auth
def cmd_trash(bid):
    return jsonify(core.rows("SELECT id, name, folder, deleted_at, length(code)-length(replace(code, char(10), ''))+1 AS lines "
                             "FROM trash_commands WHERE bot_id=? ORDER BY deleted_at DESC", (bid,)))


@app.post("/api/bots/<int:bid>/trash/<int:tid>/restore")
@auth
def cmd_trash_restore(bid, tid):
    name = core.restore_command(bid, tid)
    return (jsonify(name=name), 200) if name else (jsonify(error="Not in the recycle bin."), 404)


@app.delete("/api/bots/<int:bid>/trash/<int:tid>")
@auth
def cmd_trash_purge(bid, tid):
    core.run("DELETE FROM trash_commands WHERE id=? AND bot_id=?", (tid, bid))
    return jsonify(ok=True)


@app.delete("/api/bots/<int:bid>/trash")
@auth
def cmd_trash_empty(bid):
    core.run("DELETE FROM trash_commands WHERE bot_id=?", (bid,))
    return jsonify(ok=True)


@app.get("/api/trash/bots")
@auth
def bot_trash():
    out = core.rows("SELECT id, name, username, deleted_at, n_commands, n_users FROM trash_bots ORDER BY deleted_at DESC")
    for r in out:
        r["days_left"] = max(0, int(core.TRASH_DAYS - (time.time() - r["deleted_at"]) / 86400))
    return jsonify(out)


@app.post("/api/trash/bots/<int:bid>/restore")
@auth
def bot_trash_restore(bid):
    try:
        return jsonify(id=core.restore_trashed_bot(bid))
    except ValueError as e:
        return jsonify(error=str(e)), 400


@app.delete("/api/trash/bots/<int:bid>")
@auth
def bot_trash_purge(bid):
    core.purge_trashed_bot(bid)
    return jsonify(ok=True)


@app.get("/api/overview")
@auth
def overview():
    a_ = core.auto_cfg()
    return jsonify(errors=_count("SELECT COUNT(*) c FROM errors"), trash_bots=_count("SELECT COUNT(*) c FROM trash_bots"),
                   trash_commands=_count("SELECT COUNT(*) c FROM trash_commands"), trash_days=core.TRASH_DAYS,
                   auto={"enabled": a_["enabled"], "last_ts": a_["last_ts"], "last_status": a_["last_status"],
                         "every_hours": a_["every_hours"]},
                   bots={str(b["id"]): {"errors": _count("SELECT COUNT(*) c FROM errors WHERE bot_id=?", b["id"])}
                         for b in core.rows("SELECT id FROM bots")})


@app.post("/api/restart_all")
@auth
def restart_all():
    ids = [b["id"] for b in core.rows("SELECT id FROM bots") if core.is_running(b["id"])]
    for i in ids:
        core.stop_bot(i)
    time.sleep(0.3)
    for i in ids:
        core.start_bot(i)
    return jsonify(restarted=len(ids))


_STARTED = time.time()


@app.get("/api/system")
@auth
def system():
    import sys, platform
    bots = core.rows("SELECT id FROM bots")
    return jsonify(python=sys.version.split()[0], platform=platform.system(), uptime=int(time.time() - _STARTED),
                   bots=len(bots), running=sum(core.is_running(b["id"]) for b in bots), workers=core.WORKERS,
                   timeout=core.EXEC_TIMEOUT, db_mb=round(os.path.getsize(core.DB_PATH) / 1e6, 2),
                   public_url=core.base_url(),
                   scheduled=core.one("SELECT COUNT(*) c FROM scheduled")["c"])


# ---------- error alerts ----------
def _err_view():
    cfg = core.err_cfg()
    st = {}
    try:
        st = json.loads(core.get_setting("error_alert_status") or "{}")
    except Exception:
        pass
    return {"send_alert": cfg["send_alert"], "token_set": bool(cfg["alert_token"]), "token_hint": cfg["alert_token"][-4:],
            "alert_chat": cfg["alert_chat"], "owner_ids": ", ".join(str(x) for x in cfg["owner_ids"]),
            "show_owner": cfg["show_owner"], "show_user": cfg["show_user"], "user_msg": cfg["user_msg"], "status": st}


@app.get("/api/error_alerts")
@auth
def err_get():
    return jsonify(_err_view())


@app.put("/api/error_alerts")
@auth
def err_put():
    j, cfg = request.json, core.err_cfg()
    tok = (j.get("alert_token") or "").strip()
    if tok:
        try:
            me = core.tg(tok, "getMe")
        except Exception as e:
            return jsonify(error=f"Telegram rejected that alert bot token: {e}"), 400
        cfg["alert_token"] = tok
    if j.get("clear_token"):
        cfg["alert_token"] = ""
    chat = str(j.get("alert_chat", "")).strip()
    if chat and not re.fullmatch(r"-?\d{1,20}|@\w{4,}", chat):
        return jsonify(error="The alert chat ID is a number like 123456789 (or -100… for a group)."), 400
    ids = [x for x in re.split(r"[\s,;]+", str(j.get("owner_ids", ""))) if x]
    if any(not re.fullmatch(r"-?\d{1,20}", x) for x in ids):
        return jsonify(error="Owner IDs are numbers, separated by commas."), 400
    cfg.update(send_alert=bool(j.get("send_alert", True)), alert_chat=chat, owner_ids=[int(x) for x in ids],
               show_owner=bool(j.get("show_owner", True)), show_user=bool(j.get("show_user", True)),
               user_msg=str(j.get("user_msg", cfg["user_msg"]))[:500])
    core.set_err_cfg(cfg)
    return jsonify(_err_view())


@app.post("/api/error_alerts/test")
@auth
def err_test():
    cfg = core.err_cfg()
    if not cfg["alert_token"] or not str(cfg["alert_chat"]).strip():
        return jsonify(error="Save the alert bot token and the chat ID first."), 400
    try:
        core.send_error_alert(cfg, {"id": 0, "username": "test_bot"},
                              {"command": "/test", "line": 47, "message": "This is a test alert from Botbench.",
                               "short": "Syntax Error: expected 'except' or 'finally' block (<unknown>, line 47)"}, {"id": 1, "username": "tester"})
    except Exception as e:
        return jsonify(error=f"Couldn't send the alert: {e}"), 400
    return jsonify(ok=True)


# ---------- mini app ----------
MINI_DEFAULT = """<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>body{font-family:system-ui;padding:24px;background:var(--tg-theme-bg-color);color:var(--tg-theme-text-color)}</style>
</head><body>
<h2>Hello <span id="n">there</span></h2>
<script>const u=Telegram.WebApp.initDataUnsafe.user;if(u)n.textContent=u.first_name;Telegram.WebApp.ready();</script>
</body></html>
"""


@app.get("/api/bots/<int:bid>/miniapp")
@auth
def get_mini(bid):
    r = core.one("SELECT html FROM miniapps WHERE bot_id=?", (bid,))
    return jsonify(html=r["html"] if r else MINI_DEFAULT, saved=bool(r), url=f"https://{request.host}/m/{bid}")


@app.put("/api/bots/<int:bid>/miniapp")
@auth
def put_mini(bid):
    core.run("INSERT OR REPLACE INTO miniapps(bot_id, html) VALUES(?,?)", (bid, request.json.get("html", "")))
    return jsonify(ok=True)


@app.post("/api/bots/<int:bid>/miniapp/menu")
@auth
def mini_menu(bid):
    url = f"https://{request.host}/m/{bid}"
    try:
        core.tg(_bot(bid)["token"], "setChatMenuButton", menu_button={
            "type": "web_app", "text": (request.json.get("text") or "Open")[:16], "web_app": {"url": url}})
    except Exception as e:
        return jsonify(error=f"Telegram refused: {e}"), 400
    return jsonify(ok=True, url=url)


@app.route("/hook/<int:bid>", methods=["GET", "POST", "PUT", "OPTIONS"])
def webhook(bid):
    """Target of libs.Webhook.getUrlFor(): runs a command with the request as options (json, query, headers, ip).
    CORS is open so a web page (for example a verification page) can call it from the visitor's browser."""
    import hmac as _h
    cors = {"Access-Control-Allow-Origin": "*", "Access-Control-Allow-Methods": "GET, POST, PUT, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type, Authorization", "Access-Control-Max-Age": "600"}
    if request.method == "OPTIONS":
        return ("", 204, cors)
    c, u, s = request.args.get("c", ""), request.args.get("u", ""), request.args.get("s", "")
    want = _h.new(core.secret().encode(), f"{bid}|{c}|{u}".encode(), "sha256").hexdigest()[:24]
    bot = _bot(bid)
    if not bot or not _h.compare_digest(s, want) or not u.lstrip("-").isdigit():
        return jsonify(error="bad signature"), 403, cors
    body = request.get_json(silent=True)
    if body is None:
        body = dict(request.form) or dict(request.args)
    keep = ("user-agent", "origin", "referer", "x-forwarded-for", "content-type", "accept-language")
    res = core.fire_hook(bot, c, int(u), {"json": body, "params": dict(request.args), "query": dict(request.args),
                                    "method": request.method, "ip": (request.headers.get("X-Forwarded-For") or request.remote_addr or "").split(",")[0].strip(),
                                    "headers": {k: v for k, v in request.headers.items() if k.lower() in keep}})
    out = (res or {}).get("webhook_result")
    return jsonify(out if out is not None else {"ok": True}), 200, cors


@app.get("/m/<int:bid>")
def serve_mini(bid):
    r = core.one("SELECT html FROM miniapps WHERE bot_id=?", (bid,))
    return app.response_class(r["html"] if r else MINI_DEFAULT, mimetype="text/html")


def _auto_restore():
    """RESTORE_URL: when the database is empty (fresh deploy), load the backup from that link."""
    url = os.environ.get("RESTORE_URL", "").strip()
    if url and not core.one("SELECT 1 FROM bots LIMIT 1") and not core.one("SELECT 1 FROM trash_bots LIMIT 1"):
        try:
            print("RESTORE_URL:", core.restore_doc(core.load_backup(requests.get(url, timeout=90).content)))
        except Exception as e:
            print("RESTORE_URL failed:", e)


_auto_restore()
core.autostart()          # bots that were running (and new ones) come back on their own

if __name__ == "__main__":
    # Hosting platforms (Render, Railway...) set PORT and need 0.0.0.0
    default_host = "0.0.0.0" if os.environ.get("PORT") else "127.0.0.1"
    host = os.environ.get("HOST", default_host)
    port = int(os.environ.get("PORT", "5000"))
    try:                                   # production server when installed (pip install waitress)
        from waitress import serve
        print(f"Botbench on http://{host}:{port}")
        serve(app, host=host, port=port, threads=16)
    except ImportError:
        app.run(host=host, port=port, threaded=True)
