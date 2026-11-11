"""Manual-only Telegram forum-topic backup. Never runs on import or on a schedule.

Commands:
  python tegridy_batch.py --phase initial             # read-only preview
  python tegridy_batch.py --phase initial --execute --max-send 5
  python tegridy_batch.py --phase initial --execute
  python tegridy_batch.py --phase incremental          # read-only preview
  python tegridy_batch.py --phase incremental --execute

Per-topic filters are stored in SQLite. Touchdowns is photo+caption ONLY.
Incremental: new eligible messages after the topic's completed checkpoint.
Rescan: reevaluate historical content after a changed filter without deleting receipts.
Requires authorized user session on persistent volume. Never log secrets.
"""
import argparse
import asyncio
import fcntl
import time
import logging
import os
import sqlite3
from pathlib import Path

from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.errors import FloodWaitError

from tegridy_forwarder import message_topic, copy_message
from tegridy_state import connect, filter_message, add_copy, set_run_copied, route_filter

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("tegridy-manual-batch")


def state_db():
    return connect()

def get_state(db, source, target):
    return db.execute(
        "SELECT initial_cutoff, initial_complete, last_complete FROM batch_state WHERE source_chat=? AND source_topic=? AND target_chat=? AND target_topic=?",
        (*source, *target),
    ).fetchone()


def seen(db, source, target, msg_id):
    return db.execute(
        "SELECT 1 FROM forwarded WHERE source_chat=? AND source_msg=? AND target_chat=? AND target_topic=?",
        (source[0], msg_id, *target),
    ).fetchone() is not None


def is_in_topic(m, topic):
    found = message_topic(m)
    return m.id == topic or found == topic or (topic == 1 and found is None)


def eligible(m, mode, exclude_fun):
    return filter_message(m, mode, exclude_fun)


async def latest_topic_message_id(client, chat, topic):
    if topic == 1:
        # General is a special forum topic: group-wide last ID is a safe
        # upper bound; the scan later filters out every non-General topic.
        message = await client.get_messages(chat, limit=1)
        return message[0].id if message else 1
    async for message in client.iter_messages(chat, reply_to=topic, limit=1):
        if is_in_topic(message, topic):
            return message.id
    return topic


async def run(args):
    source = (int(args.source_chat), int(args.source_topic))
    # Always manually invoked: no live event listener. The user session is needed
    # because bots cannot enumerate full group history.
    session_path = os.environ.get("USER_SESSION_PATH", "/data/tgforwarder/user")
    if not Path(session_path + ".session").exists():
        raise ValueError("No authorized Telegram user session file; run python tegridy_auth.py via Railway SSH first")
    # A single volume-backed lock prevents overlapping manual and controller runs.
    lock_path = Path(os.getenv("BACKUP_DB_PATH", "/data/tgforwarder/forwarded.sqlite3")).with_suffix(".batch.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_handle = open(lock_path, "a+")
    try:
        fcntl.flock(lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock_handle.close()
        raise RuntimeError("Another backup is already running")
    db = state_db()
    route = db.execute("SELECT target_chat,target_topic,content_filter,exclude_fun FROM topic_routes WHERE source_chat=? AND source_topic=? AND enabled=1", source).fetchone()
    if route is None:
        raise ValueError("Source topic not linked and enabled in SQLite")
    target = (int(route[0]),int(route[1]))
    content_filter, exclude_fun = route[2],bool(route[3])
    run_id = int(os.environ.get("BACKUP_RUN_ID", "0"))
    # Telegram may otherwise silently sleep for short FloodWaits (Telethon default).
    # Handle every server-requested wait explicitly below, without bypassing it.
    client = TelegramClient(session_path, int(os.environ["API_ID"]), os.environ["API_HASH"], flood_sleep_threshold=0)
    try:
        await client.connect()
        if not await client.is_user_authorized():
            raise ValueError("USER_SESSION_STRING is not authenticated")
        record = get_state(db, source, target)
        if args.phase == "initial" and record and record[1]:
            raise ValueError("Initial batch already completed; use --phase incremental")
        if args.phase in ("incremental", "rescan") and (not record or not record[1]):
            raise ValueError("Complete initial batch before incremental mode")

        latest = await latest_topic_message_id(client, *source)
        if args.phase == "initial":
            upper = record[0] if record else latest
            lower = 0
        elif args.phase == "rescan":
            lower = 0
            upper = latest
        else:
            lower = record[2]
            upper = latest
        log.info("MODE=%s phase=%s source=%s topic=%s destination=%s topic=%s bounds=(%s,%s]",
                 "EXECUTE" if args.execute else "DRY_RUN", args.phase, source[0], source[1], target[0], target[1], lower, upper)
        if args.execute and not record and args.phase == "initial":
            # Freeze initial historical cutoff even on partial/interrupted runs.
            db.execute("INSERT INTO batch_state VALUES (?,?,?,?,?,0,0)", (*source, *target, upper))
            db.commit()

        scanned = matched = skipped = already = copied = failed = 0
        capped = False
        stopped_for_flood = False
        # Telegram's forum-thread reply_to filter can exclude the topic root;
        # this intentionally scans posts/replies within the thread, not other topics.
        messages=(client.iter_messages(source[0], reverse=True) if source[1] == 1
                  else client.iter_messages(source[0], reply_to=source[1], reverse=True))
        async for m in messages:
            if m.id <= lower:
                continue
            if m.id > upper:
                break
            scanned += 1
            if not is_in_topic(m, source[1]):
                skipped += 1
                continue
            if not eligible(m, content_filter, exclude_fun):
                skipped += 1
                continue
            matched += 1
            if seen(db, source, target, m.id):
                already += 1
                continue
            if not args.execute:
                if matched <= 20:
                    log.info("PREVIEW candidate message_id=%s has_photo=%s caption=%s", m.id, bool(m.photo), bool((m.message or "").strip()))
                continue
            if args.max_send and copied >= args.max_send:
                capped = True
                break
            try:
                # Deliberate *pre-send* pacing; no burst at the beginning.
                # Delay is per outbound photo, not per scanned/skipped message.
                if args.execute and args.delay:
                    await asyncio.sleep(args.delay)
                sent = await copy_message(client, m, *target, content_filter=content_filter, exclude_fun=exclude_fun)
                if sent is None:
                    raise RuntimeError("Unsupported message content")
                sent_id = sent[0].id if isinstance(sent, list) else sent.id
                add_copy(db, source[0], m.id, *target, sent_id)
                copied += 1
                if run_id:
                    set_run_copied(db, run_id, copied)
                log.info("COPY_EVENT phase=%s source_topic=%s msg=%s count=%s", args.phase, source[1], m.id, copied)
                if copied % 10 == 0:
                    log.info("PROGRESS copied_this_run=%s already_recorded=%s total_done_estimate=%s last_message=%s", copied, already, copied + already, m.id)
            except FloodWaitError as exc:
                failed += 1
                stopped_for_flood = True
                log.error("TELEGRAM FLOOD WAIT %ss; STOPPING at message=%s. Do not retry until Telegram wait expires; script will not bypass this restriction.", exc.seconds, m.id)
                break
            except Exception as exc:
                failed += 1
                log.error("COPY FAILED message_id=%s error_type=%s", m.id, type(exc).__name__)
                # Halt rather than falsely marking a batch complete.
                break

        complete = bool(args.execute and not capped and not failed)
        if complete:
            if args.phase == "initial":
                db.execute("UPDATE batch_state SET initial_complete=1, last_complete=? WHERE source_chat=? AND source_topic=? AND target_chat=? AND target_topic=?", (upper, *source, *target))
            elif args.phase == "incremental":
                db.execute("UPDATE batch_state SET last_complete=? WHERE source_chat=? AND source_topic=? AND target_chat=? AND target_topic=?", (upper, *source, *target))
            db.commit()
        log.info("SUMMARY phase=%s scanned=%s matched=%s skipped=%s already_recorded=%s copied=%s failed=%s flood_wait=%s batch_complete=%s upper=%s delay_seconds=%s",
                 args.phase, scanned, matched, skipped, already, copied, failed, stopped_for_flood, complete, upper, args.delay)
        if failed:
            raise RuntimeError("Batch stopped after a copy failure; repair and rerun with deduplication")
    finally:
        await client.disconnect()
        db.close()
        fcntl.flock(lock_handle, fcntl.LOCK_UN)
        lock_handle.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Explicitly triggered Tegridy forum-topic backup; never a daemon")
    parser.add_argument("--phase", choices=["initial", "incremental", "rescan"], required=True)
    parser.add_argument("--source-chat", default="-1004350769620")
    parser.add_argument("--source-topic", default="501")
    parser.add_argument("--execute", action="store_true", help="Actually send copies; omit for read-only preview")
    parser.add_argument("--max-send", type=int, default=0, help="Limit new copies per run; 0 means no cap")
    parser.add_argument("--delay", type=float, default=15.0, help="Minimum seconds BEFORE each send (default 15, minimum 10 for execute)")
    args = parser.parse_args()
    if args.max_send < 0 or args.delay < 0:
        parser.error("--max-send and --delay must be >= 0")
    if args.execute and args.delay < 10:
        parser.error("To protect the account, --delay must be at least 10 seconds when --execute is specified")
    asyncio.run(run(args))
