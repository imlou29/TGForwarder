"""Topic-scoped Telegram backup for Tegridy, independent from OpenClaw.

Requires Telethon 1.40.0. TOPIC_ROUTES is a JSON array, for example:
[{"source_chat":-1004350769620,"source_topic":501,
  "target_chat":-1003900751944,"target_topic":3962}]
Legacy SOURCE_ID and TARGET_ID are deliberately ignored.
"""
import asyncio
import json
import logging
import os
import sqlite3
import time
from pathlib import Path

from dotenv import load_dotenv
from telethon import TelegramClient, events, Button
from tegridy_controller import BackupJobs

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("tegridy-backup")


def routes():
    raw = os.environ.get("TOPIC_ROUTES")
    if not raw:
        raise ValueError("TOPIC_ROUTES is required; refusing to forward entire groups")
    data = json.loads(raw)
    if not isinstance(data, list) or not data:
        raise ValueError("TOPIC_ROUTES must be a nonempty JSON array")
    result = {}
    for r in data:
        source = (int(r["source_chat"]), int(r["source_topic"]))
        target = (int(r["target_chat"]), int(r["target_topic"]))
        if source in result:
            raise ValueError("Duplicate source topic route: %s" % (source,))
        if source[0] == target[0]:
            raise ValueError("Source and destination chats must differ")
        result[source] = target
    return result


def message_topic(message):
    """The forum root is reply_to_top_id, or reply_to_msg_id on first-level posts."""
    reply = getattr(message, "reply_to", None)
    if reply and getattr(reply, "forum_topic", False):
        return getattr(reply, "reply_to_top_id", None) or getattr(reply, "reply_to_msg_id", None)
    return None


def database():
    path = Path(os.environ.get("BACKUP_DB_PATH", "/data/tgforwarder/forwarded.sqlite3"))
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(path))
    db.execute("CREATE TABLE IF NOT EXISTS forwarded (source_chat INTEGER NOT NULL, source_msg INTEGER NOT NULL, target_chat INTEGER NOT NULL, target_topic INTEGER NOT NULL, target_msg INTEGER, PRIMARY KEY (source_chat,source_msg,target_chat,target_topic))")
    db.commit()
    return db


def already_sent(db, source_chat, source_msg, target_chat, target_topic):
    return db.execute("SELECT 1 FROM forwarded WHERE source_chat=? AND source_msg=? AND target_chat=? AND target_topic=?", (source_chat, source_msg, target_chat, target_topic)).fetchone() is not None


async def copy_message(client, message, target_chat, target_topic):
    """Copy content into the destination thread (Telegram will assign a new date)."""
    # Global invariant: ONLY Telegram photos with a nonempty written caption.
    # Applies to manual batches and any future live forwarding configuration.
    if message.action or not message.photo or not (message.message or "").strip():
        return None
    if message.media:
        # Telethon can send most media using a Telegram media reference. Some
        # restricted/unsupported media cannot be copied; log failure instead.
        sent = await client.send_file(target_chat, message.media, caption=message.message or "", formatting_entities=message.entities or None, reply_to=target_topic)
    elif message.message:
        sent = await client.send_message(target_chat, message.message, formatting_entities=message.entities or None, reply_to=target_topic, link_preview=False)
    else:
        return None
    return sent


async def forward_and_record(client, db, message, source_chat, target_chat, target_topic):
    if already_sent(db, source_chat, message.id, target_chat, target_topic):
        return False
    sent = await copy_message(client, message, target_chat, target_topic)
    if sent is None:
        return False
    sent_id = sent[0].id if isinstance(sent, list) else sent.id
    db.execute("INSERT OR IGNORE INTO forwarded VALUES (?,?,?,?,?)", (source_chat, message.id, target_chat, target_topic, sent_id))
    db.commit()
    return True


def controller_ids():
    """Fail closed: NO controller users until explicitly configured in Railway."""
    raw = os.environ.get("TG_CONTROLLER_IDS", "").strip()
    if not raw:
        return set()
    return {int(part.strip()) for part in raw.split(",") if part.strip()}


def controller_menu():
    return [
        [Button.inline("📊 Touchdowns status", b"tg:status")],
        [Button.inline("📋 Jobs", b"tg:jobs"), Button.inline("ℹ️ Help", b"tg:help")],
        [Button.inline("▶️ Start / Resume", b"tg:confirm"), Button.inline("🛑 Stop", b"tg:stop")],
    ]


def controller_status(mapping):
    """Read a fresh SQLite snapshot; do not mutate the batch or its checkpoint."""
    lines = ["📦 Tegridy Backup — Status", ""]
    path = Path(os.environ.get("BACKUP_DB_PATH", "/data/tgforwarder/forwarded.sqlite3"))
    if not path.exists():
        return "No backup database has been created yet."
    connection = sqlite3.connect("file:" + str(path) + "?mode=ro", uri=True, timeout=5)
    try:
        for source, target in mapping.items():
            count = connection.execute(
                "SELECT COUNT(*) FROM forwarded WHERE source_chat=? AND target_chat=? AND target_topic=?",
                (source[0], target[0], target[1]),
            ).fetchone()[0]
            state = connection.execute(
                "SELECT initial_cutoff, initial_complete, last_complete FROM batch_state WHERE source_chat=? AND source_topic=? AND target_chat=? AND target_topic=?",
                (*source, *target),
            ).fetchone()
            # 407 is the last historic dry-run estimate for Touchdowns only;
            # not a live enumeration and not a promise of total available posts.
            estimate = 407 if source == (-1004350769620, 501) and not (state and state[1]) else None
            lines.append("📍 Touchdowns" if source == (-1004350769620, 501) else "📍 Topic " + str(source[1]))
            lines.append("✅ Recorded copies: " + str(count))
            if estimate is not None:
                lines.append("📷 Historic preview estimate: " + str(estimate))
                lines.append("📋 Estimated remaining: ~" + str(max(0, estimate - count)))
                lines.append("📈 Estimated completion: " + f"{min(100.0, count / estimate * 100):.1f}%")
            lines.append("🏁 Initial batch: " + ("Complete" if state and state[1] else "Incomplete"))
            if state:
                lines.append("🔖 Frozen initial cutoff: " + str(state[0]))
                lines.append("📌 Last completed checkpoint: " + str(state[2]))
            lines.append("")
        lines.extend(["🛑 Automatic forwarding: " + ("Disabled" if os.environ.get("TG_DIAGNOSTIC_ONLY", "true").lower() != "false" else "Enabled"),
                      "⚠️ Active job / FloodWait countdown: not yet instrumented.",
                      "ℹ️ Counts are saved receipts, not verified destination content."])
        return "\n".join(lines)
    finally:
        connection.close()


def controller_help():
    return ("ℹ️ Tegridy Backup Controller\\n\\n"
            "Use /start, /status or the private buttons.\\n"
            "Start / Resume requires a second explicit confirmation and only runs manually.\\n"
            "Stop sends an interrupt; an in-flight Telegram request may finish.\\n"
            "Only photos containing a written caption are eligible.\\n"
            "Telegram group Restrict Saving Content must be disabled by an admin during backup.\\n"
            "Automatic forwarding remains disabled.")


async def run_live():
    mapping = routes()
    diagnostic_only = os.environ.get("TG_DIAGNOSTIC_ONLY", "true").strip().lower() != "false"
    api_id = int(os.environ["API_ID"])
    api_hash = os.environ["API_HASH"]
    token = os.environ["BOT_TOKEN"]
    # A persistent bot session reduces reauthorization and should be on a volume.
    session_path = os.environ.get("BOT_SESSION_PATH", "/data/tgforwarder/bot")
    Path(session_path).parent.mkdir(parents=True, exist_ok=True)
    client = TelegramClient(session_path, api_id, api_hash)
    db = database()
    locks = {}
    await client.start(bot_token=token)
    me = await client.get_me()
    log.info("Bot authenticated: id=%s username=%s", me.id, me.username)
    log.info("DIAGNOSTIC ONLY=%s; monitoring %d topic route(s); no messages will be sent when enabled", diagnostic_only, len(mapping))
    for source, target in mapping.items():
        for kind, chat_id in (("source", source[0]), ("destination", target[0])):
            try:
                entity = await client.get_entity(chat_id)
                log.info("Telegram %s group accessible id=%s name=%s", kind, chat_id, getattr(entity, "title", "unknown"))
            except Exception as exc:
                log.warning("Telegram %s group not resolved id=%s: %s", kind, chat_id, type(exc).__name__)

    allowed_controllers = controller_ids()
    jobs = BackupJobs()
    pending_confirmation = {}
    log.info("Private backup controller configured for %d authorized user(s); launches require explicit confirmation", len(allowed_controllers))

    @client.on(events.NewMessage(incoming=True))
    async def controller_dm(event):
        if not event.is_private:
            return
        raw_command = (event.raw_text or "").strip().split(maxsplit=1)
        if raw_command and raw_command[0].split("@")[0].lower() == "/myid":
            await event.respond("Your Telegram user ID: " + str(event.sender_id) + "\\nPanel access requires explicit admin authorization.")
            return
        # Deny silently so no unauthorized user learns the configured routes.
        if event.sender_id not in allowed_controllers:
            return
        msg = (event.raw_text or "").strip().split(maxsplit=1)
        command = msg[0].split("@")[0].lower() if msg else ""
        if command in ("/start", "/menu"):
            await event.respond("🤖 Tegridy Backup Controller\nManual-only mode. Choose a menu option.", buttons=controller_menu())
        elif command == "/status":
            try:
                await event.respond(controller_status(mapping) + "\\n\\n" + jobs.info(), buttons=controller_menu())
            except Exception:
                log.exception("Controller status read failed")
                await event.respond("⚠️ Unable to read backup progress right now.")
        elif command == "/help":
            await event.respond(controller_help(), buttons=controller_menu())

    @client.on(events.CallbackQuery())
    async def controller_buttons(event):
        if not event.is_private or event.sender_id not in allowed_controllers:
            await event.answer("Not authorized.", alert=True)
            return
        data = event.data or b""
        try:
            if data == b"tg:status":
                await event.edit(controller_status(mapping) + "\\n\\n" + jobs.info(), buttons=controller_menu())
            elif data == b"tg:jobs":
                await event.edit("📋 Manual backup jobs\\n\\n" + jobs.info() + "\\n\\n" + jobs.log_tail(1200), buttons=controller_menu())
            elif data == b"tg:confirm":
                pending_confirmation[event.sender_id] = time.monotonic() + 90
                await event.edit(
                    "⚠️ CONFIRM MANUAL BACKUP\\n\\n"
                    "Touchdowns: resume the initial backup from SQLite (only photos with captions).\\n"
                    "Telegram Restrict Saving Content must ALREADY be disabled.\\n"
                    "Telegram may impose FloodWaits. A backup cannot bypass them.\\n"
                    "Click CONFIRM within 90 seconds to start sending.",
                    buttons=[[Button.inline("✅ CONFIRM START", b"tg:launch"), Button.inline("↩️ Cancel", b"tg:cancel")]])
            elif data == b"tg:launch":
                valid_until = pending_confirmation.pop(event.sender_id, 0)
                if time.monotonic() > valid_until:
                    await event.answer("Confirmation expired; reopen Start.", alert=True)
                    return
                result = await jobs.start(client, event.sender_id)
                await event.edit(result, buttons=controller_menu())
            elif data == b"tg:stop":
                await event.edit(await jobs.stop(), buttons=controller_menu())
            elif data == b"tg:cancel":
                pending_confirmation.pop(event.sender_id, None)
                await event.edit("Cancelled; no backup started by this action.", buttons=controller_menu())
            elif data == b"tg:help":
                await event.edit(controller_help(), buttons=controller_menu())
            else:
                await event.answer("Unknown action.", alert=True)
                return
            await event.answer()
        except Exception:
            log.exception("Controller button failed")
            await event.answer("Unable to load panel.", alert=True)

    @client.on(events.NewMessage(chats=list({k[0] for k in mapping})))
    async def on_message(event):
        topic = message_topic(event.message)
        if diagnostic_only:
            log.info("DRY EVENT chat=%s msg=%s topic=%s (NOT FORWARDED)", event.chat_id, event.message.id, topic)
            return
        destination = mapping.get((event.chat_id, topic))
        if destination is None:
            return
        # One lock per destination avoids duplicate sends during overlapping events.
        lock = locks.setdefault(destination, asyncio.Lock())
        async with lock:
            try:
                if await forward_and_record(client, db, event.message, event.chat_id, *destination):
                    log.info("Copied %s/%s topic=%s", event.chat_id, event.message.id, topic)
            except Exception:
                log.exception("Copy failed %s/%s; message not marked sent", event.chat_id, event.message.id)

    try:
        await client.run_until_disconnected()
    finally:
        db.close()
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(run_live())
