"""One-time Touchdowns photo+caption backfill using an authorized Telegram user.

Use a USER_SESSION_STRING generated through an interactive local Telethon login.
Never commit the session string or Telegram credentials to GitHub.
By default this is a dry run; set BACKFILL_EXECUTE=yes to actually copy.
Run against the same persistent BACKUP_DB_PATH as the live service to deduplicate.
"""
import asyncio
import logging
import os

from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.sessions import StringSession

from tegridy_forwarder import database, forward_and_record, message_topic, routes

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("historical-import")


async def main():
    mapping = routes()
    source_chat = int(os.getenv("BACKFILL_SOURCE_CHAT", "-1004350769620"))
    source_topic = int(os.getenv("BACKFILL_SOURCE_TOPIC", "501"))
    target = mapping.get((source_chat, source_topic))
    if not target:
        raise ValueError("Backfill route not present in TOPIC_ROUTES")
    session_string = os.environ.get("USER_SESSION_STRING", "")
    if not session_string:
        raise ValueError("USER_SESSION_STRING is required; bot tokens cannot read full message history")
    execute = os.environ.get("BACKFILL_EXECUTE", "").lower() == "yes"
    limit = int(os.getenv("BACKFILL_LIMIT", "0"))
    api_id = int(os.environ["API_ID"])
    api_hash = os.environ["API_HASH"]
    client = TelegramClient(StringSession(session_string), api_id, api_hash)
    db = database()
    total = matched = copied = 0
    try:
        await client.start()
        log.info("Historical backfill mode: %s", "EXECUTE" if execute else "DRY RUN")
        # Telethon's reply_to filter fetches thread replies; verify topic membership
        # independently to avoid copying material from unrelated discussions.
        async for message in client.iter_messages(source_chat, reply_to=source_topic, reverse=True):
            total += 1
            if message_topic(message) != source_topic:
                continue
            if not (message.photo and (message.message or "").strip()):
                continue
            matched += 1
            if not execute:
                log.info("MATCH id=%d caption_chars=%d", message.id, len(message.message))
                continue
            try:
                if await forward_and_record(client, db, message, source_chat, *target):
                    copied += 1
            except Exception:
                log.exception("Backfill failed for message %d", message.id)
            if limit and copied >= limit:
                break
        log.info("Finished scanned=%d matching=%d copied=%d execute=%s", total, matched, copied, execute)
    finally:
        db.close()
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
