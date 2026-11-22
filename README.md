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
