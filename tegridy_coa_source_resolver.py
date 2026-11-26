"""Resolve one admin-referenced source post; never scans history or downloads media."""
import asyncio
import json
import os
import time
from tegridy_coa_store import source_post

def check_source(db, chat, topic):
    if topic is not None and not db.execute(
            "SELECT 1 FROM sources WHERE chat=? AND topic=? AND enabled=1", (chat, topic)).fetchone():
        raise ValueError("This topic is not enabled as a COA source")
    if topic is None and not db.execute(
            "SELECT 1 FROM sources WHERE chat=? AND enabled=1", (chat,)).fetchone():
        raise ValueError("No COA source is enabled for this group")

def store_fetched_post(db, chat, topic, mid, message, admin):
    from tegridy_coa_sync import in_topic, fingerprint, message_links
    if not message or message.id != mid or getattr(message, "action", None):
        raise ValueError("Original Telegram post is unavailable; check its link")
    sources = db.execute("SELECT * FROM sources WHERE chat=? AND enabled=1", (chat,)).fetchall()
    candidates = [s for s in sources if (topic is None or s["topic"] == topic) and in_topic(message,s["topic"])]
    if len(candidates) != 1:
        raise ValueError("Original post does not belong to the linked enabled COA topic")
    source = candidates[0]
    prior = db.execute("SELECT topic FROM messages WHERE chat=? AND id=?", (chat,mid)).fetchone()
    if prior and prior["topic"] != source["topic"]:
        raise ValueError("Stored source topic conflicts with the original post")
    with db:
        db.execute("""INSERT INTO messages(chat,id,topic,album,date,fingerprint,caption,ocr,media_status,links,generation,deleted)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,0) ON CONFLICT(chat,id) DO UPDATE SET deleted=0""",
            (chat,mid,source["topic"],str(message.grouped_id or ""),int(message.date.timestamp()),
             fingerprint(message),(message.message or "")[:30000],"","link_metadata_only",
             json.dumps(message_links(message)),source["generation"]))
        db.execute("INSERT OR REPLACE INTO state VALUES (?,?)",
                   (f"coa:on_demand:{chat}:{mid}",json.dumps({"admin":admin,"received_at":int(time.time()),"metadata_only":True})))

async def ensure_source(db, chat, topic, mid, admin):
    existing = source_post(db,chat,mid)
    if existing:
        if topic is not None and existing["topic"] != topic:
            raise ValueError("Original post topic does not match the link")
        return
    check_source(db,chat,topic)
    from tegridy_topics import locked_user_session, user_client
    from tegridy_state import connect as backup_connect, settings, set_setting
    from telethon.errors import FloodWaitError
    try:
        lock = locked_user_session()
    except RuntimeError as exc:
        raise ValueError("Catalogue connection is busy; please retry this file shortly") from exc
    client = None
    try:
        backup = backup_connect()
        try:
            if int(settings(backup,"flood_wait_until","0")) > time.time():
                raise ValueError("Telegram rate limit active; please retry later")
        finally:
            backup.close()
        retry = db.execute("SELECT value FROM state WHERE key='retry_after'").fetchone()
        if retry and int(retry[0]) > time.time():
            raise ValueError("Telegram rate limit active; please retry later")
        client = user_client()
        await client.connect()
        if not await client.is_user_authorized():
            raise ValueError("Catalogue connection is unavailable; original post was not added")
        me = await client.get_me()
        if not me or me.bot or me.id != int(os.environ.get("EXPECTED_USER_ID","9076133249")):
            raise ValueError("Catalogue connection could not be verified; original post was not added")
        message = await client.get_messages(chat,ids=mid)
        store_fetched_post(db,chat,topic,mid,message,admin)
    except FloodWaitError as exc:
        until = str(int(time.time()) + exc.seconds)
        backup = backup_connect()
        try:
            set_setting(backup,"flood_wait_until",until)
        finally:
            backup.close()
        with db:
            db.execute("INSERT OR REPLACE INTO state VALUES ('retry_after',?)",(until,))
        raise ValueError("Telegram rate limit active; please retry later") from exc
    finally:
        if client is not None:
            await client.disconnect()
        lock.close()
