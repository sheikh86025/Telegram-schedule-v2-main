import asyncio
import json
import logging
import re
import time
from html import escape
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BotCommand, BotCommandScopeAllChatAdministrators, BotCommandScopeChat, BotCommandScopeDefault,
    BufferedInputFile, InputMediaDocument, CallbackQuery, ChatMemberUpdated, InlineKeyboardButton, InlineKeyboardMarkup,
    Message, MessageEntity,
)
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramMigrateToChat,
    TelegramRetryAfter,
)
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from .config import (
    BOT_TOKEN, ADMIN_IDS, POLL_SECONDS, BATCH_SIZE, DEFAULT_INTERVAL_MINUTES, POST_DELAY_SECONDS,
    TIMEZONE, QUIET_START_HOUR, QUIET_END_HOUR, LOW_POST_THRESHOLD, DAILY_REPORT_HOUR,
    BACKUP_DELAY_SECONDS, AUTO_SNAPSHOT_HOURS, MAX_RESTORE_RANGE, DESTINATION_IDS, BACKUP_CHANNEL_ID,
    FAIL_ALERT_AFTER,
)
from . import storage

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing. Set it in Railway Variables.")

TZ = ZoneInfo(TIMEZONE)
bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

DEST_TYPES = {"channel", "group", "supergroup"}
GROUP_TYPES = {"group", "supergroup"}
CAPTION_TYPES = {"photo", "video", "animation", "audio", "document", "voice"}


# ------------------------------------------------------------------ helpers

async def db(fn, *args):
    """Run a (blocking) storage function in a thread so the bot never freezes."""
    return await asyncio.to_thread(fn, *args)


def utc_in(**delta) -> str:
    return (datetime.now(timezone.utc) + timedelta(**delta)).isoformat()


def in_quiet_hours(now_local: datetime) -> bool:
    start, end, hour = QUIET_START_HOUR, QUIET_END_HOUR, now_local.hour
    if start == end:
        return False
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end  # window crossing midnight, e.g. 22 -> 6


def quiet_label() -> str:
    if QUIET_START_HOUR == QUIET_END_HOUR:
        return "off"
    return f"{QUIET_START_HOUR:02d}:00-{QUIET_END_HOUR:02d}:00 {TIMEZONE}"


def fmt_local(iso) -> str:
    if not iso:
        return "now"
    try:
        return datetime.fromisoformat(iso).astimezone(TZ).strftime("%d %b, %H:%M")
    except ValueError:
        return "?"


def status_of(member) -> str:
    return str(getattr(member.status, "value", member.status))


def icon(kind: str) -> str:
    return "📢" if kind == "channel" else "👥"


def bot_can_post(chat_type: str, member) -> bool:
    """Can the bot post in this chat, judging by its own membership record?"""
    status = status_of(member)
    if chat_type == "channel":
        return status in {"administrator", "creator"} and getattr(member, "can_post_messages", None) is not False
    if status in {"administrator", "creator", "member"}:
        return True
    if status == "restricted":
        return bool(getattr(member, "can_send_messages", False))
    return False


# ---- who is allowed: owners (ADMIN_IDS variable) + admins added with /addadmin
def all_admin_ids() -> set:
    return set(ADMIN_IDS) | storage.extra_admin_ids()


def is_admin(message: Message) -> bool:
    return bool(message.from_user and message.from_user.id in all_admin_ids())


def is_owner(message: Message) -> bool:
    return bool(message.from_user and message.from_user.id in ADMIN_IDS)


async def deny(message: Message):
    if message.chat.type != "private":
        return  # stay silent in groups
    uid = message.from_user.id if message.from_user else "?"
    await message.answer(f"Not authorized. Your Telegram user ID: <code>{uid}</code>")


async def notify_admins(text: str):
    for admin_id in all_admin_ids():
        try:
            await bot.send_message(admin_id, text)
        except TelegramAPIError:
            logging.warning("Could not notify admin %s; they may need to start the bot first.", admin_id)


# Errors that mean "the bot can no longer post in this chat" -> remove it, never retry.
LOST_ACCESS_HINTS = (
    "not enough rights to send text", "have no rights to send a message", "need administrator rights",
    "chat_write_forbidden", "chat not found", "bot is not a member", "bot was kicked",
    "channel_private", "chat was deleted", "group chat was deleted",
)


def lost_access(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(hint in text for hint in LOST_ACCESS_HINTS)


async def drop_channel(chat_id: int, title: str, reason: str):
    """Remove a destination completely (no more scheduling, no retries) and tell the admins once."""
    await db(storage.remove_channel, chat_id)
    logging.warning("Destination %s (%s) removed: %s", chat_id, title, reason)
    await notify_admins(
        "<b>Destination removed automatically</b>\n\n"
        f"<b>Chat:</b> {escape(title)}\n"
        f"<b>ID:</b> <code>{chat_id}</code>\n"
        f"<b>Reason:</b> {escape(reason)}\n\n"
        "The bot will not post or retry there. To use it again, add the bot back with permission "
        "to post (channel: admin with 'Post messages')."
    )


def markup_json(message: Message):
    if not message.reply_markup:
        return None
    try:
        return message.reply_markup.model_dump_json(exclude_none=True)
    except Exception:
        return None


def origin_ref(message: Message):
    """Where to copy a saved post from. A post forwarded from one of our source channels is referenced by
    its ORIGINAL channel and ID (no duplicates, survives deleting the chat message); anything else is
    referenced by this private chat."""
    origin = getattr(message, "forward_origin", None)
    if origin is not None and getattr(origin, "type", None) == "channel":
        chat = getattr(origin, "chat", None)
        if chat is not None and storage.is_source(chat.id):
            return chat.id, origin.message_id
    return message.chat.id, message.message_id


def queue_post(message: Message, assign: bool = True, imported: bool = False):
    entities = [e.model_dump(exclude_none=True) for e in (message.entities or [])]
    caption_entities = [e.model_dump(exclude_none=True) for e in (message.caption_entities or [])]
    payload = json.dumps({"text": entities, "caption": caption_entities}, ensure_ascii=False)
    content_type = str(getattr(message.content_type, "value", message.content_type))
    file_id = message.photo[-1].file_id if message.photo else None
    source_chat, source_message = origin_ref(message)
    return storage.add_post(
        source_chat, source_message, message.caption or message.text, payload, markup_json(message),
        content_type, file_id, message.media_group_id, assign, imported,
    )


def parse_target(raw: str):
    """'all' -> 'all', number -> int, anything else -> ValueError."""
    raw = raw.strip()
    return "all" if raw.lower() == "all" else int(raw)


def target_from(message: Message, raw: str):
    """Target from the command argument; inside a group the chat itself is the default."""
    if raw.strip():
        return parse_target(raw)
    if message.chat.type in GROUP_TYPES:
        return message.chat.id
    raise ValueError


def command_args(message: Message) -> str:
    return (message.text or "").partition(" ")[2].strip()


# --------------------------------------------- bot added to / removed from chats

@dp.my_chat_member()
async def detect_bot_membership(update: ChatMemberUpdated):
    chat = update.chat
    if chat.type not in DEST_TYPES or storage.is_source(chat.id):
        return  # private chats and source channels are never destinations
    title = chat.title or str(chat.id)
    registered = any(int(c["chat_id"]) == chat.id for c in await db(storage.list_channels))

    if not bot_can_post(chat.type, update.new_chat_member):
        if registered:
            await drop_channel(chat.id, title, "bot was removed or can no longer post there")
        return
    if registered:
        return  # some other permission changed; nothing to do

    adder = update.from_user.id if update.from_user else None
    if adder not in all_admin_ids():
        logging.info("Bot added to %s (%s) by a non-admin; ignored.", chat.id, title)
        return

    await db(storage.register_channel_if_missing, chat.id, title, chat.type)
    logging.info("Registered destination %s (%s, %s)", chat.id, title, chat.type)
    await notify_admins(
        f"<b>{'Channel' if chat.type == 'channel' else 'Group'} added</b>\n\n"
        f"<b>Name:</b> {escape(title)}\n"
        f"<b>ID:</b> <code>{chat.id}</code>\n"
        f"<b>Batch:</b> up to {BATCH_SIZE} posts, then every {DEFAULT_INTERVAL_MINUTES} minutes\n\n"
        "Nothing else to do. Use /channels to see it or /removechannel to remove it."
    )


@dp.message(F.migrate_to_chat_id)
async def group_upgraded(message: Message):
    """Telegram gave an upgraded group a new chat ID; keep its schedule."""
    await db(storage.migrate_channel, message.chat.id, message.migrate_to_chat_id)
    logging.info("Group %s upgraded to %s", message.chat.id, message.migrate_to_chat_id)


# ------------------------------------------------------------------ command menu

# The "Menu" button next to the message box. Admins see everything; other people only see /start.
ADMIN_MENU = [
    ("help", "Show all commands and how they work"),
    ("channels", "Destinations: schedule, pending, next run"),
    ("stats", "Post counts: library, used, remaining"),
    ("addchannel", "Add a channel/group by ID or @username"),
    ("addmany", "Add many channels/groups at once (IDs or @usernames)"),
    ("removechannel", "Remove a destination (tap to choose)"),
    ("setinterval", "Set wait time between batches"),
    ("setbatch", "Set posts per batch"),
    ("pause", "Pause a destination"),
    ("resume", "Resume a paused destination"),
    ("sendnow", "Send a batch now (even in quiet hours)"),
    ("broadcast", "Reply to a post: send to all now"),
    ("sources", "List source channels"),
    ("addsource", "Add a source channel"),
    ("removesource", "Remove a source channel"),
    ("detect", "Find channels/groups where the bot is already admin"),
    ("railway_vars", "IDs to paste into Railway variables"),
    ("setbackup", "Set the backup channel"),
    ("backup", "Copy all posts into the backup channel"),
    ("export_backup", "Get a backup file of the whole bot"),
    ("import_backup", "Restore the bot from a backup file (owner)"),
    ("restore_posts", "Re-post all backed-up posts into a channel"),
    ("job", "Progress of the running backup/restore"),
    ("stopjob", "Stop the running backup/restore"),
    ("setfooter", "Set footer text under posts"),
    ("clearfooter", "Remove footer"),
    ("setbutton", "Button under the last post of each batch"),
    ("clearbutton", "Remove that button"),
    ("settimes", "Fixed posting times (09:00,14:00) instead of interval"),
    ("cleartimes", "Back to interval posting"),
    ("branding", "Show footer, button and fixed-time settings"),
    ("addadmin", "Give someone admin access (owner only)"),
    ("removeadmin", "Remove an admin (owner only)"),
    ("admins", "List owners and admins"),
]
GROUP_MENU = [
    ("addhere", "Send scheduled posts to this group/topic"),
    ("removehere", "Stop scheduled posts here"),
    ("pause", "Pause posting here"),
    ("resume", "Resume posting here"),
    ("sendnow", "Send a batch here now"),
    ("setinterval", "Set minutes between batches here"),
    ("setbatch", "Set posts per batch here"),
    ("settimes", "Set fixed posting times here"),
]
PUBLIC_MENU = [("start", "Start the bot")]


def _commands(items):
    return [BotCommand(command=name, description=text) for name, text in items]


async def set_admin_menu(user_id: int):
    """Show the full command menu in one admin's private chat."""
    try:
        await bot.set_my_commands(_commands(ADMIN_MENU), scope=BotCommandScopeChat(chat_id=user_id))
    except TelegramAPIError:
        logging.info("Could not set the menu for %s yet (they must open the bot and send /start).", user_id)


async def clear_admin_menu(user_id: int):
    try:
        await bot.delete_my_commands(scope=BotCommandScopeChat(chat_id=user_id))
    except TelegramAPIError:
        pass


async def setup_command_menu():
    try:
        await bot.set_my_commands(_commands(PUBLIC_MENU), scope=BotCommandScopeDefault())
        await bot.set_my_commands(_commands(GROUP_MENU), scope=BotCommandScopeAllChatAdministrators())
    except TelegramAPIError:
        logging.exception("Could not set the default command menu")
    for admin_id in all_admin_ids():
        await set_admin_menu(admin_id)


# ------------------------------------------------------------------ help

def help_text() -> str:
    return (
        "<b>Auto Forward Scheduler — Help</b>\n"
        "Tap the <b>Menu</b> button next to the message box to see every command.\n\n"
        "<b>Destinations (channels / groups)</b>\n"
        "Add the bot with an admin account (channel: as admin with 'Post messages'; group: as member) "
        "and it registers itself. Or: /addchannel -100… | @name, or /addhere inside a group/topic.\n"
        "Many at once: /addmany then paste all IDs / @usernames (one per line).\n"
        "/channels · /removechannel · /removehere\n"
        "/setinterval · /setbatch · /pause · /resume  (CHAT_ID or all)\n\n"
        "<b>Sources &amp; old posts</b>\n"
        "/sources · /addsource -100… · /removesource -100…\n"
        "\n"
        "<b>After a reset / redeploy</b>\n"
        "Set BACKUP_CHANNEL_ID (and optionally DESTINATION_CHAT_IDS) in Railway: the bot then finds its channels "
        "and settings by itself, no need to re-add it as admin. /detect runs the search now; /railway_vars shows "
        "the values to paste.\n\n"
        "<b>Backup &amp; restore</b>\n"
        "/setbackup -100… — a private channel where the bot keeps a copy of every post\n"
        "/backup — copy everything now (new posts are copied automatically)\n"
        "/export_backup — settings + post list as a file (also sent daily to owners)\n"
        "/restore_posts CHANNEL — post all backed-up posts into a (new) channel\n"
        "/import_backup — reply to a backup file to rebuild the bot · /job · /stopjob\n\n"
        "<b>Counts &amp; alerts</b>\n"
        "/stats — posts, used, remaining, sent in 24h\n"
        f"Low-post alert at {LOW_POST_THRESHOLD} left; alert when all posts are used; "
        + (f"daily report at {DAILY_REPORT_HOUR:02d}:00." if DAILY_REPORT_HOUR >= 0 else "daily report off.") + "\n\n"
        "<b>Footer, button &amp; fixed times</b>\n"
        "/setfooter all | text — added under every post\n"
        "/setbutton all | Join Channel | https://t.me/yourchannel — button under the <b>last post of each batch</b>\n"
        "/settimes all | 09:00,14:00,21:00 — post at these times instead of every N minutes\n"
        "/clearfooter · /clearbutton · /cleartimes · /branding\n"
        "Use a chat ID instead of <code>all</code> for one destination, or <code>-</code> as text to switch it off there.\n\n"
        "<b>Send now</b>\n"
        "/sendnow all|CHAT_ID [count] — sends immediately, <b>even in quiet hours</b>\n"
        "/broadcast — reply to a post to send it everywhere now\n\n"
        "<b>Admins</b> (owner only)\n"
        "/addadmin USER_ID · /removeadmin USER_ID · /admins\n\n"
        "<b>How it works</b>\n"
        f"Each destination sends up to {BATCH_SIZE} posts, then waits its interval (or the next fixed time). New posts go to one "
        "destination (round-robin); when its queue is empty it sends random posts from the library without "
        f"repeats until all were used. Quiet hours: {quiet_label()}."
    )


@dp.message(CommandStart())
async def start(message: Message):
    if not is_admin(message):
        return await deny(message)
    if message.chat.type == "private":
        await set_admin_menu(message.from_user.id)
    await message.answer(help_text())


@dp.message(Command("help"))
async def help_cmd(message: Message):
    if not is_admin(message):
        return await deny(message)
    if message.chat.type == "private":
        await set_admin_menu(message.from_user.id)
    await message.answer(help_text())


# ------------------------------------------------------------------ broadcast

async def direct_broadcast(message: Message):
    target = message.reply_to_message
    if not target:
        await message.answer("Reply to the post you want to broadcast, then send /broadcast.")
        return
    channels = await db(storage.list_channels)
    if not channels:
        await message.answer("No destinations registered.")
        return

    async def send(channel):
        kwargs = {"chat_id": int(channel["chat_id"]), "from_chat_id": message.chat.id, "message_id": target.message_id}
        if channel["thread_id"]:
            kwargs["message_thread_id"] = channel["thread_id"]
        await bot.copy_message(**kwargs)

    succeeded, failed = [], []
    for channel in channels:
        chat_id = int(channel["chat_id"])
        try:
            await send(channel)
            succeeded.append(chat_id)
        except TelegramForbiddenError:
            await drop_channel(chat_id, channel["title"], "bot has no access to post there")
            failed.append(chat_id)
        except TelegramRetryAfter as exc:
            await asyncio.sleep(int(exc.retry_after) + 1)
            try:
                await send(channel)
                succeeded.append(chat_id)
            except TelegramAPIError:
                logging.exception("Direct broadcast failed for %s", chat_id)
                failed.append(chat_id)
        except TelegramAPIError:
            logging.exception("Direct broadcast failed for %s", chat_id)
            failed.append(chat_id)
        await asyncio.sleep(0.5)

    summary = f"Direct broadcast: sent to {len(succeeded)}/{len(channels)} destination(s)."
    if failed:
        summary += "\nFailed IDs: " + ", ".join(str(c) for c in failed)
    await message.answer(summary)


# ------------------------------------------------------- destinations: add / remove

async def resolve_chat(ref: str):
    """Chat from a numeric ID or @username, or None."""
    try:
        return await bot.get_chat(ref if ref.startswith("@") else int(ref))
    except (ValueError, TelegramAPIError):
        return None


@dp.message(Command("addchannel"))
async def add_channel_cmd(message: Message):
    if not is_admin(message):
        return await deny(message)
    tokens = [t for t in re.split(r"[|\s]+", command_args(message)) if t]
    if not tokens:
        return await message.answer(
            "Format: <code>/addchannel -1001234567890</code> or <code>/addchannel @name</code>\n"
            f"Optional at the end: minutes (default {DEFAULT_INTERVAL_MINUTES}) and <code>topic=ID</code> "
            "for a group topic. The bot must already be in that chat."
        )
    interval, thread_id = DEFAULT_INTERVAL_MINUTES, None
    for token in tokens[1:]:
        topic = re.fullmatch(r"topic[=:]?(\d+)", token.lower())
        if topic:
            thread_id = int(topic.group(1))
        elif token.isdigit():
            interval = max(1, int(token))

    chat = await resolve_chat(tokens[0])
    if chat is None:
        return await message.answer("I cannot access that chat. Send a valid ID (-100…) or @username, and add the bot there first.")
    if chat.type not in DEST_TYPES:
        return await message.answer("That is not a channel or group.")
    if storage.is_source(chat.id):
        return await message.answer("That is one of your SOURCE channels. Posts are collected from it, never sent to it.")
    try:
        me = await bot.get_chat_member(chat.id, (await bot.get_me()).id)
    except TelegramAPIError:
        return await message.answer("I cannot check my permissions there. Add the bot first.")
    if not bot_can_post(chat.type, me):
        return await message.answer("The bot is there but cannot post. Channel: make it admin with 'Post messages'.")

    title = chat.title or str(chat.id)
    await db(storage.add_channel, chat.id, title, interval, chat.type, thread_id)
    topic_note = f", topic {thread_id}" if thread_id else ""
    await message.answer(
        f"Added {icon(chat.type)} <b>{escape(title)}</b> (<code>{chat.id}</code>{topic_note}): "
        f"up to {BATCH_SIZE} posts per batch, then every {interval} minute(s)."
    )


@dp.message(Command("addmany"))
async def add_many_cmd(message: Message):
    """Add several destinations in one message: IDs (-100...) and/or @usernames, separated by lines/spaces/commas."""
    if not is_admin(message):
        return await deny(message)
    parts = (message.text or "").split(maxsplit=1)
    raw = parts[1] if len(parts) > 1 else ""
    interval = DEFAULT_INTERVAL_MINUTES
    refs: list = []
    for token in [t for t in re.split(r"[,;|\s]+", raw) if t]:
        timing = re.fullmatch(r"(?:interval|min|minutes)[=:]?(\d+)", token.lower())
        if timing:
            interval = max(1, int(timing.group(1)))
        elif token.startswith("@") or re.fullmatch(r"-\d{5,}", token):
            if token not in refs:
                refs.append(token)
        elif token.isdigit() and len(token) <= 5:
            interval = max(1, int(token))
    if not refs:
        return await message.answer(
            "Paste many chats in one message, one per line:\n"
            "<code>/addmany\n-1001234567890\n-1009876543210\n@publicchannel</code>\n"
            f"Optional: <code>interval=200</code> (minutes between batches, default {DEFAULT_INTERVAL_MINUTES}). "
            "The bot must already be in each chat (channel: admin with 'Post messages')."
        )

    existing = {int(c["chat_id"]) for c in await db(storage.list_channels)}
    me_id = (await bot.get_me()).id
    added, skipped, failed = [], [], []
    for ref in refs:
        await asyncio.sleep(0.2)  # stay clear of Telegram flood limits
        chat = await resolve_chat(ref)
        if chat is None:
            failed.append(f"❌ <code>{escape(ref)}</code> — cannot access it (wrong ID, or the bot is not there)")
            continue
        title = chat.title or str(chat.id)
        if chat.type not in DEST_TYPES:
            failed.append(f"❌ <b>{escape(title)}</b> <code>{chat.id}</code> — not a channel or group")
            continue
        if storage.is_source(chat.id):
            failed.append(f"❌ <b>{escape(title)}</b> <code>{chat.id}</code> — this is a SOURCE channel")
            continue
        if chat.id in existing:
            skipped.append(f"↩️ <b>{escape(title)}</b> <code>{chat.id}</code>")
            continue
        try:
            me = await bot.get_chat_member(chat.id, me_id)
        except TelegramAPIError:
            failed.append(f"❌ <b>{escape(title)}</b> <code>{chat.id}</code> — cannot check my permissions there")
            continue
        if not bot_can_post(chat.type, me):
            failed.append(f"❌ <b>{escape(title)}</b> <code>{chat.id}</code> — bot cannot post (make it admin with 'Post messages')")
            continue
        await db(storage.add_channel, chat.id, title, interval, chat.type, None)
        existing.add(chat.id)
        added.append(f"✅ {icon(chat.type)} <b>{escape(title)}</b> <code>{chat.id}</code>")

    lines = [
        f"<b>/addmany</b>: {len(added)} added · {len(skipped)} already there · {len(failed)} failed "
        f"(every {interval} min, up to {BATCH_SIZE} posts per batch)"
    ]
    if added:
        lines += [""] + added
    if skipped:
        lines += ["", "Already added (unchanged):"] + skipped
    if failed:
        lines += ["", "Not added:"] + failed
    chunk = ""
    for line in lines:
        if len(chunk) + len(line) + 1 > 3500:
            await message.answer(chunk)
            chunk = ""
        chunk += line + "\n"
    if chunk.strip():
        await message.answer(chunk)


@dp.message(Command("addhere"), F.chat.type.in_(GROUP_TYPES))
async def add_here_cmd(message: Message):
    if not is_admin(message):
        return await deny(message)
    args = command_args(message).split()
    interval = max(1, int(args[0])) if args and args[0].isdigit() else DEFAULT_INTERVAL_MINUTES
    chat = message.chat
    try:
        me = await bot.get_chat_member(chat.id, (await bot.get_me()).id)
    except TelegramAPIError:
        return await message.answer("I cannot check my permissions here.")
    if not bot_can_post(chat.type, me):
        return await message.answer("I do not have permission to send messages here. Fix that and try again.")
    thread_id = message.message_thread_id if message.is_topic_message else None
    await db(storage.add_channel, chat.id, chat.title or str(chat.id), interval, chat.type, thread_id)
    where = f"this topic (#{thread_id})" if thread_id else "this group"
    await message.answer(f"Added: posts will go to {where}, up to {BATCH_SIZE} per batch, every {interval} minute(s).")


@dp.message(Command("removehere"), F.chat.type.in_(GROUP_TYPES))
async def remove_here_cmd(message: Message):
    if not is_admin(message):
        return await deny(message)
    await db(storage.remove_channel, message.chat.id)
    await message.answer("Removed. No more scheduled posts here.")


@dp.message(Command("removechannel"))
async def remove_channel_cmd(message: Message):
    if not is_admin(message):
        return await deny(message)
    args = command_args(message)
    if args:
        try:
            chat_id = int(args)
        except ValueError:
            return await message.answer("Send /removechannel alone and tap a chat, or /removechannel CHAT_ID.")
        await db(storage.remove_channel, chat_id)
        return await message.answer(f"Removed <code>{chat_id}</code>. No more posts will be sent there.")

    channels = await db(storage.list_channels)
    if not channels:
        return await message.answer("No destinations registered.")
    rows = [
        [InlineKeyboardButton(text=f"🗑 {icon(c['kind'])} {c['title'][:36]}", callback_data=f"rm:{c['chat_id']}")]
        for c in channels
    ]
    await message.answer("Tap the one you want to remove:", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@dp.callback_query(F.data.startswith("rm"))
async def remove_channel_buttons(query: CallbackQuery):
    if query.from_user.id not in all_admin_ids():
        return await query.answer("Not authorized", show_alert=True)
    action, _, raw_id = (query.data or "").partition(":")
    if action == "rmn":
        await query.message.edit_text("Cancelled.")
        return await query.answer()
    try:
        chat_id = int(raw_id)
    except ValueError:
        return await query.answer()
    if action == "rm":
        title = next((c["title"] for c in await db(storage.list_channels) if int(c["chat_id"]) == chat_id), str(chat_id))
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Yes, remove", callback_data=f"rmy:{chat_id}"),
            InlineKeyboardButton(text="Cancel", callback_data="rmn"),
        ]])
        await query.message.edit_text(f"Remove <b>{escape(title)}</b>?", reply_markup=keyboard)
    elif action == "rmy":
        await db(storage.remove_channel, chat_id)
        await query.message.edit_text(f"Removed <code>{chat_id}</code>. No more posts will be sent there.")
    await query.answer()


# ------------------------------------------------------- schedule settings

@dp.message(Command("setinterval"))
async def set_interval_cmd(message: Message):
    if not is_admin(message):
        return await deny(message)
    parts = [p.strip() for p in command_args(message).split("|")]
    try:
        if len(parts) == 2:
            target, minutes = parse_target(parts[0]), int(parts[1])
        elif len(parts) == 1 and parts[0].isdigit() and message.chat.type in GROUP_TYPES:
            target, minutes = message.chat.id, int(parts[0])
        else:
            raise ValueError
        if minutes < 1:
            raise ValueError
    except ValueError:
        return await message.answer("Format: /setinterval CHAT_ID | MINUTES   (or: /setinterval all | MINUTES)")
    count = await db(storage.set_interval, target, minutes)
    await message.answer(f"Updated {count} destination(s): next batches wait {minutes} minute(s)." if count
                         else "Destination not found. Check /channels.")


@dp.message(Command("setbatch"))
async def set_batch_cmd(message: Message):
    if not is_admin(message):
        return await deny(message)
    parts = [p.strip() for p in command_args(message).split("|")]
    try:
        if len(parts) == 2:
            target, size = parse_target(parts[0]), int(parts[1])
        elif len(parts) == 1 and parts[0].isdigit() and message.chat.type in GROUP_TYPES:
            target, size = message.chat.id, int(parts[0])
        else:
            raise ValueError
        if size < 1:
            raise ValueError
    except ValueError:
        return await message.answer("Format: /setbatch CHAT_ID | POSTS   (or: /setbatch all | POSTS)")
    count = await db(storage.set_batch_size, target, size)
    await message.answer(f"Updated {count} destination(s): {size} post(s) per batch." if count
                         else "Destination not found. Check /channels.")


def _simple_target_command(name: str, action, done: str):
    @dp.message(Command(name))
    async def handler(message: Message):
        if not is_admin(message):
            return await deny(message)
        try:
            target = target_from(message, command_args(message))
        except ValueError:
            return await message.answer(f"Format: /{name} CHAT_ID   (or: /{name} all)")
        count = await db(action, target)
        await message.answer(f"{done}: {count} destination(s)." if count else "Destination not found. Check /channels.")
    return handler


_simple_target_command("pause", lambda t: storage.set_paused(t, True), "Paused")
_simple_target_command("resume", lambda t: storage.set_paused(t, False), "Resumed")


@dp.message(Command("sendnow"))
async def send_now_cmd(message: Message):
    """Start a batch immediately, even during quiet hours. Optional post count: /sendnow all 3"""
    if not is_admin(message):
        return await deny(message)
    target, count = None, None
    for token in command_args(message).replace("|", " ").split():
        if token.lower() == "all" or token.lstrip("-").isdigit() and token.startswith("-"):
            target = parse_target(token)
        elif token.isdigit():
            count = max(1, int(token))
    if target is None and message.chat.type in GROUP_TYPES:
        target = message.chat.id
    if target is None:
        return await message.answer("Format: /sendnow CHAT_ID [posts]   or   /sendnow all [posts]")
    n = await db(storage.send_now, target, count)
    if not n:
        return await message.answer("Destination not found. Check /channels.")
    await message.answer(
        f"Sending now to {n} destination(s)"
        + (f": {count} post(s) each" if count else ": a full batch each")
        + ". Works even during quiet hours."
    )


# ------------------------------------------------------- list and counts

def build_stats_text(title: str = "Statistics") -> str:
    data = storage.stats()
    lines = [f"<b>📊 {title}</b>", f"Library: <b>{data['total']}</b> post(s)"]
    for s in data["sources"]:
        lines.append(f"  • source {escape(s['title'])} <code>{s['chat_id']}</code>: {s['posts']}")
    if data["manual"]:
        lines.append(f"  • added manually / forwarded: {data['manual']}")
    backup_chat = storage.get_backup_chat()
    if backup_chat:
        done, total = storage.backup_counts(backup_chat)
        lines.append(f"🗄 Backup channel: {done}/{total} post(s) backed up")
    else:
        lines.append("🗄 Backup channel: not set (/setbackup)")
    lines.append("")
    if not data["destinations"]:
        lines.append("No destinations registered.")
    for d in data["destinations"]:
        flag = " ⏸ paused" if d["paused"] else ""
        lines.append(
            f"{icon(d['kind'])} <b>{escape(d['title'])}</b> <code>{d['chat_id']}</code>{flag}\n"
            f"   sent 24h: {d['sent_24h']} · used {d['used']}/{data['total']} · "
            f"remaining {d['remaining']} · pending {d['pending']} · cycles {d['cycles']}"
        )
    return "\n".join(lines)


@dp.message(Command("stats"))
async def stats_cmd(message: Message):
    if not is_admin(message):
        return await deny(message)
    await message.answer(await db(build_stats_text))


@dp.message(Command("channels"))
async def channels_cmd(message: Message):
    if not is_admin(message):
        return await deny(message)
    channels = await db(storage.list_channels)
    library = await db(storage.library_size)
    if not channels:
        return await message.answer("No destinations registered. Add the bot to a channel or group.")
    lines = []
    for c in channels:
        extras = []
        if c["paused"]:
            extras.append("⏸ paused")
        if c["thread_id"]:
            extras.append(f"topic {c['thread_id']}")
        times = await db(storage.get_branding, "times", int(c["chat_id"]))
        when = f"at {times.replace(',', ', ')}" if times else f"every {c['interval_minutes']} min"
        lines.append(
            f"{icon(c['kind'])} <b>{escape(c['title'])}</b> <code>{c['chat_id']}</code>"
            + (f" ({', '.join(extras)})" if extras else "")
            + f"\n   {when} · batch {c['batch_size'] or BATCH_SIZE} · "
              f"pending {c['pending']} · next {fmt_local(c['next_run'])}"
        )
    await message.answer(
        f"<b>Destinations</b> ({len(channels)})\nLibrary: {library} post(s)\n"
        f"Quiet hours: {quiet_label()}\n\n" + "\n".join(lines)
    )


# ------------------------------------------------------- sources and old posts

@dp.message(Command("sources"))
async def sources_cmd(message: Message):
    if not is_admin(message):
        return await deny(message)
    sources = await db(storage.list_sources)
    if not sources:
        return await message.answer("No source channels yet. Use /addsource -100… (the bot must be admin there).")
    lines = [f"📥 <b>{escape(s['title'])}</b> <code>{s['chat_id']}</code> — {s['posts']} post(s)" for s in sources]
    await message.answer("<b>Source channels</b>\n" + "\n".join(lines))


@dp.message(Command("addsource"))
async def add_source_cmd(message: Message):
    if not is_admin(message):
        return await deny(message)
    ref = command_args(message).split()[0] if command_args(message) else ""
    if not ref:
        return await message.answer(
            "Format: <code>/addsource -1001234567890</code> or <code>/addsource @name</code>\n"
            "Make the bot admin in that channel first. New posts there are collected automatically."
        )
    chat = await resolve_chat(ref)
    if chat is None or chat.type != "channel":
        return await message.answer("I cannot access that channel. Add the bot as admin there and try again.")
    try:
        me = await bot.get_chat_member(chat.id, (await bot.get_me()).id)
    except TelegramAPIError:
        return await message.answer("I cannot check my permissions there. Make the bot admin first.")
    if status_of(me) not in {"administrator", "creator"}:
        return await message.answer("The bot must be an admin of that channel to read its posts.")
    title = chat.title or str(chat.id)
    await db(storage.add_source, chat.id, title)
    await message.answer(
        f"Source added: <b>{escape(title)}</b> (<code>{chat.id}</code>). New posts are collected automatically.\n"
        "It is no longer a destination, if it was one."
    )


@dp.message(Command("removesource"))
async def remove_source_cmd(message: Message):
    if not is_admin(message):
        return await deny(message)
    try:
        chat_id = int(command_args(message))
    except ValueError:
        return await message.answer("Format: /removesource CHAT_ID   (see /sources)")
    if await db(storage.remove_source, chat_id):
        await message.answer(
            "Source removed: its new posts are no longer collected. Posts already in the library stay.\n"
            "Note: a source listed in the SOURCE_CHANNEL_ID(S) variable comes back after a restart."
        )
    else:
        await message.answer("That ID is not a source. Check /sources.")


# ------------------------------------------------------- backup channel, snapshot file, restore

JOB = {"task": None, "kind": "", "total": 0, "done": 0, "skipped": 0, "started": None}


def is_missing_message(text: str) -> bool:
    return "message to copy not found" in text or "message_id_invalid" in text or "can't be copied" in text


async def can_post_in(chat_id: int):
    """True / False if the bot can / cannot post in that chat; None when Telegram could not be reached."""
    try:
        chat = await bot.get_chat(chat_id)
        me = await bot.get_chat_member(chat_id, (await bot.get_me()).id)
    except (TelegramForbiddenError, TelegramBadRequest):
        return False
    except TelegramAPIError:
        return None
    return bot_can_post(chat.type, me)


async def source_unreachable(post, destination_id: int):
    """The bot cannot read a post's source channel (and there is no backup copy): pause, warn once in a while."""
    await db(storage.postpone_channel, destination_id, utc_in(minutes=30))
    source = post["source_chat_id"]
    key = f"srcalert:{source}"
    last = await db(storage.get_setting, key)
    if last is None or last < (datetime.now(timezone.utc) - timedelta(hours=12)).isoformat():
        await db(storage.set_setting, key, datetime.now(timezone.utc).isoformat())
        await notify_admins(
            f"⚠️ <b>Cannot read posts from</b> <code>{source}</code>\n"
            "The bot was removed from that channel or the channel is gone. Sending of posts from it is paused "
            "(will retry). Add the bot back there as admin, or restore your posts from the backup channel "
            "with /restore_posts."
        )


def post_markup(post):
    if not post["reply_markup_json"]:
        return None
    try:
        return InlineKeyboardMarkup.model_validate_json(post["reply_markup_json"])
    except Exception:
        return None


async def copy_safely(kwargs: dict):
    """Copy one message with retries. Returns ('ok', new_message_id) | ('skip', None) | ('fatal', None)."""
    destination = kwargs["chat_id"]
    for _ in range(3):
        try:
            sent = await bot.copy_message(**kwargs)
            return "ok", sent.message_id
        except TelegramRetryAfter as exc:
            await asyncio.sleep(int(exc.retry_after) + 1)
        except TelegramMigrateToChat:
            return "fatal", None
        except (TelegramForbiddenError, TelegramBadRequest) as exc:
            text = str(exc).lower()
            if is_missing_message(text):
                return "skip", None
            if isinstance(exc, TelegramForbiddenError) or lost_access(exc):
                return ("fatal", None) if await can_post_in(destination) is False else ("skip", None)
            logging.warning("Copy rejected (%s)", exc)
            return "skip", None
        except TelegramAPIError:
            logging.exception("Copy failed")
            return "skip", None
    return "skip", None


async def backup_post(post, backup_chat: int) -> str:
    kwargs = {"chat_id": backup_chat, "from_chat_id": post["source_chat_id"], "message_id": post["message_id"]}
    markup = post_markup(post)
    if markup:
        kwargs["reply_markup"] = markup
    outcome, new_id = await copy_safely(kwargs)
    if outcome == "ok":
        await db(storage.save_backup, post["id"], backup_chat, new_id)
    return outcome


async def maybe_backup_live(queue_id: int):
    """A new post just arrived: copy it into the backup channel right away (if one is set)."""
    try:
        backup_chat = await db(storage.get_backup_chat)
        if not backup_chat:
            return
        post = await db(storage.get_post, queue_id)
        if post and post["source_chat_id"] != backup_chat:
            await backup_post(post, backup_chat)
    except Exception:
        logging.exception("Live backup failed for post %s", queue_id)


# ---- one background job at a time (backup or restore)

def job_running() -> bool:
    task = JOB["task"]
    return task is not None and not task.done()


def eta_text(items: int) -> str:
    minutes = max(1, round(items * BACKUP_DELAY_SECONDS / 60))
    return f"about {minutes} min" if minutes < 120 else f"about {minutes / 60:.1f} hours"


async def tell(chat_id: int, text: str):
    try:
        await bot.send_message(chat_id, text)
    except TelegramAPIError:
        logging.warning("Could not send job message to %s", chat_id)


async def run_backup_job(notify_chat: int, backup_chat: int):
    posts = await db(storage.posts_needing_backup, backup_chat)
    JOB.update(kind="Backup", total=len(posts), done=0, skipped=0, started=time.time())
    if not posts:
        return await tell(notify_chat, "Everything is already backed up. ✅")
    await tell(notify_chat, f"📦 Backup started: {len(posts)} post(s), {eta_text(len(posts))}.\n/job shows progress, /stopjob stops it.")
    fatal = False
    try:
        for i, post in enumerate(posts, 1):
            outcome = await backup_post(post, backup_chat)
            if outcome == "fatal":
                fatal = True
                break
            JOB["done" if outcome == "ok" else "skipped"] += 1
            if i % 250 == 0 and i < len(posts):
                await tell(notify_chat, f"📦 Backup: {i}/{len(posts)}")
            await asyncio.sleep(BACKUP_DELAY_SECONDS)
    except asyncio.CancelledError:
        await tell(notify_chat, f"⏹ Backup stopped at {JOB['done']}/{JOB['total']}. Run /backup again to continue; finished posts are skipped.")
        raise
    if fatal:
        return await tell(notify_chat, "❌ Backup stopped: the bot cannot post in the backup channel. Make it admin with 'Post messages', then run /backup again.")
    await tell(notify_chat, f"✅ Backup finished: {JOB['done']} copied, {JOB['skipped']} skipped (posts that no longer exist).\nTip: /export_backup saves the settings and post list as a file.")


async def run_restore_job(notify_chat: int, backup_chat: int, targets: list, items: list):
    JOB.update(kind="Restore", total=len(items) * len(targets), done=0, skipped=0, started=time.time())
    await tell(notify_chat, f"♻️ Restore started: {len(items)} post(s) → {len(targets)} destination(s), {eta_text(JOB['total'])}.\n/job shows progress, /stopjob stops it.")
    last_mid = None
    try:
        for target in targets:
            for index, (mid, markup_json) in enumerate(items, 1):
                kwargs = {"chat_id": target["chat_id"], "from_chat_id": backup_chat, "message_id": mid}
                if target.get("thread_id"):
                    kwargs["message_thread_id"] = target["thread_id"]
                if markup_json:
                    try:
                        kwargs["reply_markup"] = InlineKeyboardMarkup.model_validate_json(markup_json)
                    except Exception:
                        pass
                outcome, _ = await copy_safely(kwargs)
                if outcome == "fatal":
                    await tell(notify_chat, f"❌ Cannot post in {target['title']} any more; skipping it.")
                    JOB["skipped"] += len(items) - index + 1
                    break
                JOB["done" if outcome == "ok" else "skipped"] += 1
                last_mid = mid
                if index % 250 == 0 and index < len(items):
                    await tell(notify_chat, f"♻️ {target['title']}: {index}/{len(items)}")
                await asyncio.sleep(BACKUP_DELAY_SECONDS)
    except asyncio.CancelledError:
        hint = ""
        if len(targets) == 1 and last_mid:
            hint = f"\nContinue later with /restore_posts {targets[0]['chat_id']} {last_mid + 1} {items[-1][0]}"
        await tell(notify_chat, f"⏹ Restore stopped at {JOB['done']}/{JOB['total']}.{hint}")
        raise
    await tell(notify_chat, f"✅ Restore finished: {JOB['done']} posted, {JOB['skipped']} skipped.")


def start_job(coro):
    JOB["task"] = asyncio.create_task(coro)


@dp.message(Command("job"))
async def job_cmd(message: Message):
    if not is_admin(message):
        return await deny(message)
    if not job_running():
        return await message.answer("No backup or restore is running.")
    elapsed = int(time.time() - JOB["started"])
    await message.answer(
        f"<b>{JOB['kind']}</b> running: {JOB['done']} done, {JOB['skipped']} skipped, of {JOB['total']} "
        f"({elapsed // 60} min so far). /stopjob to stop."
    )


@dp.message(Command("stopjob"))
async def stopjob_cmd(message: Message):
    if not is_admin(message):
        return await deny(message)
    if not job_running():
        return await message.answer("No backup or restore is running.")
    JOB["task"].cancel()
    await message.answer("Stopping…")


@dp.message(Command("setbackup"))
async def set_backup_cmd(message: Message):
    if not is_admin(message):
        return await deny(message)
    args = command_args(message).split()
    current = await db(storage.get_backup_chat)
    if not args:
        shown = f"Current backup channel: <code>{current}</code>\n\n" if current else "No backup channel set yet.\n\n"
        return await message.answer(
            shown + "Create a <b>private channel</b> just for backups, add this bot as admin ('Post messages'), then:\n"
            "<code>/setbackup -1001234567890</code> (or @name)\n<code>/setbackup off</code> to stop using it."
        )
    if args[0].lower() in {"off", "none", "remove"}:
        await db(storage.set_backup_chat, None)
        return await message.answer("Backup channel unset. Copies already in it stay there.")
    chat = await resolve_chat(args[0])
    if chat is None or chat.type not in {"channel", "supergroup", "group"}:
        return await message.answer("I cannot access that chat. Add the bot as admin there first.")
    if storage.is_source(chat.id) or any(int(c["chat_id"]) == chat.id for c in await db(storage.list_channels)):
        return await message.answer("The backup channel must be a separate channel, not a source or destination.")
    if await can_post_in(chat.id) is not True:
        return await message.answer("The bot cannot post there. Make it admin with 'Post messages'.")
    await db(storage.set_backup_chat, chat.id)
    done, total = await db(storage.backup_counts, chat.id)
    await message.answer(
        f"Backup channel set: <b>{escape(chat.title or str(chat.id))}</b> (<code>{chat.id}</code>).\n"
        f"New posts are copied there automatically. {total - done} existing post(s) are not backed up yet — send /backup."
    )


@dp.message(Command("backup"))
async def backup_cmd(message: Message):
    if not is_admin(message):
        return await deny(message)
    if message.chat.type != "private":
        return await message.answer("Please use /backup in a private chat with the bot.")
    backup_chat = await db(storage.get_backup_chat)
    if not backup_chat:
        return await message.answer("Set a backup channel first: /setbackup")
    if job_running():
        return await message.answer("Another job is running. See /job.")
    if await can_post_in(backup_chat) is not True:
        return await message.answer("The bot cannot post in the backup channel. Check it is admin with 'Post messages'.")
    start_job(run_backup_job(message.chat.id, backup_chat))


async def build_snapshot():
    def make():
        data = storage.export_snapshot()
        return json.dumps(data, ensure_ascii=False).encode("utf-8"), data
    return await asyncio.to_thread(make)


async def send_snapshot(chat_id: int, auto: bool = False):
    blob, data = await build_snapshot()
    if len(blob) > 45 * 1024 * 1024:
        return await tell(chat_id, "The backup file would be larger than Telegram allows (50 MB).")
    name = f"scheduler-backup-{datetime.now(TZ):%Y%m%d-%H%M}.json"
    caption = (
        f"{'Automatic ' if auto else ''}backup: {len(data['posts'])} posts, {len(data['sources'])} sources, "
        f"{len(data['destinations'])} destinations.\nKeep this file. To rebuild the bot: send it to the new bot, "
        "reply to it with /import_backup."
    )
    try:
        await bot.send_document(chat_id, BufferedInputFile(blob, name), caption=caption)
    except TelegramAPIError:
        logging.warning("Could not send the backup file to %s", chat_id)


@dp.message(Command("export_backup"))
async def export_backup_cmd(message: Message):
    if not is_admin(message):
        return await deny(message)
    if message.chat.type != "private":
        return await message.answer("Please use /export_backup in a private chat with the bot.")
    await send_snapshot(message.chat.id)


async def maybe_auto_snapshot():
    if AUTO_SNAPSHOT_HOURS <= 0:
        return
    last = await db(storage.get_setting, "snapshot_at")
    if last and last > (datetime.now(timezone.utc) - timedelta(hours=AUTO_SNAPSHOT_HOURS)).isoformat():
        return
    await db(storage.set_setting, "snapshot_at", datetime.now(timezone.utc).isoformat())
    if not await db(storage.library_size) and not await db(storage.list_channels):
        return
    for owner in ADMIN_IDS:
        await send_snapshot(owner, auto=True)


@dp.message(Command("import_backup"))
async def import_backup_cmd(message: Message):
    if not is_admin(message):
        return await deny(message)
    if not is_owner(message):
        return await message.answer("Only the owner (ADMIN_IDS) can restore a backup file.")
    document = message.document or (message.reply_to_message.document if message.reply_to_message else None)
    if not document:
        return await message.answer("Send the backup file to me, then reply to it with /import_backup.")
    if (document.file_size or 0) > 19 * 1024 * 1024:
        return await message.answer("That file is too big for a bot to download (20 MB limit).")
    try:
        raw = (await bot.download(document)).read()
        data = json.loads(raw.decode("utf-8"))
    except Exception:
        return await message.answer("I could not read that file. Send the original file made by /export_backup.")
    try:
        summary = await db(storage.import_snapshot, data)
    except ValueError as exc:
        return await message.answer(str(exc))

    restored, rejoin = await restore_destinations(summary["destinations"])
    lines = [
        "<b>Backup restored</b>",
        f"Posts: {summary['posts_added']} added, {summary['posts_existing']} already there",
        f"Sources: {summary['sources']} · extra admins: {summary['admins']} · footer/button/time settings: {summary['settings']}",
        f"Destinations re-added: {len(restored)}",
    ]
    if rejoin:
        lines.append("Add the bot again (admin) to: " + ", ".join(escape(t) for t in rejoin))
    sources = await db(storage.list_sources)
    lines.append(
        "Make the bot admin in your source channels so new posts are collected"
        + (": " + ", ".join(escape(s["title"]) for s in sources) if sources else ".")
    )
    if await db(storage.get_backup_chat):
        lines.append("Backup channel is set. If an original post is gone, its backup copy is used automatically.")
    await message.answer("\n".join(lines))


async def prepare_restore(token: str, first, last):
    """-> (backup_chat, targets, items, error). first/last select message IDs in the backup channel."""
    backup_chat = await db(storage.get_backup_chat)
    if not backup_chat:
        return None, None, None, "No backup channel set. Use /setbackup -100… first (the channel that holds your copies)."
    if token == "all":
        targets = [{"chat_id": int(c["chat_id"]), "title": c["title"], "thread_id": c["thread_id"]}
                   for c in await db(storage.list_channels)]
        if not targets:
            return None, None, None, "No destinations registered."
    else:
        chat = await resolve_chat(token)
        if chat is None or chat.type not in DEST_TYPES:
            return None, None, None, "I cannot access that channel/group. Add the bot there as admin first."
        if chat.id == backup_chat:
            return None, None, None, "That is the backup channel itself."
        if await can_post_in(chat.id) is not True:
            return None, None, None, "The bot cannot post there. Make it admin with 'Post messages'."
        targets = [{"chat_id": chat.id, "title": chat.title or str(chat.id), "thread_id": None}]

    known = await db(storage.restore_items, backup_chat)
    if first:
        if last is None and known:
            last = known[-1][0]
        if last is None:
            return None, None, None, "Give the last ID too: /restore_posts TARGET FIRST LAST"
        if last < first:
            return None, None, None, "FIRST must not be bigger than LAST."
        if last - first + 1 > MAX_RESTORE_RANGE:
            return None, None, None, f"Too many at once (max {MAX_RESTORE_RANGE}). Split the range."
        markups = {mid: markup for mid, markup in known}
        snapshot_msg = await db(storage.get_setting, "snapshot_msg")
        items = [(mid, markups.get(mid)) for mid in range(first, last + 1) if str(mid) != str(snapshot_msg)]
    else:
        items = known
        if not items:
            return None, None, None, (
                "The library has no backup copies. Restore by range instead: "
                "/restore_posts TARGET FIRST LAST  (message IDs inside the backup channel)."
            )
    return backup_chat, targets, items, None


@dp.message(Command("restore_posts"))
async def restore_posts_cmd(message: Message):
    """Post all backed-up posts into a channel/group (oldest first) - e.g. a new channel after a disaster."""
    if not is_admin(message):
        return await deny(message)
    if message.chat.type != "private":
        return await message.answer("Please use /restore_posts in a private chat with the bot.")
    args = command_args(message).split()
    if not args or len(args) > 3 or not all(a.isdigit() for a in args[1:]):
        return await message.answer(
            "<b>Restore posts</b>\n"
            "<code>/restore_posts @channel</code> — every backed-up post, oldest first\n"
            "<code>/restore_posts @channel FIRST LAST</code> — only message IDs FIRST..LAST of the backup channel\n"
            "<code>/restore_posts all</code> — into every registered destination\n\n"
            "Works even after a fresh install: run /setbackup with your backup channel, then use the range form."
        )
    if job_running():
        return await message.answer("Another job is running. See /job.")
    first = int(args[1]) if len(args) > 1 else None
    last = int(args[2]) if len(args) > 2 else None
    backup_chat, targets, items, error = await prepare_restore(args[0].lower(), first, last)
    if error:
        return await message.answer(error)
    token = "all" if args[0].lower() == "all" else str(targets[0]["chat_id"])
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Yes, restore", callback_data=f"rsy:{token}:{first or 0}:{last or 0}"),
        InlineKeyboardButton(text="Cancel", callback_data="rsn"),
    ]])
    where = "every destination" if token == "all" else f"<b>{escape(targets[0]['title'])}</b>"
    await message.answer(
        f"Post <b>{len(items)}</b> backed-up post(s) into {where}"
        + (f" ({len(targets)} destinations)" if token == "all" else "")
        + f"? That takes {eta_text(len(items) * len(targets))}. They are plain copies (no footer/button), oldest first.",
        reply_markup=keyboard,
    )


@dp.callback_query(F.data.startswith("rs"))
async def restore_buttons(query: CallbackQuery):
    if query.from_user.id not in all_admin_ids():
        return await query.answer("Not authorized", show_alert=True)
    parts = (query.data or "").split(":")
    if parts[0] == "rsn":
        await query.message.edit_text("Cancelled.")
        return await query.answer()
    if parts[0] != "rsy" or len(parts) != 4:
        return await query.answer()
    if job_running():
        return await query.answer("Another job is running. See /job.", show_alert=True)
    try:
        first, last = int(parts[2]) or None, int(parts[3]) or None
    except ValueError:
        return await query.answer()
    backup_chat, targets, items, error = await prepare_restore(parts[1], first, last)
    if error:
        await query.message.edit_text(error)
        return await query.answer()
    await query.message.edit_text("Restore is starting… you will get progress messages. /job · /stopjob")
    start_job(run_restore_job(query.from_user.id, backup_chat, targets, items))
    await query.answer()


# ------------------------------------------------------- automatic detection after a reset

async def restore_destinations(destinations: list):
    """Re-register destinations from a snapshot where the bot can still post. Returns (restored, need_rejoin)."""
    restored, rejoin = [], []
    for d in destinations:
        chat_id = int(d["chat_id"])
        if storage.is_source(chat_id):
            continue
        if await can_post_in(chat_id):
            await db(storage.add_channel, chat_id, d["title"], int(d["interval_minutes"]), d.get("kind") or "channel", d.get("thread_id"))
            if d.get("batch_size"):
                await db(storage.set_batch_size, chat_id, int(d["batch_size"]))
            if d.get("paused"):
                await db(storage.set_paused, chat_id, True)
            restored.append(d["title"])
        else:
            rejoin.append(d["title"])
    return restored, rejoin


def snapshot_document(message):
    document = getattr(message, "document", None) if message else None
    if document and (document.file_name or "").startswith("scheduler-backup"):
        return document
    return None


async def pinned_snapshot_message(backup_chat: int):
    """The pinned snapshot message of the backup channel, or None."""
    try:
        chat = await bot.get_chat(backup_chat)
    except TelegramAPIError:
        return None
    pinned = getattr(chat, "pinned_message", None)
    return pinned if snapshot_document(pinned) else None


async def read_pinned_snapshot(backup_chat: int):
    """-> (message_id, data) from the snapshot pinned in the backup channel, or None."""
    pinned = await pinned_snapshot_message(backup_chat)
    if pinned is None:
        return None
    document = snapshot_document(pinned)
    if (document.file_size or 0) > 19 * 1024 * 1024:
        logging.warning("The pinned snapshot is too big for a bot to download")
        return None
    try:
        raw = (await bot.download(document)).read()
        return pinned.message_id, json.loads(raw.decode("utf-8"))
    except (TelegramAPIError, ValueError):
        logging.exception("Could not read the pinned snapshot")
        return None


async def auto_detect(extra_ids=(), force_snapshot: bool = False) -> list:
    """Find everything the bot already belongs to, without anyone removing and re-adding it:
    1) backup channel from BACKUP_CHANNEL_ID; 2) on a fresh install, the snapshot pinned in that channel
    (sources, destinations, settings, posts); 3) chats listed in DESTINATION_CHAT_IDS (+ extra_ids).
    Returns report lines (empty when nothing happened)."""
    lines = []
    if BACKUP_CHANNEL_ID and not await db(storage.get_backup_chat):
        if await can_post_in(BACKUP_CHANNEL_ID):
            await db(storage.set_backup_chat, BACKUP_CHANNEL_ID)
            lines.append("• Backup channel set from BACKUP_CHANNEL_ID")
        else:
            lines.append(f"⚠️ BACKUP_CHANNEL_ID <code>{BACKUP_CHANNEL_ID}</code>: the bot cannot post there")
    backup_chat = await db(storage.get_backup_chat)

    fresh = not await db(storage.library_size) and not await db(storage.list_channels)
    if backup_chat and (fresh or (force_snapshot and not await db(storage.get_setting, "snapshot_msg"))):
        found = await read_pinned_snapshot(backup_chat)
        if found:
            message_id, data = found
            try:
                summary = await db(storage.import_snapshot, data)
            except ValueError:
                summary = None
            if summary:
                await db(storage.set_setting, "snapshot_msg", message_id)
                restored, rejoin = await restore_destinations(summary["destinations"])
                lines.append(
                    f"• Snapshot restored: {summary['posts_added']} post(s), {summary['sources']} source(s), "
                    f"{len(restored)} destination(s) (no re-adding needed)"
                )
                if rejoin:
                    lines.append("⚠️ Bot is not admin any more in: " + ", ".join(escape(t) for t in rejoin))
        elif fresh:
            lines.append("• No snapshot found in the backup channel (nothing to restore yet)")

    registered = {int(c["chat_id"]) for c in await db(storage.list_channels)}
    for chat_id in sorted(DESTINATION_IDS | set(extra_ids)):
        if chat_id in registered or storage.is_source(chat_id) or chat_id == backup_chat:
            continue
        if await can_post_in(chat_id):
            try:
                chat = await bot.get_chat(chat_id)
                title, kind = chat.title or str(chat_id), chat.type
            except TelegramAPIError:
                title, kind = str(chat_id), "channel"
            await db(storage.add_channel, chat_id, title, DEFAULT_INTERVAL_MINUTES, kind, None)
            lines.append(f"• Found and added: {icon(kind)} {escape(title)} (<code>{chat_id}</code>)")
        else:
            lines.append(f"⚠️ <code>{chat_id}</code>: the bot cannot post there (not admin / removed)")
    return lines


@dp.message(Command("detect"))
async def detect_cmd(message: Message):
    """Look for channels/groups where the bot is already admin. Optional: extra chat IDs to check."""
    if not is_admin(message):
        return await deny(message)
    extra = [int(t) for t in command_args(message).replace(",", " ").split() if t.lstrip("-").isdigit()]
    lines = await auto_detect(extra, force_snapshot=True)
    if not lines:
        lines = ["Nothing new found."]
    await message.answer(
        "🔎 <b>Detect</b>\n" + "\n".join(lines)
        + "\n\nThe bot cannot list its own chats. A channel/group that is missing: put its ID in "
          "DESTINATION_CHAT_IDS (see /railway_vars), run <code>/detect -100…</code>, or just post something there "
          "and I will ask you what to do."
    )


@dp.message(Command("railway_vars"))
async def railway_vars_cmd(message: Message):
    if not is_admin(message):
        return await deny(message)
    destinations = await db(storage.list_channels)
    sources = sorted(storage.source_ids())
    backup_chat = await db(storage.get_backup_chat)
    lines = []
    if backup_chat:
        lines.append(f"BACKUP_CHANNEL_ID={backup_chat}")
    lines.append("DESTINATION_CHAT_IDS=" + ",".join(str(c["chat_id"]) for c in destinations))
    if sources:
        lines.append("SOURCE_CHANNEL_IDS=" + ",".join(str(s) for s in sources))
    await message.answer(
        "<b>Paste these into Railway → Variables</b> once. After any reset or redeploy the bot then finds "
        "these chats by itself (it must still be admin there).\n\n<code>" + "\n".join(lines) + "</code>"
        + ("" if backup_chat else "\n\nNo backup channel yet: /setbackup is strongly recommended.")
    )


# ---- keep a fresh snapshot pinned in the backup channel (this is what makes recovery automatic)

SNAPSHOT_CHECK_SECONDS = 600
_snapshot_state = {"checked": 0.0, "warned": False}


async def push_snapshot(backup_chat: int, message_id) -> bool:
    blob, data = await build_snapshot()
    if len(blob) > 45 * 1024 * 1024:
        return False
    name = "scheduler-backup-latest.json"
    caption = (f"Automatic snapshot · {datetime.now(TZ):%d %b %Y %H:%M} · {len(data['posts'])} posts · "
               f"{len(data['destinations'])} destinations. Do not delete or unpin: the bot restores itself from this file.")
    if message_id:
        try:
            await bot.edit_message_media(
                media=InputMediaDocument(media=BufferedInputFile(blob, name), caption=caption),
                chat_id=backup_chat, message_id=int(message_id),
            )
            return True
        except TelegramBadRequest as exc:
            if "not modified" in str(exc).lower():
                return True
            logging.info("Old snapshot message cannot be edited (%s); sending a new one", exc)
    sent = await bot.send_document(backup_chat, BufferedInputFile(blob, name), caption=caption, disable_notification=True)
    await db(storage.set_setting, "snapshot_msg", sent.message_id)
    try:
        await bot.pin_chat_message(backup_chat, sent.message_id, disable_notification=True)
    except TelegramAPIError:
        await notify_admins(
            "⚠️ Give the bot the <b>'Pin messages'</b> permission in the backup channel. Without it the bot cannot "
            "find its snapshot after a reset (recovery would not be automatic)."
        )
    return True


async def maybe_push_snapshot():
    if time.time() - _snapshot_state["checked"] < SNAPSHOT_CHECK_SECONDS:
        return
    _snapshot_state["checked"] = time.time()
    backup_chat = await db(storage.get_backup_chat)
    if not backup_chat:
        return
    if not await db(storage.library_size) and not await db(storage.list_channels):
        return  # never overwrite a good snapshot with an empty state
    fingerprint = await db(storage.state_fingerprint)
    if fingerprint == await db(storage.get_setting, "snapshot_fp"):
        return
    message_id = await db(storage.get_setting, "snapshot_msg")
    if not message_id and await pinned_snapshot_message(backup_chat) is not None:
        # There is a snapshot in the channel that this bot has not loaded: never overwrite it.
        if not _snapshot_state["warned"]:
            _snapshot_state["warned"] = True
            await notify_admins(
                "⚠️ The backup channel already holds a snapshot that I have not loaded, so I will not overwrite it. "
                "Run /detect to restore from it."
            )
        return
    try:
        if await push_snapshot(backup_chat, message_id):
            await db(storage.set_setting, "snapshot_fp", fingerprint)
    except TelegramAPIError:
        logging.exception("Could not update the snapshot in the backup channel")


# ---- a chat the bot belongs to but does not know yet

PROMPTED: set = set()


async def offer_unknown_chat(chat):
    """Activity in a channel/group where the bot is a member but which is not set up: ask the admins once."""
    cid = chat.id
    if cid in PROMPTED or chat.type not in DEST_TYPES or storage.is_source(cid):
        return
    PROMPTED.add(cid)
    if cid == await db(storage.get_backup_chat) or await db(storage.get_setting, f"ignored:{cid}"):
        return
    if any(int(c["chat_id"]) == cid for c in await db(storage.list_channels)) or not all_admin_ids():
        return
    title = chat.title or str(cid)
    rows = [[InlineKeyboardButton(text="➕ Add as destination (posts go here)", callback_data=f"adc:{cid}")]]
    if chat.type == "channel":
        rows.append([InlineKeyboardButton(text="📥 Add as source (collect posts from it)", callback_data=f"ads:{cid}")])
    rows.append([InlineKeyboardButton(text="🚫 Ignore", callback_data=f"adi:{cid}")])
    for admin_id in all_admin_ids():
        try:
            await bot.send_message(
                admin_id,
                f"📡 I am in <b>{escape(title)}</b> (<code>{cid}</code>, {chat.type}) but it is not set up yet. "
                "What should I do with it?",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
            )
        except TelegramAPIError:
            pass


@dp.callback_query(F.data.startswith("ad"))
async def unknown_chat_buttons(query: CallbackQuery):
    if query.from_user.id not in all_admin_ids():
        return await query.answer("Not authorized", show_alert=True)
    action, _, raw = (query.data or "").partition(":")
    try:
        cid = int(raw)
    except ValueError:
        return await query.answer()
    if action == "adi":
        await db(storage.set_setting, f"ignored:{cid}", "1")
        await query.message.edit_text("Ignored. I will not ask about this chat again.")
        return await query.answer()
    chat = await resolve_chat(str(cid))
    if chat is None or chat.type not in DEST_TYPES:
        await query.message.edit_text("I cannot access that chat any more.")
        return await query.answer()
    title = chat.title or str(cid)
    if action == "adc":
        if await can_post_in(cid) is not True:
            await query.message.edit_text("I cannot post there. For a channel, make me admin with 'Post messages'.")
        else:
            await db(storage.add_channel, cid, title, DEFAULT_INTERVAL_MINUTES, chat.type, None)
            await query.message.edit_text(
                f"Added <b>{escape(title)}</b> as a destination: up to {BATCH_SIZE} posts, then every "
                f"{DEFAULT_INTERVAL_MINUTES} minutes."
            )
    elif action == "ads" and chat.type == "channel":
        await db(storage.add_source, cid, title)
        await query.message.edit_text(f"Added <b>{escape(title)}</b> as a source. New posts there are collected.")
    await query.answer()


# ------------------------------------------------------- footer, button, fixed times

MAX_FOOTER = 300
MAX_BUTTONS = 3


def _branding_setter(kind: str, command: str, limit: int, label: str):
    @dp.message(Command(command))
    async def handler(message: Message):
        if not is_admin(message):
            return await deny(message)
        head, sep, body = command_args(message).partition("|")
        body = body.strip()
        try:
            target = parse_target(head) if head.strip() else None
        except ValueError:
            target = None
        if target is None and message.chat.type in GROUP_TYPES and not sep:
            target, body = message.chat.id, head.strip()
        if target is None or not body:
            return await message.answer(
                f"Format: <code>/{command} all | your text</code>\n"
                f"or <code>/{command} CHAT_ID | your text</code>  (use <code>-</code> as text to switch it off there)."
            )
        if len(body) > limit:
            return await message.answer(f"Too long: {len(body)} characters, maximum {limit}.")
        await db(storage.set_branding, kind, target, body)
        where = "every destination" if target == "all" else f"<code>{target}</code>"
        shown = "switched off" if body == "-" else f"set to:\n{escape(body)}"
        await message.answer(f"{label} for {where} {shown}")
    return handler


def _branding_clearer(kind: str, command: str, label: str, extra: str = ""):
    @dp.message(Command(command))
    async def handler(message: Message):
        if not is_admin(message):
            return await deny(message)
        try:
            target = target_from(message, command_args(message))
        except ValueError:
            return await message.answer(f"Format: /{command} all   or   /{command} CHAT_ID")
        await db(storage.clear_branding, kind, target)
        await message.answer(f"{label} cleared for {'every destination' if target == 'all' else target}.{extra}")
    return handler


_branding_setter("footer", "setfooter", MAX_FOOTER, "Footer")
_branding_clearer("footer", "clearfooter", "Footer")
_branding_clearer("button", "clearbutton", "Button")
_branding_clearer("times", "cleartimes", "Fixed times", " Destinations without their own setting go back to the interval.")


# ---- button under the last post of each batch

def parse_buttons(parts: list) -> list:
    """['Join', 'https://t.me/x', 'More', 't.me/y'] -> [['Join', url], ['More', url]]; ValueError when invalid."""
    if not parts or len(parts) % 2 or len(parts) > MAX_BUTTONS * 2:
        raise ValueError
    pairs = []
    for i in range(0, len(parts), 2):
        text, url = parts[i].strip(), parts[i + 1].strip()
        if not text or len(text) > 60:
            raise ValueError
        if url.startswith("@"):
            url = "https://t.me/" + url[1:]
        elif url.lower().startswith(("t.me/", "telegram.me/")):
            url = "https://" + url
        if not re.match(r"^(https?://|tg://)\S+$", url):
            raise ValueError
        pairs.append([text, url])
    return pairs


@dp.message(Command("setbutton"))
async def set_button_cmd(message: Message):
    if not is_admin(message):
        return await deny(message)
    parts = [p.strip() for p in command_args(message).split("|")]
    target, rest = None, parts
    try:
        target, rest = parse_target(parts[0]), parts[1:]
    except ValueError:
        if message.chat.type in GROUP_TYPES:
            target = message.chat.id
    usage = (
        "Format: <code>/setbutton all | Join Channel | https://t.me/yourchannel</code>\n"
        "One destination: <code>/setbutton CHAT_ID | Text | link</code> (in a group: <code>/setbutton Text | link</code>)\n"
        "Up to 3 buttons: <code>/setbutton all | Text 1 | link 1 | Text 2 | link 2</code>\n"
        "Switch off for one destination: <code>/setbutton CHAT_ID | -</code>\n\n"
        "The button is added under the <b>last post of every batch</b>."
    )
    if target is None:
        return await message.answer(usage)
    if rest == ["-"]:
        value = "-"
    else:
        try:
            value = json.dumps(parse_buttons(rest), ensure_ascii=False)
        except ValueError:
            return await message.answer("Could not read the button text/link.\n\n" + usage)
    await db(storage.set_branding, "button", target, value)
    where = "every destination" if target == "all" else f"<code>{target}</code>"
    await message.answer(
        f"Button for {where} switched off." if value == "-"
        else f"Button for {where} saved. It appears under the last post of each batch (from the next batch)."
    )


def button_pairs(raw):
    if not raw:
        return []
    try:
        return [(str(t), str(u)) for t, u in json.loads(raw)]
    except (ValueError, TypeError):
        return []


async def add_button_to_last(chat_id: int, message_id: int, post, pairs: list):
    """Put the 'Join Channel' style button under the last message of a batch (keeps the post's own buttons)."""
    rows = []
    existing = post_markup(post)
    if existing:
        rows = [list(r) for r in existing.inline_keyboard]
    rows += [[InlineKeyboardButton(text=t, url=u)] for t, u in pairs]
    markup = InlineKeyboardMarkup(inline_keyboard=rows)
    for _ in range(2):
        try:
            await bot.edit_message_reply_markup(chat_id=chat_id, message_id=message_id, reply_markup=markup)
            return
        except TelegramRetryAfter as exc:
            await asyncio.sleep(int(exc.retry_after) + 1)
        except TelegramAPIError as exc:
            logging.warning("Could not add the button in %s: %s", chat_id, exc)
            return


# ---- fixed posting times

TIME_RE = re.compile(r"^(\d{1,2})(?::(\d{2}))?$")


def parse_times(raw: str) -> list:
    """'9:00, 14:00 21' -> ['09:00', '14:00', '21:00'] (sorted, no duplicates); ValueError when invalid."""
    items = [p for p in re.split(r"[,\s;]+", raw.strip()) if p]
    if not items or len(items) > 24:
        raise ValueError
    found = set()
    for item in items:
        m = TIME_RE.match(item)
        if not m:
            raise ValueError
        hour, minute = int(m.group(1)), int(m.group(2) or 0)
        if hour > 23 or minute > 59:
            raise ValueError
        found.add(f"{hour:02d}:{minute:02d}")
    return sorted(found)


def next_fixed_run(times: str) -> str:
    """UTC ISO time of the next fixed slot (local time zone) after now."""
    now_local = datetime.now(TZ)
    for day in (0, 1):
        for slot in times.split(","):
            hour, minute = (int(x) for x in slot.split(":"))
            candidate = (now_local + timedelta(days=day)).replace(hour=hour, minute=minute, second=0, microsecond=0)
            if candidate > now_local:
                return candidate.astimezone(timezone.utc).isoformat()
    return utc_in(hours=24)


async def reschedule_fixed(target):
    """After /settimes: every affected destination waits for its next fixed slot."""
    for c in await db(storage.list_channels):
        chat_id = int(c["chat_id"])
        if target != "all" and chat_id != target:
            continue
        times = await db(storage.get_branding, "times", chat_id)
        if times:
            await db(storage.postpone_channel, chat_id, next_fixed_run(times))


@dp.message(Command("settimes"))
async def set_times_cmd(message: Message):
    if not is_admin(message):
        return await deny(message)
    head, sep, body = command_args(message).partition("|")
    body = body.strip()
    try:
        target = parse_target(head) if head.strip() else None
    except ValueError:
        target = None
    if target is None and message.chat.type in GROUP_TYPES and not sep:
        target, body = message.chat.id, head.strip()
    usage = (
        "Format: <code>/settimes all | 09:00,14:00,21:00</code>\n"
        "One destination: <code>/settimes CHAT_ID | 09:00,21:00</code> (in a group: <code>/settimes 09:00,21:00</code>)\n"
        "Back to the interval for one destination: <code>/settimes CHAT_ID | -</code>\n"
        f"Times are in {TIMEZONE}. At every time one batch is sent (see /setbatch)."
    )
    if target is None or not body:
        return await message.answer(usage)
    if body == "-":
        value = "-"
    else:
        try:
            value = ",".join(parse_times(body))
        except ValueError:
            return await message.answer("Could not read those times. Use 24-hour HH:MM.\n\n" + usage)
    await db(storage.set_branding, "times", target, value)
    where = "every destination" if target == "all" else f"<code>{target}</code>"
    if value == "-":
        return await message.answer(f"Fixed times switched off for {where}: it uses the interval.")
    await reschedule_fixed(target)
    await message.answer(
        f"Fixed times for {where}: <b>{value.replace(',', ', ')}</b> ({TIMEZONE}).\n"
        f"Next batch: {fmt_local(next_fixed_run(value))}. During quiet hours ({quiet_label()}) a batch waits until they end."
    )


@dp.message(Command("branding"))
async def branding_cmd(message: Message):
    if not is_admin(message):
        return await deny(message)
    rows = await db(storage.list_branding)
    if not rows:
        return await message.answer("Nothing set. Use /setfooter, /setbutton and /settimes.")
    lines = []
    for r in rows:
        kind, _, target = r["key"].partition(":")
        where = "all destinations" if target == "all" else target
        if kind == "button":
            shown = "off" if r["value"] == "-" else " · ".join(f"{escape(t)} → {escape(u)}" for t, u in button_pairs(r["value"]))
        elif kind == "times":
            shown = "off (interval)" if r["value"] == "-" else r["value"].replace(",", ", ")
        else:
            shown = escape(r["value"])
        lines.append(f"<b>{kind}</b> ({where}): {shown}")
    await message.answer(
        "\n".join(lines)
        + "\n\nFooter works on text posts and on photo/video/file captions. The button goes under the last post "
          "of each batch. A destination's own setting beats the 'all' one."
    )


# ------------------------------------------------------- admin management (owner only)

@dp.message(Command("addadmin"))
async def add_admin_cmd(message: Message):
    if not is_admin(message):
        return await deny(message)
    if not is_owner(message):
        return await message.answer("Only the owner (ADMIN_IDS) can add admins.")
    raw = command_args(message)
    user_id = None
    if raw.isdigit():
        user_id = int(raw)
    elif message.reply_to_message and message.reply_to_message.from_user and not raw:
        user_id = message.reply_to_message.from_user.id
    if not user_id:
        return await message.answer(
            "Format: <code>/addadmin USER_ID</code>, or reply to the person's message with /addadmin.\n"
            "Anyone can find their own ID by sending /start to this bot."
        )
    if user_id in ADMIN_IDS or user_id in storage.extra_admin_ids():
        return await message.answer("That user is already an admin.")
    await db(storage.add_admin, user_id, message.from_user.id)
    await set_admin_menu(user_id)
    await message.answer(
        f"Admin added: <code>{user_id}</code>. They must open this bot and send /start once to receive alerts."
    )


@dp.message(Command("removeadmin"))
async def remove_admin_cmd(message: Message):
    if not is_admin(message):
        return await deny(message)
    if not is_owner(message):
        return await message.answer("Only the owner (ADMIN_IDS) can remove admins.")
    raw = command_args(message)
    if not raw.isdigit():
        return await message.answer("Format: /removeadmin USER_ID   (see /admins)")
    if int(raw) in ADMIN_IDS:
        return await message.answer("Owners are set in the ADMIN_IDS variable and cannot be removed here.")
    if await db(storage.remove_admin, int(raw)):
        await clear_admin_menu(int(raw))
        await message.answer(f"Admin removed: <code>{raw}</code>.")
    else:
        await message.answer("That user is not an added admin.")


@dp.message(Command("admins"))
async def admins_cmd(message: Message):
    if not is_admin(message):
        return await deny(message)
    owners = "\n".join(f"👑 <code>{i}</code>" for i in sorted(ADMIN_IDS)) or "none"
    extra = "\n".join(f"🛠 <code>{i}</code>" for i in sorted(storage.extra_admin_ids())) or "none"
    await message.answer(f"<b>Owners</b>\n{owners}\n\n<b>Added admins</b>\n{extra}")


# ------------------------------------------------------- collecting posts

@dp.channel_post()
async def capture_source_channel_post(message: Message):
    if not storage.is_source(message.chat.id):
        await offer_unknown_chat(message.chat)  # a channel where the bot is admin but which is not set up
        return

    content = (message.text or message.caption or "").strip()
    entities = message.entities or message.caption_entities or []
    command_entity = any(
        e.type == "bot_command" and content[e.offset:e.offset + e.length].split("@")[0] == "/broadcast"
        for e in entities
    )
    command_text = content.split(maxsplit=1)[0].split("@")[0] if content else ""
    if command_text == "/broadcast" or command_entity:
        await direct_broadcast(message)
        return

    qid = await db(queue_post, message)
    if qid:
        logging.info("Queued post %s from source %s", message.message_id, message.chat.id)
        await maybe_backup_live(qid)
    else:
        logging.info("Source post %s already saved; ignored.", message.message_id)


@dp.message(Command("broadcast"), F.chat.type == "private")
async def broadcast_private_cmd(message: Message):
    if not is_admin(message):
        return await deny(message)
    await direct_broadcast(message)


@dp.message(F.chat.type == "private")
async def capture_admin_direct_post(message: Message):
    if not is_admin(message) or (message.text and message.text.startswith("/")):
        return
    document = message.document
    if document and (document.file_name or "").startswith("scheduler-backup"):
        return await message.reply(
            "Backup file received (not saved as a post). To restore from it, reply to this file with "
            "/import_backup  (owner only)."
        )
    qid = await db(queue_post, message)
    if qid:
        await maybe_backup_live(qid)
        await message.reply("Post saved to the library and queued for one destination.")
    else:
        await message.reply("This post was already saved earlier.")


# ----------------------------------------------------------------- sending

def load_entities(post, kind: str):
    try:
        payload = json.loads(post["entities_json"] or "{}")
    except ValueError:
        return None
    result = []
    for raw in payload.get(kind) or []:
        try:
            result.append(MessageEntity.model_validate(raw))
        except Exception:
            continue
    return result or None


async def deliver(chat_id: int, thread_id, post, footer) -> int:
    """Send one saved post and return the new message id. Plain copy by default; with a footer the post is
    rebuilt: text posts are re-sent with the footer, media gets a new caption."""
    markup = None
    if post["reply_markup_json"]:
        try:
            markup = InlineKeyboardMarkup.model_validate_json(post["reply_markup_json"])
        except Exception:
            logging.warning("Could not restore inline keyboard; sending without it")

    ctype = post["content_type"]
    text = post["caption"] or ""
    base = {"chat_id": chat_id}
    if thread_id:
        base["message_thread_id"] = thread_id

    # Text post + footer: a text message cannot be edited while copying, so send it again with the footer.
    if ctype == "text" and footer and text and len(text) + 2 + len(footer) <= 4096:
        sent = await bot.send_message(
            **base, text=f"{text}\n\n{footer}", entities=load_entities(post, "text"), parse_mode=None,
            reply_markup=markup,
        )
        return sent.message_id

    new_caption = None
    if ctype in CAPTION_TYPES:
        # Album items usually have no caption of their own: no footer there, so it is not repeated per photo.
        if footer and (text or not post["media_group_id"]):
            candidate = f"{text}\n\n{footer}" if text else footer
            if len(candidate) <= 1024:
                new_caption = candidate

    kwargs = {**base, "from_chat_id": post["source_chat_id"], "message_id": post["message_id"]}
    if markup:
        kwargs["reply_markup"] = markup
    if new_caption is not None:
        kwargs.update(caption=new_caption, caption_entities=load_entities(post, "caption"), parse_mode=None)
    sent = await bot.copy_message(**kwargs)
    return sent.message_id


# ---- failure alert: DM the admins when a destination keeps failing

FAIL_REMIND_HOURS = 6


async def record_failure(chat_id: int, title: str, reason: str):
    count = int(await db(storage.get_setting, f"fail:{chat_id}", 0) or 0) + 1
    await db(storage.set_setting, f"fail:{chat_id}", count)
    if count < FAIL_ALERT_AFTER:
        return
    last = await db(storage.get_setting, f"failalert:{chat_id}")
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=FAIL_REMIND_HOURS)).isoformat()
    if last and str(last) > cutoff:
        return
    await db(storage.set_setting, f"failalert:{chat_id}", datetime.now(timezone.utc).isoformat())
    await notify_admins(
        "⚠️ <b>Posting keeps failing</b>\n\n"
        f"<b>Chat:</b> {escape(title)}\n"
        f"<b>ID:</b> <code>{chat_id}</code>\n"
        f"<b>Failed in a row:</b> {count}\n"
        f"<b>Last error:</b> {escape(reason[:300])}\n\n"
        "Check that the bot is still admin there with 'Post messages'. It keeps trying; "
        f"you get a reminder at most every {FAIL_REMIND_HOURS} hours."
    )


async def record_success(chat_id: int):
    if await db(storage.get_setting, f"fail:{chat_id}") is not None:
        await db(storage.delete_settings, f"fail:{chat_id}", f"failalert:{chat_id}")


async def run_alerts(chat_id: int, title: str):
    try:
        alerts = await db(storage.pop_alerts, chat_id, LOW_POST_THRESHOLD)
    except Exception:
        logging.exception("Could not check alerts for %s", chat_id)
        return
    for kind, value in alerts:
        if kind == "low":
            await notify_admins(
                f"⚠️ <b>Low posts</b> for {escape(title)} (<code>{chat_id}</code>)\n"
                f"Only <b>{value}</b> unused post(s) left before it starts repeating. "
                "Add more posts (forward them to me or post in a source channel)."
            )
        elif kind == "all_used":
            await notify_admins(
                f"🔁 <b>All posts used</b> on {escape(title)} (<code>{chat_id}</code>)\n"
                f"Every post has been sent once. It now repeats posts randomly (cycle #{value}). "
                "Add or import new posts for fresh content."
            )


async def process_channel(channel):
    chat_id = int(channel["chat_id"])
    if storage.is_source(chat_id):
        await db(storage.remove_channel, chat_id)
        return
    interval = int(channel["interval_minutes"])
    forced = int(channel["force_now"] or 0)
    batch = forced if forced > 0 else int(channel["batch_size"] or BATCH_SIZE)
    if forced:
        await db(storage.clear_force, chat_id)  # /sendnow handled: do it once
    thread_id = channel["thread_id"]
    footer = await db(storage.get_branding, "footer", chat_id)
    times = await db(storage.get_branding, "times", chat_id)
    buttons = button_pairs(await db(storage.get_branding, "button", chat_id))
    last_sent = None  # (message id, post) of the newest message of this batch
    ran_dry = False
    sent = attempts = 0
    max_attempts = batch * 3 + 50  # posts that no longer exist are skipped without counting as sent

    while sent < batch and attempts < max_attempts:
        attempts += 1
        post = await db(storage.get_next_post, chat_id)
        if not post:
            ran_dry = True
            break

        try:
            new_id = await deliver(chat_id, thread_id, post, footer)
        except TelegramRetryAfter as exc:
            wait = int(exc.retry_after) + 1
            logging.warning("Rate limited on %s; retrying in %ss", chat_id, wait)
            await db(storage.postpone_channel, chat_id, utc_in(seconds=wait))
            return
        except TelegramMigrateToChat as exc:
            await db(storage.migrate_channel, chat_id, int(exc.migrate_to_chat_id))
            logging.info("Group %s upgraded to %s; schedule moved", chat_id, exc.migrate_to_chat_id)
            return
        except (TelegramForbiddenError, TelegramBadRequest) as exc:
            text = str(exc).lower()
            if is_missing_message(text):
                if await db(storage.use_backup_copy, post["id"]):
                    logging.info("Original of post %s is gone; now using its backup copy", post["id"])
                    continue
                logging.info("Source post %s no longer exists / cannot be copied; removing it", post["id"])
                await db(storage.delete_post, post["id"])
                continue
            if "thread not found" in text or ("topic" in text and "closed" in text):
                await drop_channel(chat_id, channel["title"], "the target topic no longer exists or is closed")
                return
            if isinstance(exc, TelegramForbiddenError) or lost_access(exc):
                # "chat not found" / "bot is not a member" can mean the DESTINATION or the SOURCE of this post.
                verdict = await can_post_in(chat_id)
                if verdict is False:
                    await drop_channel(chat_id, channel["title"], "bot lost access or permission to post")
                    return
                if verdict is None:
                    await record_failure(chat_id, channel["title"], "Telegram could not be reached to check permissions")
                    await db(storage.postpone_channel, chat_id, utc_in(minutes=1))
                    return
                if await db(storage.use_backup_copy, post["id"]):
                    continue  # the source channel is unreachable, but the backup copy works
                await source_unreachable(post, chat_id)
                return
            logging.exception("Post %s rejected for %s; skipping it", post["id"], chat_id)
            await record_failure(chat_id, channel["title"], f"Telegram rejected a post: {exc}")
            await db(storage.finish_channel_post, chat_id, post["id"], False)
            continue
        except TelegramAPIError as exc:
            logging.exception("Send failed for %s; retrying in 1 minute", chat_id)
            await record_failure(chat_id, channel["title"], f"Send failed: {exc}")
            await db(storage.postpone_channel, chat_id, utc_in(minutes=1))
            return

        await db(storage.finish_channel_post, chat_id, post["id"])
        last_sent = (new_id, post)
        sent += 1
        logging.info("Sent post %s/%s to %s", sent, batch, chat_id)
        if sent < batch and POST_DELAY_SECONDS:
            await asyncio.sleep(POST_DELAY_SECONDS)

    if sent:
        await record_success(chat_id)
        if buttons and last_sent:
            await add_button_to_last(chat_id, last_sent[0], last_sent[1], buttons)
    if times and (sent or ran_dry):
        await db(storage.postpone_channel, chat_id, next_fixed_run(times))
        logging.info("Batch done for %s: %s post(s); next fixed time (%s)", chat_id, sent, times)
    elif sent:
        await db(storage.postpone_channel, chat_id, utc_in(minutes=interval))
        logging.info("Batch done for %s: %s post(s); next in %s min", chat_id, sent, interval)
    else:
        await db(storage.postpone_channel, chat_id, utc_in(seconds=60))
    await run_alerts(chat_id, channel["title"])


async def maybe_daily_report(now_local: datetime):
    if DAILY_REPORT_HOUR < 0 or now_local.hour != DAILY_REPORT_HOUR:
        return
    today = now_local.strftime("%Y-%m-%d")
    if await db(storage.get_setting, "report_date") == today:
        return
    await db(storage.set_setting, "report_date", today)
    text = await db(build_stats_text, f"Daily report {now_local.strftime('%d %b')}")
    await notify_admins(text)
    await db(storage.prune_send_log)


async def scheduler():
    while True:
        busy = False
        try:
            now_local = datetime.now(TZ)
            quiet = in_quiet_hours(now_local)
            channel = await db(storage.next_due_channel, datetime.now(timezone.utc).isoformat(), quiet)
            if channel:
                busy = True
                await process_channel(channel)
            await maybe_daily_report(now_local)
            await maybe_auto_snapshot()
            await maybe_push_snapshot()
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.exception("Scheduler error; continuing in 30 seconds")
            await asyncio.sleep(30)
        # Another destination may be due right now (several fixed times coincide): do not wait a full poll.
        await asyncio.sleep(1 if busy else POLL_SECONDS)


@dp.message(F.chat.type.in_(GROUP_TYPES))
async def unknown_group_activity(message: Message):
    """Last handler for groups: a message in a group that is not set up yet -> ask the admins once."""
    await offer_unknown_chat(message.chat)


async def main():
    await db(storage.init_db)
    await setup_command_menu()
    try:
        lines = await auto_detect()
        if lines:
            await notify_admins("🔎 <b>Auto-detect after start</b>\n" + "\n".join(lines))
    except Exception:
        logging.exception("Auto-detect failed")
    if not ADMIN_IDS:
        logging.warning("ADMIN_IDS is empty: nobody can use admin commands or auto-register chats.")
    if not storage.source_ids():
        logging.warning("No source channel yet: set SOURCE_CHANNEL_ID(S) or use /addsource.")
    logging.info(
        "Started: batch=%s, default interval=%s min, quiet hours=%s, sources=%s",
        BATCH_SIZE, DEFAULT_INTERVAL_MINUTES, quiet_label(), sorted(storage.source_ids()),
    )
    await asyncio.gather(dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types()), scheduler())


if __name__ == "__main__":
    asyncio.run(main())
