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
from pathlib import Path

from dotenv import load_dotenv
from telethon import TelegramClient, events

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
    if message.action:
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


async def run_live():
    mapping = routes()
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
    log.info("Bot authenticated; monitoring %d topic route(s)", len(mapping))

    @client.on(events.NewMessage(chats=list({k[0] for k in mapping})))
    async def on_message(event):
        topic = message_topic(event.message)
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
