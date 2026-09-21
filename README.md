# File Share Bot – @TG_HINDI_ANIME69

When anyone starts the bot it replies:

> Hello, I am a file share bot of @TG_HINDI_ANIME69

Admins store messages/files and get shareable links. Anyone opening a link
receives the content.

## Commands

| Command | Who | What it does |
|---|---|---|
| `/start` | everyone | Check the bot is alive; opens share links |
| `/genlink` | admins* | Store a single message or file (send it, or reply to it with `/genlink`) |
| `/batch` | admins* | Store many messages **from a channel**: forward the first and last message (or send their links) |
| `/custom_batch` | admins* | Store many random messages: send them one by one, then `/done` |
| `/special_link` | admins | Like `/custom_batch` but the link is **editable**: `/special_link <link or code>` replaces its content |
| `/universal_link` | admins | Copies messages to your `DB_CHANNEL`; the link works from **any of your clones** |
| `/shortener` | admins* | Shorten a link: `/shortener <link>` |
| `/settings` | admins | Protect content (no forward/save), auto-delete timer, shorten generated links |
| `/broadcast` | admins | Reply to a message with `/broadcast` to send it to all users |
| `/ban` `/unban` | admins | `/ban <user_id>`, `/unban <user_id>` |
| `/forcesub` | admins | Force users to join channels before they get files (see below) |

`/done` finishes a batch, `/cancel` aborts, `/id` shows your Telegram ID.
\* Set `PUBLIC_LINKS=true` to let every user use these.
Admins can also just send a file to the bot to get a link instantly.

### Force subscribe (admins only)
```
/forcesub add @yourchannel      (or the channel ID, e.g. -1001234567890)
/forcesub list
/forcesub remove @yourchannel   (or its ID / list number)
/forcesub clear                 (turn it off)
```
The bot must be an **admin** in each channel (with "Invite users" permission for
private channels). Up to 5 channels. Users who haven't joined see join buttons
and a **Try Again** button. Admins skip the check. If Telegram can't verify a
channel (e.g. the bot was removed), users are let through rather than blocked.

## Setup

1. Create a bot with [@BotFather](https://t.me/BotFather) and copy the token.
2. Send `/id` to your bot to get your Telegram ID.
3. Set environment variables:

| Variable | Required | Description |
|---|---|---|
| `BOT_TOKEN` | yes | Token from @BotFather |
| `ADMIN_IDS` | yes | Comma-separated admin user IDs |
| `DB_CHANNEL` | for `/universal_link` | Private channel ID, bot must be admin there |
| `PUBLIC_LINKS` | no | `true` = everyone can create links |
| `LINK_SECRET` | no | Signing secret for channel links (same on all clones) |
| `SHORTENER_URL`, `SHORTENER_API` | no | Enable `/shortener` (e.g. `gplinks.in` + API key) |
| `DATA_DIR` | no | Database folder (default `data`) |

## Run locally
```bash
python -m venv venv && source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env      # then edit .env
python bot.py
```

## Deploy
Push to GitHub, then use **Railway / Render / Koyeb** (Dockerfile or
`render.yaml` are detected), **Heroku** (`Procfile`), or a VPS:
`docker build -t file-share-bot . && docker run -d --restart=always --env-file .env -v $(pwd)/data:/app/data file-share-bot`

Long polling is used, so no webhook is needed. Run **only one** instance per
bot token.

## Where links are stored (important)
- `/batch` and `/universal_link` links point at messages in your channel and are
  **signed**, so they keep working even if the bot's disk is wiped.
- `/genlink`, `/custom_batch` and `/special_link` links, plus users, bans,
  settings and force-sub channels, live in a SQLite file (`DATA_DIR/files.db`).
  On hosts with an ephemeral disk this is **erased on every redeploy**. Attach a
  persistent volume (e.g. mount `/data` and set `DATA_DIR=/data`) if you need
  them to last.
- Channels with "Restrict saving content" turned on can't be copied from, so
  `/batch` and `/universal_link` won't work with them.
- The bot needs to be an admin in the source channel for `/batch`.
