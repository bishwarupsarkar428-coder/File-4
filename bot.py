"""Telegram file share bot for @TG_HINDI_ANIME69.

Commands: /start /genlink /batch /custom_batch /special_link /universal_link
          /shortener /settings /broadcast /ban /unban /forcesub
"""
import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import re
import threading
import urllib.parse
import urllib.request
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:  # optional: load variables from a local .env file
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

from telegram import (
    BotCommand,
    BotCommandScopeChat,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
    MessageOriginChannel,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden, RetryAfter, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ChatJoinRequestHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from db import Database

MAIN_CHANNEL = "@TG_HINDI_ANIME69"
UPDATE_CHANNEL = os.getenv("UPDATE_CHANNEL", "https://t.me/TG_FILE_STORE69").strip()
CLONE_URL = os.getenv("CLONE_URL", "").strip()   # optional "Create my own clone" button
HOME_TEXT = (
    "<i>Hello {mention} ✨\n\n"
    "I am a permanent file store bot and users can access stored messages "
    "by using a shareable link given by me\n\n"
    "To know more click help button</i>"
)
INVALID_LINK = "This link is invalid or has expired."
BANNED_TEXT = "You are banned from using this bot."
ADMIN_ONLY = "Only admins can use this command."

RANGE_PREFIX = "r_"          # links that point at a message range in a channel
CODE_RE = re.compile(r"[A-Za-z0-9_-]{6,64}")
LINK_RE = re.compile(r"t\.me/(c/)?([A-Za-z0-9_]+)/(\d+)(?:/(\d+))?")
URL_RE = re.compile(r"https?://\S+")
MAX_RANGE = 1000             # max messages behind one channel link
MAX_FORCESUB = 5
AUTODEL_STEPS = [0, 5, 10, 30, 60]   # minutes, 0 = off
NO_CAPTION = {"video_note", "sticker", "text"}

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("file-share-bot")


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
def parse_admin_ids(raw: str) -> set:
    ids = set()
    for part in re.split(r"[,\s]+", raw or ""):
        if part.lstrip("-").isdigit():
            ids.add(int(part))
    return ids


def parse_chat_id(raw):
    raw = (raw or "").strip()
    return int(raw) if re.fullmatch(r"-?\d+", raw) else None


def env_flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in ("1", "true", "yes", "on")


ADMIN_IDS = parse_admin_ids(os.getenv("ADMIN_IDS", ""))
DATA_DIR = os.getenv("DATA_DIR", "data")
DB_PATH = os.path.join(DATA_DIR, "files.db")
DB_CHANNEL = parse_chat_id(os.getenv("DB_CHANNEL"))
PUBLIC_LINKS = env_flag("PUBLIC_LINKS")
SHORTENER_URL = re.sub(r"^https?://", "", os.getenv("SHORTENER_URL", "").strip()).strip("/")
SHORTENER_API = os.getenv("SHORTENER_API", "").strip()


def link_secret() -> bytes:
    return (os.getenv("LINK_SECRET") or os.getenv("BOT_TOKEN", "")).encode()


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def can_create(user_id: int) -> bool:
    return is_admin(user_id) or PUBLIC_LINKS


def shortener_ready() -> bool:
    return bool(SHORTENER_URL and SHORTENER_API)


# --------------------------------------------------------------------------- #
# Health-check server (needed by Render / Koyeb / Railway web services)
# --------------------------------------------------------------------------- #
class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"OK"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args):
        pass


def start_health_server():
    port = os.getenv("PORT")
    if not port:
        return
    try:
        server = ThreadingHTTPServer(("0.0.0.0", int(port)), _HealthHandler)
    except (ValueError, OSError) as exc:
        log.warning("Could not start health server: %s", exc)
        return
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log.info("Health server listening on port %s", port)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def db(context) -> Database:
    return context.application.bot_data["db"]


def make_link(context, payload: str) -> str:
    return f"https://t.me/{context.bot.username}?start={payload}"


def extract_media(message):
    """Return (kind, file_id) for a message, or None."""
    # Animations also carry a `document`, so check them first.
    if message.animation:
        return "animation", message.animation.file_id
    if message.document:
        return "document", message.document.file_id
    if message.video:
        return "video", message.video.file_id
    if message.audio:
        return "audio", message.audio.file_id
    if message.voice:
        return "voice", message.voice.file_id
    if message.video_note:
        return "video_note", message.video_note.file_id
    if message.sticker:
        return "sticker", message.sticker.file_id
    if message.photo:
        return "photo", message.photo[-1].file_id
    return None


def build_item(message):
    """Return (kind, data, caption) for a storable message, or None."""
    media = extract_media(message)
    if media:
        kind, file_id = media
        caption = message.caption_html if message.caption else None
        return kind, file_id, caption
    if message.text:
        return "text", message.text_html, None
    return None


def extract_code(text: str):
    match = re.search(r"start=([A-Za-z0-9_-]+)", text)
    code = match.group(1) if match else text.strip()
    return code if CODE_RE.fullmatch(code) else None


def retry_seconds(exc) -> float:
    value = exc.retry_after
    return value.total_seconds() if hasattr(value, "total_seconds") else float(value)


def user_gone(exc) -> bool:
    text = str(exc).lower()
    return "blocked" in text or "deactivated" in text


async def safe_call(fn, *args, **kwargs):
    """Call a bot method. Retries once on flood control, returns None on
    ordinary Telegram errors, and re-raises Forbidden for the caller."""
    for attempt in range(2):
        try:
            return await fn(*args, **kwargs)
        except RetryAfter as exc:
            if attempt == 1:
                return None
            await asyncio.sleep(retry_seconds(exc) + 1)
        except Forbidden:
            raise
        except TelegramError as exc:
            log.warning("%s failed: %s", getattr(fn, "__name__", "call"), exc)
            return None
    return None


_bg_tasks = set()


def spawn(coro):
    task = asyncio.create_task(coro)
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)


# ------------------------- channel-range links (signed) --------------------- #
def _sign(data: bytes) -> bytes:
    return hmac.new(link_secret(), data, hashlib.sha256).digest()[:6]


def encode_range(chat_id: int, first: int, last: int) -> str:
    if first > last:
        first, last = last, first
    data = f"{chat_id}:{first}:{last}".encode()
    token = base64.urlsafe_b64encode(data + _sign(data)).decode().rstrip("=")
    return RANGE_PREFIX + token


def decode_range(payload: str):
    try:
        token = payload[len(RANGE_PREFIX):]
        raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
        data, sig = raw[:-6], raw[-6:]
        if not hmac.compare_digest(sig, _sign(data)):
            return None
        chat_id, first, last = (int(x) for x in data.decode().split(":"))
    except Exception:
        return None
    if first > last or last - first + 1 > MAX_RANGE:
        return None
    return chat_id, first, last


# ------------------------------ URL shortener ------------------------------- #
def _shorten_sync(url: str):
    api = (
        f"https://{SHORTENER_URL}/api?api={urllib.parse.quote(SHORTENER_API, safe='')}"
        f"&url={urllib.parse.quote(url, safe='')}"
    )
    with urllib.request.urlopen(api, timeout=15) as resp:
        data = json.loads(resp.read().decode())
    for key in ("shortenedUrl", "shortened_url", "short_url", "shortUrl", "result_url"):
        if data.get(key):
            return data[key]
    return None


async def shorten(url: str):
    try:
        return await asyncio.to_thread(_shorten_sync, url)
    except Exception as exc:  # network, bad JSON, bad key...
        log.warning("Shortener failed: %s", exc)
        return None


async def send_link(message, context, payload: str,
                    intro: str = "Here is your link:", extra: str = ""):
    """Reply with the shareable link and a SHARE URL button."""
    link = make_link(context, payload)
    share = link
    text = f"<b>{escape(intro)}</b>\n\n{escape(link)}"
    if db(context).get_setting("short") == "1" and shortener_ready():
        short = await shorten(link)
        if short:
            text += f"\n\n<b>Short link:</b>\n{escape(short)}"
            share = short
    if extra:
        text += f"\n\n{escape(extra)}"
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton(
            "SHARE URL", url="https://t.me/share/url?url=" + urllib.parse.quote(share, safe="")
        )
    ]])
    await message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard)


# ------------------------------- sending files ------------------------------ #
async def send_item(bot, chat_id, kind, data, caption, protect):
    if kind == "text":
        return await safe_call(
            bot.send_message, chat_id, data,
            parse_mode=ParseMode.HTML, protect_content=protect,
        )
    kwargs = {kind: data, "protect_content": protect}
    if caption and kind not in NO_CAPTION:
        kwargs.update(caption=caption, parse_mode=ParseMode.HTML)
    return await safe_call(getattr(bot, f"send_{kind}"), chat_id, **kwargs)


async def deliver_range(bot, user_chat, src, first, last, protect):
    """Copy messages first..last from a channel to the user. Returns new ids."""
    sent, fails = [], 0
    for mid in range(first, last + 1):
        try:
            res = await safe_call(
                bot.copy_message, chat_id=user_chat, from_chat_id=src,
                message_id=mid, protect_content=protect,
            )
        except Forbidden as exc:
            if user_gone(exc):
                raise
            res = None
        if res is None:
            fails += 1
            if not sent and fails >= 10:  # channel not reachable, stop early
                break
        else:
            sent.append(res.message_id)
            fails = 0
        await asyncio.sleep(0.05)
    return sent


async def delete_later(bot, chat_id, message_ids, seconds):
    await asyncio.sleep(seconds)
    for mid in message_ids:
        try:
            await bot.delete_message(chat_id, mid)
        except TelegramError:
            pass
        await asyncio.sleep(0.03)


async def deliver_payload(update, context, payload):
    message = update.effective_message
    chat_id = update.effective_chat.id
    d = db(context)
    protect = d.get_setting("protect") == "1"
    minutes = int(d.get_setting("autodel") or 0)
    sent_ids = []

    if payload.startswith(RANGE_PREFIX):
        ref = decode_range(payload)
        if ref is None:
            await message.reply_text(INVALID_LINK)
            return
        src, first, last = ref
        sent_ids = await deliver_range(context.bot, chat_id, src, first, last, protect)
    else:
        items = d.get_items(payload) if CODE_RE.fullmatch(payload) else []
        if not items:
            await message.reply_text(INVALID_LINK)
            return
        for kind, data, caption in items:
            res = await send_item(context.bot, chat_id, kind, data, caption, protect)
            if res is not None:
                sent_ids.append(res.message_id)
            await asyncio.sleep(0.05)

    if not sent_ids:
        await message.reply_text("Sorry, these files are no longer available.")
        return
    if minutes:
        note = await message.reply_text(
            "⚠️ <u>Important</u>:\n\n"
            f"<i>All Messages will be deleted after <b>{minutes} minutes</b>. "
            "Please save or forward these messages to your "
            "<b>personal saved messages</b> to avoid losing them!</i>",
            parse_mode=ParseMode.HTML,
        )
        spawn(delete_later(context.bot, chat_id, sent_ids + [note.message_id], minutes * 60))


# --------------------------------- force sub -------------------------------- #
def is_member(member) -> bool:
    if member.status in ("creator", "administrator", "member"):
        return True
    return member.status == "restricted" and bool(getattr(member, "is_member", False))


async def missing_channels(context, user_id):
    """Force-sub channels the user has not joined (fails open on errors)."""
    missing = []
    d = db(context)
    for chat_id, title, link, mode in d.list_forcesub():
        # request mode: a pending join request is enough
        if mode == "request" and d.has_join_request(chat_id, user_id):
            continue
        try:
            member = await context.bot.get_chat_member(chat_id, user_id)
        except TelegramError as exc:
            log.warning("Force-sub check failed for %s: %s", chat_id, exc)
            continue
        if not is_member(member):
            missing.append((chat_id, title, link, mode))
    return missing


async def send_join_prompt(message, context, missing, payload):
    rows = [
        [InlineKeyboardButton(
            f"{'Request to join' if mode == 'request' else 'Join'} {title}", url=link)]
        for _, title, link, mode in missing
        if link
    ]
    rows.append(
        [InlineKeyboardButton("Try Again", url=make_link(context, payload))]
    )
    text = ("To use this bot, you must join our channel(s) first.\n"
            "After joining, tap Try Again.")
    if any(mode == "request" for *_, mode in missing):
        text += "\n\nFor 'Request to join' channels, just send the join request - no need to wait for approval."
    await message.reply_text(text, reply_markup=InlineKeyboardMarkup(rows))


# ------------------------------- access checks ------------------------------ #
def is_banned(context, user_id) -> bool:
    return db(context).is_banned(user_id)


async def require_admin(update, context) -> bool:
    if is_admin(update.effective_user.id):
        return True
    await update.effective_message.reply_text(ADMIN_ONLY)
    return False


async def require_creator(update, context) -> bool:
    user_id = update.effective_user.id
    if is_banned(context, user_id):
        await update.effective_message.reply_text(BANNED_TEXT)
        return False
    if can_create(user_id):
        return True
    await update.effective_message.reply_text(ADMIN_ONLY)
    return False


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    user = update.effective_user
    if update.effective_chat.type == "private":
        db(context).add_user(user.id)
    if is_banned(context, user.id):
        await message.reply_text(BANNED_TEXT)
        return

    if not context.args:
        await send_home(message, user)  # plain /start only
        return

    payload = context.args[0]
    try:
        if not is_admin(user.id):
            missing = await missing_channels(context, user.id)
            if missing:
                await send_join_prompt(message, context, missing, payload)
                return
        await deliver_payload(update, context, payload)
    except Forbidden:
        log.info("User %s blocked the bot", user.id)


def help_text(user_id: int) -> str:
    text = "📖 Help\n\nOpen a share link to receive the files.\n/start – restart the bot"
    if can_create(user_id):
        text += (
            "\n\n/genlink – store a single message or file\n"
            "/batch – store many messages from a channel\n"
            "/custom_batch – store many random messages\n"
            "/shortener – shorten a link\n"
            "/done – finish a batch, /cancel – cancel"
        )
    if is_admin(user_id):
        text += (
            "\n\nAdmin:\n"
            "/special_link – editable link\n"
            "/universal_link – link that works in all your clones\n"
            "/settings – bot settings\n"
            "/forcesub – force-subscribe channels\n"
            "/broadcast – reply to a message to send it to all users\n"
            "/ban <id>, /unban <id>"
        )
    return text


def about_text(context) -> str:
    return (
        "🤖 About this bot\n\n"
        f"Name: {context.bot.first_name}\n"
        f"Username: @{context.bot.username}\n"
        "Type: Permanent file store bot\n"
        f"Owner channel: {MAIN_CHANNEL}\n"
        f"Updates: {UPDATE_CHANNEL}\n"
        "Language: Python 3\n"
        "Library: python-telegram-bot\n\n"
        "Send me a shareable link to get the stored files."
    )


def home_text(user) -> str:
    mention = f'<a href="tg://user?id={user.id}">{escape(user.first_name or "there")}</a>'
    return HOME_TEXT.format(mention=mention)


def home_keyboard() -> InlineKeyboardMarkup:
    rows = [[
        InlineKeyboardButton("HELP", callback_data="menu:help"),
        InlineKeyboardButton("ABOUT", callback_data="menu:about"),
    ]]
    if CLONE_URL:
        rows.append([InlineKeyboardButton("CREATE MY OWN CLONE", url=CLONE_URL)])
    rows.append([InlineKeyboardButton("📟 UPDATE CHANNEL", url=UPDATE_CHANNEL)])
    return InlineKeyboardMarkup(rows)


async def send_home(message, user):
    await message.reply_text(
        home_text(user), parse_mode=ParseMode.HTML, reply_markup=home_keyboard()
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(help_text(update.effective_user.id))


async def menu_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    action = query.data.split(":", 1)[1]
    back = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ BACK", callback_data="menu:home")]])
    try:
        if action == "help":
            await query.edit_message_text(help_text(query.from_user.id), reply_markup=back)
        elif action == "about":
            await query.edit_message_text(
                about_text(context), reply_markup=back,
                link_preview_options=LinkPreviewOptions(is_disabled=True),
            )
        else:
            await query.edit_message_text(
                home_text(query.from_user), parse_mode=ParseMode.HTML,
                reply_markup=home_keyboard(),
            )
    except BadRequest:
        pass  # "message is not modified" etc.
    await query.answer()


async def id_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        f"Your Telegram ID: {update.effective_user.id}"
    )


async def genlink_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_creator(update, context):
        return
    message = update.effective_message
    replied = message.reply_to_message
    if replied:
        item = build_item(replied)
        if item:
            code = db(context).create_link([item], created_by=update.effective_user.id)
            await send_link(message, context, code)
            return
    context.user_data["flow"] = {"type": "genlink"}
    await message.reply_text("Send A Message For To Get Your Shareable Link")


async def batch_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_creator(update, context):
        return
    context.user_data["flow"] = {"type": "batch", "first": None}
    await update.effective_message.reply_text(
        "Forward the FIRST message from your channel (or send its message link).\n"
        "The bot must be an admin of that channel. Send /cancel to abort."
    )


async def custom_batch_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_creator(update, context):
        return
    context.user_data["flow"] = {"type": "collect", "mode": "custom",
                                 "items": [], "refs": [], "edit_code": None}
    await update.effective_message.reply_text(
        "Send the messages/files you want to store one by one.\n"
        "Send /done when finished, or /cancel to abort."
    )


async def special_link_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    message = update.effective_message
    edit_code = None
    if context.args:
        edit_code = extract_code(" ".join(context.args))
        if not edit_code or not db(context).is_editable(edit_code):
            await message.reply_text(
                "That link doesn't exist or is not editable. "
                "Only links made with /special_link can be edited."
            )
            return
    context.user_data["flow"] = {"type": "collect", "mode": "special",
                                 "items": [], "refs": [], "edit_code": edit_code}
    if edit_code:
        await message.reply_text(
            "Editing this link. Send the NEW messages/files (they replace the old "
            "ones), then send /done. /cancel to abort."
        )
    else:
        await message.reply_text(
            "Send the messages/files for the editable link, then /done. "
            "/cancel to abort."
        )


async def universal_link_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    message = update.effective_message
    if DB_CHANNEL is None:
        await message.reply_text(
            "DB_CHANNEL is not set. Create a private channel, add this bot as "
            "admin, and set DB_CHANNEL to its ID (e.g. -1001234567890)."
        )
        return
    context.user_data["flow"] = {"type": "collect", "mode": "universal",
                                 "items": [], "refs": [], "edit_code": None}
    await message.reply_text(
        "Send the messages you want in the universal link, then /done. "
        "/cancel to abort."
    )


async def copy_to_db_channel(context, refs):
    """Copy messages into DB_CHANNEL and return a signed range payload."""
    if len(refs) > MAX_RANGE:
        return None
    async with context.application.bot_data["lock"]:
        ids = []
        for chat_id, mid in refs:
            res = await safe_call(
                context.bot.copy_message, chat_id=DB_CHANNEL,
                from_chat_id=chat_id, message_id=mid,
            )
            if res is None:
                return None
            ids.append(res.message_id)
            await asyncio.sleep(0.1)
    if max(ids) - min(ids) + 1 > MAX_RANGE:
        return None
    return encode_range(DB_CHANNEL, min(ids), max(ids))


async def done_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    flow = context.user_data.get("flow")
    if not flow or flow["type"] != "collect":
        await message.reply_text(
            "Nothing to finish. Start with /custom_batch, /special_link "
            "or /universal_link."
        )
        return
    mode = flow["mode"]
    count = len(flow["refs"]) if mode == "universal" else len(flow["items"])
    if count == 0:
        await message.reply_text("You haven't added anything yet.")
        return
    context.user_data.pop("flow", None)
    d = db(context)
    user_id = update.effective_user.id

    if mode == "universal":
        try:
            payload = await copy_to_db_channel(context, flow["refs"])
        except TelegramError as exc:
            log.warning("Universal link failed: %s", exc)
            payload = None
        if payload is None:
            await message.reply_text(
                "Could not copy the messages to DB_CHANNEL. Make sure the bot is "
                "an admin there and you added at most 1000 messages."
            )
            return
        await send_link(message, context, payload)
        return

    if mode == "special" and flow["edit_code"]:
        d.replace_link(flow["edit_code"], flow["items"])
        await send_link(message, context, flow["edit_code"], intro="Link updated (same link):")
        return

    code = d.create_link(flow["items"], editable=(mode == "special"), created_by=user_id)
    extra = f"To edit it later: /special_link {code}" if mode == "special" else ""
    await send_link(message, context, code, extra=extra)


async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("flow", None)
    await update.effective_message.reply_text("Cancelled.")


async def do_shorten(message, url: str):
    if not URL_RE.fullmatch(url):
        await message.reply_text("Please send a valid link starting with http:// or https://")
        return
    short = await shorten(url)
    await message.reply_text(short or "Could not shorten that link. Try again later.")


async def shortener_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_creator(update, context):
        return
    message = update.effective_message
    if not shortener_ready():
        await message.reply_text(
            "The link shortener is not set up. The owner must set SHORTENER_URL "
            "and SHORTENER_API."
        )
        return
    if context.args:
        await do_shorten(message, context.args[0])
        return
    context.user_data["flow"] = {"type": "shortener"}
    await message.reply_text("Send the link you want to shorten.")


# ---------------------------------- settings -------------------------------- #
def settings_view(context):
    d = db(context)
    protect = d.get_setting("protect") == "1"
    minutes = int(d.get_setting("autodel") or 0)
    short = d.get_setting("short") == "1"
    approve = d.get_setting("autoapprove") == "1"
    autodel = f"{minutes} min" if minutes else "OFF"
    text = (
        "⚙️ Settings\n\n"
        f"Protect content (no forwarding/saving): {'ON' if protect else 'OFF'}\n"
        f"Auto-delete delivered files: {autodel}\n"
        f"Shorten generated links: {'ON' if short else 'OFF'}"
        + ("" if shortener_ready() else " (shortener not configured)")
        + f"\nAuto-approve join requests (request mode): {'ON' if approve else 'OFF'}"
    )
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(f"Protect content: {'ON' if protect else 'OFF'}",
                                  callback_data="set:protect")],
            [InlineKeyboardButton(f"Auto-delete: {autodel}", callback_data="set:autodel")],
            [InlineKeyboardButton(f"Shorten links: {'ON' if short else 'OFF'}",
                                  callback_data="set:short")],
            [InlineKeyboardButton(f"Auto-approve requests: {'ON' if approve else 'OFF'}",
                                  callback_data="set:approve")],
            [InlineKeyboardButton("Close", callback_data="set:close")],
        ]
    )
    return text, keyboard


async def settings_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    text, keyboard = settings_view(context)
    await update.effective_message.reply_text(text, reply_markup=keyboard)


async def settings_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await query.answer("Only admins can change settings.", show_alert=True)
        return
    action = query.data.split(":", 1)[1]
    d = db(context)

    if action == "close":
        await query.answer()
        try:
            await query.message.delete()
        except TelegramError:
            pass
        return
    if action == "protect":
        d.set_setting("protect", "0" if d.get_setting("protect") == "1" else "1")
    elif action == "autodel":
        current = int(d.get_setting("autodel") or 0)
        if current in AUTODEL_STEPS:
            nxt = AUTODEL_STEPS[(AUTODEL_STEPS.index(current) + 1) % len(AUTODEL_STEPS)]
        else:
            nxt = 0
        d.set_setting("autodel", nxt)
    elif action == "approve":
        d.set_setting("autoapprove", "0" if d.get_setting("autoapprove") == "1" else "1")
    elif action == "short":
        if not shortener_ready():
            await query.answer("Set SHORTENER_URL and SHORTENER_API first.", show_alert=True)
            return
        d.set_setting("short", "0" if d.get_setting("short") == "1" else "1")

    await query.answer()
    text, keyboard = settings_view(context)
    try:
        await query.edit_message_text(text, reply_markup=keyboard)
    except BadRequest:
        pass


# --------------------------- broadcast / ban / unban ------------------------ #
async def broadcast_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    message = update.effective_message
    replied = message.reply_to_message
    if not replied:
        await message.reply_text("Reply to the message you want to broadcast with /broadcast.")
        return
    d = db(context)
    users = d.all_user_ids()
    await message.reply_text(f"Broadcasting to {len(users)} users...")
    sent = blocked = failed = 0
    for uid in users:
        try:
            res = await safe_call(
                context.bot.copy_message, chat_id=uid,
                from_chat_id=message.chat_id, message_id=replied.message_id,
            )
            if res is None:
                failed += 1
            else:
                sent += 1
        except Forbidden:
            blocked += 1
            d.remove_user(uid)
        await asyncio.sleep(0.05)
    await message.reply_text(
        f"Broadcast finished.\nSent: {sent}\nBlocked (removed): {blocked}\nFailed: {failed}"
    )


def parse_user_id(context):
    if context.args and re.fullmatch(r"\d+", context.args[0]):
        return int(context.args[0])
    return None


async def ban_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    message = update.effective_message
    uid = parse_user_id(context)
    if uid is None:
        await message.reply_text("Usage: /ban <user_id>")
        return
    if is_admin(uid):
        await message.reply_text("You can't ban an admin.")
        return
    db(context).set_banned(uid, True)
    await message.reply_text(f"User {uid} banned.")


async def unban_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    message = update.effective_message
    uid = parse_user_id(context)
    if uid is None:
        await message.reply_text("Usage: /unban <user_id>")
        return
    db(context).set_banned(uid, False)
    await message.reply_text(f"User {uid} unbanned.")


# --------------------------------- /forcesub -------------------------------- #
FORCESUB_HELP = (
    "Force subscribe (admins only)\n\n"
    "/forcesub add <@channel or id> – users must join it\n"
    "/forcesub add <@channel or id> request – REQUEST MODE: users only need to "
    "send a join request (for private channels)\n"
    "/forcesub remove <@channel, id or number>\n"
    "/forcesub list – show channels\n"
    "/forcesub clear – turn force subscribe off\n\n"
    "The bot must be an admin in the channel (with the 'Invite users' "
    "permission for private channels)."
)


def forcesub_list_text(context):
    rows = db(context).list_forcesub()
    if not rows:
        return "Force subscribe is OFF (no channels)."
    lines = [
        f"{i}. {title} ({chat_id})" + (" [request mode]" if mode == "request" else "")
        for i, (chat_id, title, _link, mode) in enumerate(rows, 1)
    ]
    return "Force subscribe is ON for:\n" + "\n".join(lines)


def find_forcesub(context, arg: str):
    rows = db(context).list_forcesub()
    arg_l = arg.strip().lstrip("@").lower()
    for index, (chat_id, _title, link, _mode) in enumerate(rows, 1):
        username = link.rsplit("/", 1)[-1].lower() if "/+" not in link else ""
        if arg == str(chat_id) or (username and arg_l == username):
            return chat_id
        if arg.isdigit() and int(arg) == index:
            return chat_id
    return None


async def forcesub_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    message = update.effective_message
    args = context.args
    if not args:
        await message.reply_text(forcesub_list_text(context) + "\n\n" + FORCESUB_HELP)
        return

    action = args[0].lower()
    d = db(context)
    if action == "list":
        await message.reply_text(forcesub_list_text(context))
    elif action == "clear":
        d.clear_forcesub()
        await message.reply_text("Force subscribe turned OFF.")
    elif action == "remove" and len(args) > 1:
        chat_id = find_forcesub(context, args[1])
        if chat_id is None:
            await message.reply_text("That channel is not in the force-subscribe list.")
            return
        d.remove_forcesub(chat_id)
        await message.reply_text("Removed.\n\n" + forcesub_list_text(context))
    elif action == "add" and len(args) > 1:
        if len(d.list_forcesub()) >= MAX_FORCESUB:
            await message.reply_text(f"You can add at most {MAX_FORCESUB} channels.")
            return
        target = parse_chat_id(args[1])
        if target is None:
            target = args[1] if args[1].startswith("@") else "@" + args[1]
        mode = "request" if len(args) > 2 and args[2].lower() == "request" else "normal"
        try:
            chat = await context.bot.get_chat(target)
            me = await context.bot.get_chat_member(chat.id, context.bot.id)
            if me.status not in ("administrator", "creator"):
                raise ValueError("bot is not admin")
            if mode == "request":
                # invite link that asks people to send a join request
                invite = await context.bot.create_chat_invite_link(
                    chat.id, name="Force subscribe", creates_join_request=True
                )
                link = invite.invite_link
            elif chat.username:
                link = f"https://t.me/{chat.username}"
            else:
                link = await context.bot.export_chat_invite_link(chat.id)
        except (TelegramError, ValueError) as exc:
            log.warning("forcesub add failed: %s", exc)
            await message.reply_text(
                "Could not add that channel. Make sure the ID/username is correct "
                "and that the bot is an ADMIN there (with 'Invite users' permission "
                "for private channels)."
            )
            return
        d.add_forcesub(chat.id, chat.title or str(chat.id), link, mode)
        await message.reply_text("Added.\n\n" + forcesub_list_text(context))
    else:
        await message.reply_text(FORCESUB_HELP)


# --------------------------------------------------------------------------- #
# Messages (files / text) – used by flows and quick link creation
# --------------------------------------------------------------------------- #
async def resolve_ref(context, message):
    """(chat_id, message_id) from a forwarded channel post or a t.me link."""
    origin = getattr(message, "forward_origin", None)
    if isinstance(origin, MessageOriginChannel):
        return origin.chat.id, origin.message_id
    text = message.text or message.caption or ""
    match = LINK_RE.search(text)
    if not match:
        return None
    private, name, first_num, second_num = match.groups()
    msg_id = int(second_num or first_num)
    if private:
        return (int("-100" + name), msg_id) if name.isdigit() else None
    try:
        chat = await context.bot.get_chat("@" + name)
    except TelegramError:
        return None
    return chat.id, msg_id


async def bot_can_access(context, chat_id) -> bool:
    try:
        member = await context.bot.get_chat_member(chat_id, context.bot.id)
    except TelegramError:
        return False
    return member.status in ("administrator", "creator", "member")


async def handle_flow(update, context, flow):
    message = update.effective_message
    user_id = update.effective_user.id
    kind = flow["type"]

    if kind == "genlink":
        item = build_item(message)
        if item is None:
            await message.reply_text("I can't store that. Send a file or a text message.")
            return
        context.user_data.pop("flow", None)
        code = db(context).create_link([item], created_by=user_id)
        await send_link(message, context, code)

    elif kind == "collect":
        item = build_item(message)
        if flow["mode"] == "universal":
            flow["refs"].append((message.chat_id, message.message_id))
            count = len(flow["refs"])
        else:
            if item is None:
                await message.reply_text("Skipped: I can't store that type of message.")
                return
            flow["items"].append(item)
            count = len(flow["items"])
        await message.reply_text(f"Added ({count}). Send more or /done.")

    elif kind == "batch":
        ref = await resolve_ref(context, message)
        if ref is None:
            await message.reply_text(
                "I couldn't read that. Forward a post from the channel or send its "
                "message link (t.me/...)."
            )
            return
        chat_id, msg_id = ref
        if flow.get("first") is None:
            if not await bot_can_access(context, chat_id):
                await message.reply_text(
                    "I can't access that channel. Add the bot as an admin there first."
                )
                return
            flow["first"] = (chat_id, msg_id)
            await message.reply_text("Got it. Now forward the LAST message (or send its link).")
            return
        first_chat, first_id = flow["first"]
        if chat_id != first_chat:
            await message.reply_text("Both messages must be from the same channel.")
            return
        if abs(msg_id - first_id) + 1 > MAX_RANGE:
            await message.reply_text(f"Too many messages. The limit is {MAX_RANGE}.")
            return
        context.user_data.pop("flow", None)
        payload = encode_range(chat_id, first_id, msg_id)
        await send_link(message, context, payload)

    elif kind == "shortener":
        match = URL_RE.search(message.text or "")
        context.user_data.pop("flow", None)
        if not match:
            await message.reply_text("That doesn't look like a link. Send /shortener to try again.")
            return
        await do_shorten(message, match.group(0))


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    user_id = update.effective_user.id
    if is_banned(context, user_id):
        await message.reply_text(BANNED_TEXT)
        return

    flow = context.user_data.get("flow")
    if flow:
        await handle_flow(update, context, flow)
        return

    # Quick mode: send a file, get a link
    if can_create(user_id) and extract_media(message):
        item = build_item(message)
        code = db(context).create_link([item], created_by=user_id)
        await send_link(message, context, code)
        return

    await send_home(message, update.effective_user)


async def on_join_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Remember join requests for force-sub channels (request mode)."""
    request = update.chat_join_request
    chat_id, user_id = request.chat.id, request.from_user.id
    d = db(context)
    if chat_id not in {row[0] for row in d.list_forcesub()}:
        return
    d.add_join_request(chat_id, user_id)
    if d.get_setting("autoapprove") == "1":
        try:
            await context.bot.approve_chat_join_request(chat_id, user_id)
        except TelegramError as exc:
            log.warning("Could not approve join request: %s", exc)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.error("Unhandled error", exc_info=context.error)


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
USER_MENU = [
    BotCommand("start", "Check i am alive"),
]
CREATOR_MENU = [
    BotCommand("genlink", "To store a single message or file"),
    BotCommand("batch", "To store multiple messages from a channel"),
    BotCommand("custom_batch", "To store multiple random messages"),
    BotCommand("shortener", "To shorten any shareable links"),
]
ADMIN_MENU = [
    BotCommand("special_link", "Store multiple messages and get an editable link"),
    BotCommand("universal_link", "Multiple messages usable from any of your clones"),
    BotCommand("settings", "Customize your settings as you need"),
    BotCommand("forcesub", "Manage force subscribe channels"),
    BotCommand("broadcast", "Broadcast a message to users"),
    BotCommand("ban", "Ban a user"),
    BotCommand("unban", "Unban a user"),
]


async def post_init(application):
    """Show the command menu (best effort - never blocks start-up)."""
    everyone = USER_MENU + (CREATOR_MENU if PUBLIC_LINKS else [])
    try:
        await application.bot.set_my_commands(everyone)
    except TelegramError as exc:
        log.warning("Could not set default commands: %s", exc)
    for admin_id in ADMIN_IDS:
        try:
            await application.bot.set_my_commands(
                USER_MENU + CREATOR_MENU + ADMIN_MENU,
                scope=BotCommandScopeChat(admin_id),
            )
        except TelegramError as exc:
            log.info("Could not set menu for admin %s: %s", admin_id, exc)


def main():
    token = os.getenv("BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit(
            "BOT_TOKEN is not set. Get one from @BotFather and set it as an "
            "environment variable (see README.md)."
        )
    if not ADMIN_IDS:
        log.warning(
            "ADMIN_IDS is empty - nobody can create links yet. "
            "Send /id to the bot and set ADMIN_IDS."
        )

    application = Application.builder().token(token).post_init(post_init).build()
    application.bot_data["db"] = Database(DB_PATH)
    application.bot_data["lock"] = asyncio.Lock()

    media_filter = (
        filters.ANIMATION
        | filters.Document.ALL
        | filters.VIDEO
        | filters.AUDIO
        | filters.VOICE
        | filters.VIDEO_NOTE
        | filters.Sticker.ALL
        | filters.PHOTO
    )

    # block=False: long deliveries/broadcasts must not freeze other users
    application.add_handler(CommandHandler("start", start, block=False))
    application.add_handler(CommandHandler("broadcast", broadcast_cmd, block=False))
    for name, callback in (
        ("help", help_cmd),
        ("id", id_cmd),
        ("genlink", genlink_cmd),
        ("batch", batch_cmd),
        ("custom_batch", custom_batch_cmd),
        ("special_link", special_link_cmd),
        ("universal_link", universal_link_cmd),
        ("done", done_cmd),
        ("cancel", cancel_cmd),
        ("shortener", shortener_cmd),
        ("settings", settings_cmd),
        ("ban", ban_cmd),
        ("unban", unban_cmd),
        ("forcesub", forcesub_cmd),
    ):
        application.add_handler(CommandHandler(name, callback))
    application.add_handler(CallbackQueryHandler(settings_cb, pattern=r"^set:"))
    application.add_handler(CallbackQueryHandler(menu_cb, pattern=r"^menu:"))
    application.add_handler(
        MessageHandler(
            filters.ChatType.PRIVATE & ~filters.COMMAND & (media_filter | filters.TEXT),
            on_message,
        )
    )
    application.add_handler(ChatJoinRequestHandler(on_join_request))
    application.add_error_handler(on_error)

    start_health_server()
    log.info("Bot is starting...")
    application.run_polling(
        drop_pending_updates=True, allowed_updates=Update.ALL_TYPES
    )


if __name__ == "__main__":
    main()
