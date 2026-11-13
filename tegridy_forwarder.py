"""Topic-scoped Telegram backup for Tegridy, independent from OpenClaw.

Requires Telethon 1.40.0. TOPIC_ROUTES is a JSON array, for example:
[{"source_chat":-1004350769620,"source_topic":501,
  "target_chat":-1003900751944,"target_topic":3962}]
Legacy SOURCE_ID and TARGET_ID are deliberately ignored.
"""
import asyncio
import fcntl
import json
import logging
import os
import sqlite3
import time
from pathlib import Path

from dotenv import load_dotenv
from telethon import TelegramClient, events, Button
from tegridy_controller import BackupJobs
from tegridy_ui import install
from tegridy_state import connect, mappings, filter_message, add_copy, settings, SRC

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
    return connect()


def already_sent(db, source_chat, source_msg, target_chat, target_topic):
    return db.execute("SELECT 1 FROM forwarded WHERE source_chat=? AND source_msg=? AND target_chat=? AND target_topic=?", (source_chat, source_msg, target_chat, target_topic)).fetchone() is not None


async def copy_message(client, message, target_chat, target_topic, content_filter="photo_caption", exclude_fun=True):
    """Copy eligible content; media and text retain captions where supported."""
    if not filter_message(message, content_filter, exclude_fun):
        return None
    if message.media and (message.photo or message.video or message.document):
        # Telethon can send most media using a Telegram media reference. Some
        # restricted/unsupported media cannot be copied; log failure instead.
        sent = await client.send_file(target_chat, message.media, caption=message.message or "", formatting_entities=message.entities or None, reply_to=target_topic)
    elif message.message:
        sent = await client.send_message(target_chat, message.message, formatting_entities=message.entities or None, reply_to=target_topic, link_preview=False)
    else:
        return None
    return sent


async def forward_and_record(client, db, message, source_chat, target_chat, target_topic, content_filter="photo_caption", exclude_fun=True):
    if already_sent(db, source_chat, message.id, target_chat, target_topic):
        return False
    sent = await copy_message(client, message, target_chat, target_topic, content_filter, exclude_fun)
    if sent is None:
        return False
    sent_id = sent[0].id if isinstance(sent, list) else sent.id
    add_copy(db,source_chat,message.id,target_chat,target_topic,sent_id)
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
    return ("ℹ️ Tegridy Backup Controller\n\n"
            "Use /start, /status or the private buttons.\n"
            "Start / Resume requires a second explicit confirmation and only runs manually.\n"
            "Stop sends an interrupt; an in-flight Telegram request may finish.\n"
            "Only photos containing a written caption are eligible.\n"
            "Telegram group Restrict Saving Content must be disabled by an admin during backup.\n"
            "Automatic forwarding remains disabled.")


async def run_live():
    # TOPIC_ROUTES seeds existing records only. SQLite is the durable config.
    mapping = routes()
    api_id = int(os.environ["API_ID"])
    api_hash = os.environ["API_HASH"]
    token = os.environ["BOT_TOKEN"]
    # Pin the intended controller bot and never reuse a previous bot's Telethon session.
    expected_bot_id = int(os.environ.get("EXPECTED_BOT_ID", "9076133249"))
    if token.split(":", 1)[0] != str(expected_bot_id):
        raise ValueError("BOT_TOKEN does not belong to the configured Tegridy Backup bot ID")
    session_base = os.environ.get("BOT_SESSION_PATH", "/data/tgforwarder/bot")
    session_path = f"{session_base}-{expected_bot_id}"
    Path(session_path).parent.mkdir(parents=True, exist_ok=True)
    client = TelegramClient(session_path, api_id, api_hash)
    db = database()
    # Any job left 'running' through a prior process restart was interrupted.
    with db:
        db.execute("UPDATE backup_runs SET status='interrupted',finished_at=?,error='Service restarted during job' WHERE status='running'",
                   (int(time.time()),))
    live_lock = asyncio.Lock()
    await client.start(bot_token=token)
    me = await client.get_me()
    if not me or me.id != expected_bot_id:
        raise RuntimeError("Telegram authenticated a different bot than the configured backup controller")
    log.info("Bot authenticated: id=%s username=%s", me.id, me.username)
    log.info("Runtime automatic forwarding switch=%s; TG_DIAGNOSTIC_ONLY is legacy configuration, not the runtime switch",
             settings(db,"auto_forwarding","off"))
    for source, target in mapping.items():
        for kind, chat_id in (("source", source[0]), ("destination", target[0])):
            try:
                entity = await client.get_entity(chat_id)
                log.info("Telegram %s group accessible id=%s name=%s", kind, chat_id, getattr(entity, "title", "unknown"))
            except Exception as exc:
                log.warning("Telegram %s group not resolved id=%s: %s", kind, chat_id, type(exc).__name__)

    allowed_controllers = controller_ids()
    jobs = BackupJobs()
    install(client,jobs,allowed_controllers)
    log.info("Private controller active with %s authorized admins; forwarding=%s",
             len(allowed_controllers),settings(db,"auto_forwarding","off"))

    @client.on(events.NewMessage(incoming=True, chats=[SRC]))
    async def auto_forward_message(event):
        if settings(db,"auto_forwarding","off") != "on":
            return
        topic = message_topic(event.message)
        if topic is None:
            topic = 1  # General forum topic.
        routes_now = [r for r in mappings(db) if
                      r["source_chat"]==event.chat_id and r["source_topic"]==topic]
        if not routes_now:
            return
        route=routes_now[0]
        if not filter_message(event.message,route["filter"],bool(route["exclude_fun"])):
            return
        if jobs.running():
            log.warning("Skipping auto forward while manual job active: msg=%s",event.message.id)
            return
        async with live_lock:
            if settings(db,"auto_forwarding","off")!="on":
                return
            lock_path=Path(os.getenv("BACKUP_DB_PATH","/data/tgforwarder/forwarded.sqlite3")).with_suffix(".batch.lock")
            handle=open(lock_path,"a+")
            try:
                try:
                    fcntl.flock(handle,fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    log.warning("Manual backup holds lock; auto event deferred to next manual incremental: msg=%s",event.message.id)
                    return
                if already_sent(db,event.chat_id,event.message.id,route["target_chat"],route["target_topic"]):
                    return
                # Same 15-second rate pacing as manual sends.
                await asyncio.sleep(15)
                if settings(db,"auto_forwarding","off")!="on":
                    return
                if await forward_and_record(
                        client,db,event.message,event.chat_id,
                        route["target_chat"],route["target_topic"],
                        content_filter=route["filter"],
                        exclude_fun=bool(route["exclude_fun"])):
                    log.info("AUTO COPIED topic=%s source_message=%s",topic,event.message.id)
            except Exception:
                log.exception("Automatic forwarding failed. Never bypass Telegram restrictions or FloodWait.")
            finally:
                fcntl.flock(handle,fcntl.LOCK_UN)
                handle.close()

    try:
        await client.run_until_disconnected()
    finally:
        db.close()
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(run_live())
