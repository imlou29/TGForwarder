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
from tegridy_coa_scope import paused, mode, has_lab_link

log = logging.getLogger("tegridy-coas")


class SourcePaused(RuntimeError):
    pass


def ensure_active(db, source):
    row = db.execute("SELECT enabled FROM sources WHERE chat=? AND topic=?", (source["chat"], source["topic"])).fetchone()
    if not row or not row[0] or paused(db, source["chat"], source["topic"]):
        raise SourcePaused("Catalogue source is paused or disabled")


def message_links(message):
    links = re.findall(r"https?://[^\s<>]+", message.message or "")
    for entity in message.entities or []:
        url = getattr(entity, "url", "")
        if url.startswith(("https://", "http://")):
            links.append(url)
    return sorted(set(links))


def in_topic(message, topic):
    reply = getattr(message, "reply_to", None)
    found = (getattr(reply, "reply_to_top_id", None) or getattr(reply, "reply_to_msg_id", None)) if getattr(reply, "forum_topic", False) else None
    return message.id == topic or found == topic or (topic == 1 and found is None)


def fingerprint(message):
    payload = (message.message or "", str(getattr(message, "edit_date", "")),
               getattr(getattr(message, "photo", None), "id", None),
               getattr(getattr(message, "document", None), "id", None))
    return hashlib.sha256(json.dumps(payload).encode()).hexdigest()


def ocr_file(path, timeout=45):
    result = subprocess.run(["tesseract", str(path), "stdout", "-l", "eng+spa", "--psm", "11"],
                            capture_output=True, timeout=timeout, check=True,
                            env={**os.environ, "OMP_THREAD_LIMIT": "1"})
    return result.stdout.decode("utf-8", errors="replace")[:30000]


def extract_pdf(path):
    """Bounded local PDF text extraction with per-page OCR fallback."""
    if not all(shutil.which(tool) for tool in ("pdfinfo", "pdftotext", "pdftoppm", "tesseract")):
        return "", "pdf_tools_unavailable"
    try:
        info = subprocess.run(["pdfinfo", str(path)], capture_output=True, timeout=15,
                              check=True, env={**os.environ, "LC_ALL": "C"}).stdout.decode("utf-8", errors="replace")
        # Owner-password protection may coexist with permitted, password-free
        # reading/copying. Do not reject those PDFs merely for encryption.
        encrypted = re.search(r"^Encrypted:\s+yes[^\n]*", info, re.M | re.I)
        if encrypted and not re.search(r"\bcopy:\s*yes\b", encrypted[0], re.I):
            return "", "pdf_restricted"
        match = re.search(r"^Pages:\s+(\d+)", info, re.M)
        if not match:
            return "", "pdf_failed"
        pages = int(match.group(1))
    except (subprocess.SubprocessError, OSError) as exc:
        error = getattr(exc, "stderr", b"") or b""
        if isinstance(error, bytes):
            error = error.decode("utf-8", errors="replace")
        return "", "pdf_restricted" if "password" in error.lower() else "pdf_failed"
    chunks = []
    partial = pages > 6
    used_ocr = False
    deadline = time.monotonic() + 75
    with tempfile.TemporaryDirectory(prefix="coa-pdf-") as folder:
        for page in range(1, min(pages, 6) + 1):
            if deadline - time.monotonic() < 2:
                partial = True
                break
            try:
                native = subprocess.run(["pdftotext", "-f", str(page), "-l", str(page), "-layout", str(path), "-"],
                                        capture_output=True, timeout=min(15, max(1, deadline-time.monotonic())), check=True).stdout.decode("utf-8", errors="replace").strip()
                if native:
                    chunks.append(f"PDF page {page} text:\n" + native)
                # A short text layer can be just a footer on a scanned report.
                if len(re.findall(r"[A-Za-z0-9]", native)) < 80:
                    prefix = Path(folder) / ("page-" + str(page))
                    subprocess.run(["pdftoppm", "-f", str(page), "-l", str(page), "-scale-to", "2200", "-singlefile", "-png", str(path), str(prefix)],
                                   capture_output=True, timeout=min(20, max(1, deadline-time.monotonic())), check=True)
                    text = ocr_file(str(prefix) + ".png", timeout=min(30, max(1, deadline-time.monotonic())))
                    used_ocr = True
                    if text.strip():
                        chunks.append(f"PDF page {page} OCR (unverified):\n" + text.strip())
                    elif not native:
                        partial = True
            except (subprocess.SubprocessError, OSError):
                partial = True
    text = "\n\n".join(chunks)
    if not text.strip():
        return "", "pdf_empty"
    if partial or len(text) > 30000:
        return text[:30000], "pdf_partial"
    return text, "pdf_ocr_text" if used_ocr else "pdf_text"


async def extract_media(client, message, protected):
    image = bool(message.photo) or bool(message.document and (message.document.mime_type or "").startswith("image/"))
    pdf = bool(message.document and (message.document.mime_type or "").lower() == "application/pdf")
    if not image and not pdf:
        return "", "link_or_text" if not message.document else "unsupported_document"
    if protected or getattr(message, "noforwards", False):
        return "", "protected_media_not_downloaded"
    if pdf and not all(shutil.which(tool) for tool in ("pdfinfo", "pdftotext", "pdftoppm", "tesseract")):
        return "", "pdf_tools_unavailable"
    if image and not shutil.which("tesseract"):
        return "", "ocr_unavailable"
    if getattr(message.file, "size", 0) and message.file.size > 15 * 1024 * 1024:
        return "", "pdf_too_large" if pdf else "image_too_large"
    with tempfile.TemporaryDirectory(prefix="coa-") as folder:
        path = await client.download_media(message, file=folder + "/")
        if not path:
            return "", "download_unavailable"
        if Path(path).stat().st_size > 15 * 1024 * 1024:
            return "", "pdf_too_large" if pdf else "image_too_large"
        try:
            if pdf:
                return await asyncio.to_thread(extract_pdf, path)
            text = await asyncio.to_thread(ocr_file, path)
            return text, "ocr_text" if text.strip() else "ocr_empty"
        except (subprocess.SubprocessError, OSError):
            return "", "ocr_failed"


async def save_message(db, client, source, message, generation, protected, metadata_only=False):
    ensure_active(db, source)
    if not in_topic(message, source["topic"]) or getattr(message, "action", None):
        return
    old = db.execute("SELECT fingerprint,ocr,media_status FROM messages WHERE chat=? AND id=?", (source["chat"], message.id)).fetchone()
    fp = fingerprint(message)
    retry = old and (old["media_status"] in ("ocr_unavailable", "ocr_failed", "ocr_empty", "download_unavailable", "protected_media_not_downloaded", "pdf_tools_unavailable", "pdf_failed", "pdf_empty")
                     or (old["media_status"] == "unsupported_document" and message.document and message.document.mime_type == "application/pdf"))
    if old and old["fingerprint"] == fp and not retry:
        with db:
            db.execute("UPDATE messages SET generation=?,deleted=0 WHERE chat=? AND id=?", (generation, source["chat"], message.id))
        return
    if metadata_only:
        text, status = (old["ocr"], old["media_status"]) if old and old["fingerprint"] == fp else ("", "link_metadata_only")
    else:
        text, status = await extract_media(client, message, protected)
    caption = (message.message or "")[:30000]
    links = message_links(message)
    with db:
        db.execute("""INSERT INTO messages VALUES (?,?,?,?,?,?,?,?,?,?,?,0)
          ON CONFLICT(chat,id) DO UPDATE SET topic=excluded.topic,album=excluded.album,
          date=excluded.date,fingerprint=excluded.fingerprint,caption=excluded.caption,
          ocr=excluded.ocr,media_status=excluded.media_status,links=excluded.links,
          generation=excluded.generation,deleted=0""",
          (source["chat"], message.id, source["topic"], str(message.grouped_id or ""),
           int(message.date.timestamp()), fp, caption, text, status,
           json.dumps(sorted(set(links))), generation))


async def sync_recent_links(db, client, source):
    """Only inspect the newest 200 posts; never traverse older history or media."""
    kwargs = {} if source["topic"] == 1 else {"reply_to": source["topic"]}
    highest = source["recent_cursor"]
    async for message in client.iter_messages(source["chat"], limit=200, **kwargs):
        ensure_active(db, source)
        if not in_topic(message, source["topic"]):
            continue
        highest = max(highest, message.id)
        if has_lab_link(message_links(message)):
            await save_message(db, client, source, message, source["generation"], True, metadata_only=True)
    ensure_active(db, source)
    now = int(time.time())
    with db:
        db.execute("UPDATE sources SET cutoff=0,cursor=0,completed_at=?,last_sync=?,recent_cursor=?,error='' WHERE chat=? AND topic=?", (now, now, highest, source["chat"], source["topic"]))


async def sync_source(db, client, source, batch):
    ensure_active(db, source)
    if mode(db, source["chat"], source["topic"]) == "recent_links":
        return await sync_recent_links(db, client, source)
    chat, topic = source["chat"], source["topic"]
    entity = await client.get_entity(chat)
    protected = bool(getattr(entity, "noforwards", False))
    protection_key = f"protection:{chat}:{topic}"
    prior_protection = db.execute("SELECT value FROM state WHERE key=?", (protection_key,)).fetchone()
    if prior_protection and prior_protection[0] == "1" and not protected:
        # Opening an authorized read window retries old protected files, not just
        # the newest posts. Only catalogue scan cursors change; never receipts.
        with db:
            db.execute("UPDATE sources SET completed_at=0,cutoff=0,cursor=0 WHERE chat=? AND topic=?", (chat, topic))
        source = db.execute("SELECT * FROM sources WHERE chat=? AND topic=?", (chat, topic)).fetchone()
    with db:
        db.execute("INSERT OR REPLACE INTO state VALUES (?,?)", (protection_key, "1" if protected else "0"))
    kwargs = {} if topic == 1 else {"reply_to": topic}
    # Keep recent posts available while the historical pass is still in progress.
    recent_options = {"reverse": True, "min_id": source["recent_cursor"], "limit": batch} if source["recent_cursor"] else {"limit": 30}
    highwater = source["recent_cursor"]
    recent_started = time.monotonic()
    recent_budget_hit = False
    async for message in client.iter_messages(chat, **recent_options, **kwargs):
        await save_message(db, client, source, message, source["generation"], protected)
        highwater = max(highwater, message.id)
        if source["recent_cursor"]:
            with db:
                db.execute("UPDATE sources SET recent_cursor=? WHERE chat=? AND topic=?", (highwater, chat, topic))
        if time.monotonic() - recent_started >= 180:
            recent_budget_hit = True
            break
    with db:
        db.execute("UPDATE sources SET recent_cursor=? WHERE chat=? AND topic=?", (highwater, chat, topic))
    now = int(time.time())
    if recent_budget_hit:
        with db:
            db.execute("UPDATE sources SET last_sync=?,error='' WHERE chat=? AND topic=?", (now, chat, topic))
        return
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
    started = time.monotonic()
    budget_hit = False
    async for message in client.iter_messages(chat, reverse=True, min_id=source["cursor"], max_id=source["cutoff"] + 1, limit=batch, **kwargs):
        await save_message(db, client, source, message, source["generation"], protected)
        with db:
            db.execute("UPDATE sources SET cursor=? WHERE chat=? AND topic=?", (message.id, chat, topic))
        count += 1
        if time.monotonic() - started >= 240:
            budget_hit = True
            break
    with db:
        if count < batch and not budget_hit:
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
        version = db.execute("SELECT value FROM state WHERE key='extractor_version'").fetchone()
        if not version or version[0] != "pdf-v1":
            with db:
                db.execute("UPDATE sources SET completed_at=0,cutoff=0,cursor=0 WHERE enabled=1")
                db.execute("INSERT OR REPLACE INTO state VALUES ('extractor_version','pdf-v1')")
        sources = [s for s in db.execute("SELECT * FROM sources WHERE enabled=1").fetchall() if not paused(db, s["chat"], s["topic"])]
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
            except SourcePaused:
                log.info("COA sync paused topic=%s; existing records preserved", source["topic"])
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
