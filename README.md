# Botbench

A web panel for building Telegram bots where every command is plain Python
(like Telebot Creator, but with Python instead of TPY).

## Run it

    pip install -r requirements.txt
    PANEL_PASSWORD=choose-a-password python app.py

Open http://127.0.0.1:5000, sign in, click **Add a bot**, paste a token from
@BotFather, edit the commands, and press **Start bot**.

Settings (environment variables): `PANEL_PASSWORD` (default `admin` - change it),
`HOST`, `PORT`, `EXEC_TIMEOUT` (seconds per command, default 10), `PANEL_DB`.

## How it works

- `app.py` - panel server and JSON API
- `core.py` - SQLite storage, Telegram calls, long-polling runner per bot
- `sandbox.py` - runs one command's code in a separate process with a timeout
- `templates/` - the panel UI

Bots use long polling, so no public URL or HTTPS is needed. Running bots
restart automatically when `app.py` restarts.

## Security

Command code runs as the user that started `app.py` and can use the network
and files. Share panel access only with people you trust, and don't expose the
panel to the internet without HTTPS and a strong password.

## Deploying on Render

1. Push the whole folder to GitHub, **including the `templates/` folder**
   (`app.py`, `core.py`, `sandbox.py`, `requirements.txt`, `templates/login.html`,
   `templates/panel.html`).
2. New Web Service: build command `pip install -r requirements.txt`,
   start command `python app.py`.
3. Add environment variables: `PANEL_PASSWORD` (required - pick a strong one) and
   `SECRET_KEY` is not needed.
4. Bots only answer while the service is awake. Free instances sleep after
   inactivity, so use a paid instance for an always-on bot.
5. Without a persistent disk the SQLite file is wiped on every deploy/restart.
   Add a Render Disk (e.g. mounted at `/data`) and set `PANEL_DB=/data/panel.db`.

## How commands match

Command names are matched the way users type them, with or without a slash and ignoring
upper/lower case: a command named `hii` (or `/hii`) answers "hii" and "/hii"; a command named
`Link Wallet` or `🚀 Withdraw` answers that exact button text. Extra words become `args`.
`*` catches everything else and `cb:data` answers an inline button.

## Speed and settings

Commands run in a small pool of warm worker processes (`WORKERS`, default 3), so a reply takes
milliseconds. A command that runs past `EXEC_TIMEOUT` seconds (default 10) is stopped and its
worker replaced. Bots that were running start again automatically when the app starts, and a
newly added bot starts straight away. Keep a single web process (Render's default).
Environment variables you add in Manage > Environment variables are available as globals in
every command.
