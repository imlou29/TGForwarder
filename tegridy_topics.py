"""Discover, match, and explicitly create Telegram forum topics.

Never sends or copies chat content. Requires the dedicated user's
authorized session; caller must prevent concurrent backup runs.
"""
import asyncio
import fcntl
import os
import re
import time
import unicodedata
from pathlib import Path

from telethon import TelegramClient, functions
from tegridy_state import SRC, DST, connect


def normalize(name):
    return " ".join(unicodedata.normalize("NFKC", name or "").casefold().split())


def user_client():
    path = os.environ.get("USER_SESSION_PATH", "/data/tgforwarder/user")
    if not Path(path + ".session").exists():
        raise RuntimeError("Dedicated user session not available")
    return TelegramClient(path, int(os.environ["API_ID"]), os.environ["API_HASH"],
                          flood_sleep_threshold=0)


async def fetch_topics(client, chat):
    peer = await client.get_input_entity(chat)
    topics = {}
    offset_date = offset_id = offset_topic = 0
    for page in range(20):
        response = await client(functions.channels.GetForumTopicsRequest(
            channel=peer, offset_date=offset_date, offset_id=offset_id,
            offset_topic=offset_topic, limit=100, q=""))
        batch = [t for t in response.topics if getattr(t, "title", None) is not None]
        for t in batch:
            topics[int(t.id)] = str(t.title)
        if len(topics) >= int(getattr(response, "count", 0)):
            return topics
        if not batch:
            break
        last = batch[-1]
        prev = (offset_date, offset_id, offset_topic)
        offset_topic = int(last.id)
        offset_id = int(getattr(last, "top_message", 0))
        # Top-message date must be used, not the topic creation date.
        dates = {m.id: getattr(m, "date", None) for m in response.messages}
        dt = dates.get(offset_id)
        offset_date = int(dt.timestamp()) if dt else 0
        if (offset_date, offset_id, offset_topic) == prev:
            break
    raise RuntimeError("Topic discovery incomplete; refusing to infer matches")


def locked_user_session():
    path = Path(os.getenv("BACKUP_DB_PATH", "/data/tgforwarder/forwarded.sqlite3")).with_suffix(".batch.lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        raise RuntimeError("A backup is running; discover topics after it stops")
    return handle


async def discover():
    handle = locked_user_session()
    try:
        client = user_client()
        await client.connect()
        try:
            if not await client.is_user_authorized():
                raise RuntimeError("Dedicated user session is not authorized")
            source = await fetch_topics(client, SRC)
            destination = await fetch_topics(client, DST)
        finally:
            await client.disconnect()
        db = connect()
        try:
            now = int(time.time())
            with db:
                for chat, inventory in ((SRC, source), (DST, destination)):
                    for topic, title in inventory.items():
                        db.execute("""INSERT INTO topic_inventory VALUES (?,?,?,?,?)
                          ON CONFLICT(chat,topic) DO UPDATE SET
                          title=excluded.title,is_general=excluded.is_general,
                          scanned_at=excluded.scanned_at""",
                          (chat,topic,title,int(topic==1),now))
                db.execute("DELETE FROM topic_inventory WHERE chat IN (?,?) AND scanned_at<>?", (SRC,DST,now))
                # Match a source by exact normalized title ONLY if unique in both groups.
                taken = {(r[0],r[1]) for r in db.execute("SELECT target_chat,target_topic FROM topic_routes")}
                linked = {(r[0],r[1]) for r in db.execute("SELECT source_chat,source_topic FROM topic_routes")}
                src_count = {}
                dst_count = {}
                for name in source.values():
                    key=normalize(name)
                    src_count[key]=src_count.get(key,0)+1
                for name in destination.values():
                    key=normalize(name)
                    dst_count[key]=dst_count.get(key,0)+1
                for source_id,title in source.items():
                    key=normalize(title)
                    if (SRC,source_id) in linked or src_count.get(key)!=1 or dst_count.get(key)!=1:
                        continue
                    dest_id=next((tid for tid,n in destination.items() if normalize(n)==key),None)
                    if dest_id is None or (DST,dest_id) in taken:
                        continue
                    # Touchdowns preserved; new routes default text-only until configured.
                    db.execute("""INSERT OR IGNORE INTO topic_routes
                        (source_chat,source_topic,target_chat,target_topic,source_name,target_name,
                        content_filter) VALUES (?,?,?,?,?,?,?)""",
                        (SRC,source_id,DST,dest_id,title,destination[dest_id],"text"))
                    linked.add((SRC,source_id))
                    taken.add((DST,dest_id))
            return topic_report(db)
        finally:
            db.close()
    finally:
        fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()


def topic_report(db):
    sources = db.execute("SELECT topic,title FROM topic_inventory WHERE chat=? ORDER BY title", (SRC,)).fetchall()
    dest = db.execute("SELECT topic,title FROM topic_inventory WHERE chat=? ORDER BY title", (DST,)).fetchall()
    linked = {(r[0],r[1]): (r[2],r[3]) for r in
              db.execute("SELECT source_chat,source_topic,target_chat,target_topic FROM topic_routes")}
    missing = [(int(sid),name) for sid,name in sources if (SRC,sid) not in linked]
    return {"sources":sources,"destinations":dest,"matched":len(sources)-len(missing),
            "missing":missing,"total":len(sources)}


def candidate_destinations(db, source_topic):
    src = db.execute("SELECT title FROM topic_inventory WHERE chat=? AND topic=?", (SRC,source_topic)).fetchone()
    if not src:
        return []
    return db.execute("""SELECT i.topic,i.title FROM topic_inventory i
         WHERE i.chat=? AND NOT EXISTS (SELECT 1 FROM topic_routes r
             WHERE r.target_chat=i.chat AND r.target_topic=i.topic)
         ORDER BY CASE WHEN lower(i.title)=lower(?) THEN 0 ELSE 1 END, i.title""",
         (DST,src[0])).fetchall()


def link_existing(db, source_topic, destination_topic):
    source=db.execute("SELECT title FROM topic_inventory WHERE chat=? AND topic=?", (SRC,source_topic)).fetchone()
    target=db.execute("SELECT title FROM topic_inventory WHERE chat=? AND topic=?", (DST,destination_topic)).fetchone()
    if not source or not target:
        raise ValueError("Inventory expired or topic missing; run Sync Topics")
    with db:
        db.execute("""INSERT INTO topic_routes
          (source_chat,source_topic,target_chat,target_topic,source_name,target_name,content_filter)
          VALUES (?,?,?,?,?,?,?)""",(SRC,source_topic,DST,destination_topic,source[0],target[0],"text"))
    return source[0], target[0]


async def create_missing(source_topic):
    """Explicitly confirmed, unique missing topic, no blind creation."""
    handle=locked_user_session()
    try:
        db=connect()
        try:
            row=db.execute("SELECT title FROM topic_inventory WHERE chat=? AND topic=?", (SRC,source_topic)).fetchone()
            if not row:
                raise ValueError("Topic no longer in inventory")
            if db.execute("SELECT 1 FROM topic_routes WHERE source_chat=? AND source_topic=?", (SRC,source_topic)).fetchone():
                raise ValueError("Topic already linked; refusing to create")
            title=row[0]
        finally:
            db.close()
        client=user_client()
        await client.connect()
        try:
            if not await client.is_user_authorized():
                raise ValueError("Dedicated user is not authorized")
            # Recheck actual current destination immediately before write.
            destinations=await fetch_topics(client,DST)
            source=await fetch_topics(client,SRC)
            if source.get(source_topic)!=title:
                raise ValueError("Source changed since discovery; scan again")
            matches=[tid for tid,name in destinations.items() if normalize(name)==normalize(title)]
            if matches:
                raise ValueError("Matching destination topic already exists; link instead")
            await client(functions.channels.CreateForumTopicRequest(
                channel=await client.get_input_entity(DST), title=title))
            after=await fetch_topics(client,DST)
            created=[tid for tid,name in after.items() if tid not in destinations and normalize(name)==normalize(title)]
            if len(created)!=1:
                raise RuntimeError("Topic may have been created but ID could not be confirmed; rescan before retrying")
            new_id=created[0]
        finally:
            await client.disconnect()
        db=connect()
        try:
            with db:
                db.execute("""INSERT OR IGNORE INTO topic_inventory VALUES (?,?,?,?,?)""",
                           (DST,new_id,title,0,int(time.time())))
            link_existing(db,source_topic,new_id)
        finally:
            db.close()
        return title,new_id
    finally:
        fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()
