"""Read selected Telegram topics into the COA DB; never sends Telegram messages."""
import argparse
import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from dotenv import load_dotenv
from telethon.errors import FloodWaitError
from tegridy_topics import locked_user_session, user_client
from tegridy_coa_store import connect
from tegridy_state import connect as backup_connect, settings, set_setting

log = logging.getLogger("tegridy-coas")


def in_topic(message, topic):
    reply = getattr(message, "reply_to", None)
    found = (getattr(reply, "reply_to_top_id", None) or getattr(reply, "reply_to_msg_id", None)) if getattr(reply, "forum_topic", False) else None
    return message.id == topic or found == topic or (topic == 1 and found is None)


def fingerprint(message):
    payload = (message.message or "", str(getattr(message, "edit_date", "")),
               getattr(getattr(message, "photo", None), "id", None),
               getattr(getattr(message, "document", None), "id", None))
    return hashlib.sha256(json.dumps(payload).encode()).hexdigest()


def ocr_file(path):
    result = subprocess.run(["tesseract", str(path), "stdout", "-l", "eng+spa", "--psm", "11"],
                            capture_output=True, timeout=45, check=True)
    return result.stdout.decode("utf-8", errors="replace")[:30000]


async def extract_media(client, message, protected):
    image = bool(message.photo) or bool(message.document and (message.document.mime_type or "").startswith("image/"))
    if not image:
        return "", "link_or_text" if not message.document else "unsupported_document"
    if protected or getattr(message, "noforwards", False):
        return "", "protected_media_not_downloaded"
    if not shutil.which("tesseract"):
        return "", "ocr_unavailable"
    if getattr(message.file, "size", 0) and message.file.size > 15 * 1024 * 1024:
        return "", "image_too_large"
    with tempfile.TemporaryDirectory(prefix="coa-") as folder:
        path = await client.download_media(message, file=folder + "/")
        if not path:
            return "", "download_unavailable"
        if Path(path).stat().st_size > 15 * 1024 * 1024:
            return "", "image_too_large"
        try:
            text = await asyncio.to_thread(ocr_file, path)
            return text, "ocr_text" if text.strip() else "ocr_empty"
        except (subprocess.SubprocessError, OSError):
            return "", "ocr_failed"


async def save_message(db, client, source, message, generation, protected):
    if not in_topic(message, source["topic"]) or getattr(message, "action", None):
        return
    old = db.execute("SELECT fingerprint,ocr,media_status FROM messages WHERE chat=? AND id=?", (source["chat"], message.id)).fetchone()
    fp = fingerprint(message)
    retry = old and old["media_status"] in ("ocr_unavailable", "ocr_failed", "download_unavailable", "protected_media_not_downloaded")
    if old and old["fingerprint"] == fp and not retry:
        with db:
            db.execute("UPDATE messages SET generation=?,deleted=0 WHERE chat=? AND id=?", (generation, source["chat"], message.id))
        return
    text, status = await extract_media(client, message, protected)
    caption = (message.message or "")[:30000]
    links = re.findall(r"https?://[^\s<>]+", caption)
    for entity in message.entities or []:
        url = getattr(entity, "url", "")
        if url.startswith(("https://", "http://")):
            links.append(url)
    with db:
        db.execute("""INSERT INTO messages VALUES (?,?,?,?,?,?,?,?,?,?,?,0)
          ON CONFLICT(chat,id) DO UPDATE SET topic=excluded.topic,album=excluded.album,
          date=excluded.date,fingerprint=excluded.fingerprint,caption=excluded.caption,
          ocr=excluded.ocr,media_status=excluded.media_status,links=excluded.links,
          generation=excluded.generation,deleted=0""",
          (source["chat"], message.id, source["topic"], str(message.grouped_id or ""),
           int(message.date.timestamp()), fp, caption, text, status,
           json.dumps(sorted(set(links))), generation))


async def sync_source(db, client, source, batch):
    chat, topic = source["chat"], source["topic"]
    entity = await client.get_entity(chat)
    protected = bool(getattr(entity, "noforwards", False))
    kwargs = {} if topic == 1 else {"reply_to": topic}
    # Keep recent posts available while the historical pass is still in progress.
    recent_options = {"reverse": True, "min_id": source["recent_cursor"], "limit": batch} if source["recent_cursor"] else {"limit": 30}
    highwater = source["recent_cursor"]
    async for message in client.iter_messages(chat, **recent_options, **kwargs):
        await save_message(db, client, source, message, source["generation"], protected)
        highwater = max(highwater, message.id)
        if source["recent_cursor"]:
            with db:
                db.execute("UPDATE sources SET recent_cursor=? WHERE chat=? AND topic=?", (highwater, chat, topic))
    with db:
        db.execute("UPDATE sources SET recent_cursor=? WHERE chat=? AND topic=?", (highwater, chat, topic))
    now = int(time.time())
    if not source["cutoff"]:
        if source["completed_at"] and now - source["completed_at"] < 86400:
            with db:
                db.execute("UPDATE sources SET last_sync=?,error='' WHERE chat=? AND topic=?", (now, chat, topic))
            return
        latest = await client.get_messages(chat, limit=1)
        stored_max = db.execute("SELECT max(id) FROM messages WHERE chat=? AND topic=?", (chat, topic)).fetchone()[0] or topic
        cutoff = max(latest[0].id if latest else topic, stored_max)
        with db:
            db.execute("UPDATE sources SET cutoff=?,cursor=0,generation=generation+1 WHERE chat=? AND topic=?", (cutoff, chat, topic))
        source = db.execute("SELECT * FROM sources WHERE chat=? AND topic=?", (chat, topic)).fetchone()
    if source["cursor"] == 0 and topic != 1:
        root = await client.get_messages(chat, ids=topic)
        if root:
            await save_message(db, client, source, root, source["generation"], protected)
    count = 0
    async for message in client.iter_messages(chat, reverse=True, min_id=source["cursor"], max_id=source["cutoff"] + 1, limit=batch, **kwargs):
        await save_message(db, client, source, message, source["generation"], protected)
        with db:
            db.execute("UPDATE sources SET cursor=? WHERE chat=? AND topic=?", (message.id, chat, topic))
        count += 1
    with db:
        if count < batch:
            # A complete frozen historical pass also marks deleted old posts.
            db.execute("UPDATE messages SET deleted=1 WHERE chat=? AND topic=? AND id<=? AND generation<>?", (chat, topic, source["cutoff"], source["generation"]))
            db.execute("UPDATE sources SET cutoff=0,cursor=0,completed_at=? WHERE chat=? AND topic=?", (now, chat, topic))
        db.execute("UPDATE sources SET last_sync=?,error='' WHERE chat=? AND topic=?", (now, chat, topic))


async def run(batch=100):
    lock = locked_user_session()
    db = connect()
    client = None
    try:
        backup = backup_connect()
        try:
            if int(settings(backup, "flood_wait_until", "0")) > time.time():
                return
        finally:
            backup.close()
        sources = db.execute("SELECT * FROM sources WHERE enabled=1").fetchall()
        wait = db.execute("SELECT value FROM state WHERE key='retry_after'").fetchone()
        if not sources or (wait and int(wait[0]) > time.time()):
            return
        client = user_client()
        await client.connect()
        if not await client.is_user_authorized():
            raise RuntimeError("Dedicated account requires authorization")
        me = await client.get_me()
        if not me or me.bot or me.id != int(os.environ.get("EXPECTED_USER_ID", "9076133249")):
            raise RuntimeError("Dedicated Telegram account mismatch")
        for source in sources:
            try:
                await sync_source(db, client, source, batch)
                progress = db.execute("SELECT cutoff,completed_at FROM sources WHERE chat=? AND topic=?", (source["chat"], source["topic"])).fetchone()
                count = db.execute("SELECT count(*) FROM messages WHERE chat=? AND topic=? AND deleted=0", (source["chat"], source["topic"])).fetchone()[0]
                log.info("COA sync topic=%s indexed=%s history=%s", source["topic"], count,
                         "loading" if progress["cutoff"] or not progress["completed_at"] else "loaded")
            except FloodWaitError as exc:
                backup = backup_connect()
                try:
                    set_setting(backup, "flood_wait_until", str(int(time.time()) + exc.seconds))
                finally:
                    backup.close()
                with db:
                    db.execute("INSERT OR REPLACE INTO state VALUES ('retry_after',?)", (str(int(time.time()) + exc.seconds),))
                    db.execute("UPDATE sources SET error='Telegram rate limit; waiting' WHERE chat=? AND topic=?", (source["chat"], source["topic"]))
                log.warning("COA sync paused for Telegram rate limit: %s seconds", exc.seconds)
                break
            except Exception as exc:
                with db:
                    db.execute("UPDATE sources SET error=? WHERE chat=? AND topic=?", (type(exc).__name__, source["chat"], source["topic"]))
                log.warning("COA source %s sync failed: %s", source["topic"], type(exc).__name__)
    finally:
        if client is not None:
            await client.disconnect()
        db.close()
        lock.close()


if __name__ == "__main__":
    load_dotenv()
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch", type=int, default=100)
    args = parser.parse_args()
    if not 1 <= args.batch <= 500:
        parser.error("batch must be between 1 and 500")
    asyncio.run(run(args.batch))
