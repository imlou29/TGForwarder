"""Original COA files supplied privately by existing authorized admins only."""
import asyncio
import hashlib
import logging
import re
import tempfile
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from tegridy_coa_store import add_import, connect, source_post, telegram_link
from tegridy_state import SRC

log = logging.getLogger("tegridy-coas")
HELP = ("Send an original PDF/image privately with this caption:\n"
        "GB | product | original Telegram message link\n\n"
        "Example format: Lobster | RT10 | https://t.me/c/…\n"
        "The original post must already be in the COA catalogue. A message ID can replace the link. "
        "Only authorized admins can import. Files are read locally and are not forwarded to the group or backup. "
        "Upload originals supplied by the publisher/lab; do not forward protected media.\n\n"
        "Remove an incorrect import with /coa_remove import_id. "
        "GB/product labels can be corrected separately with /coa_label.")


def parse_caption(caption):
    parts = [p.strip() for p in (caption or "").split("|")]
    if len(parts) != 3 or not parts[0] or not parts[1] or len(parts[0]) > 100 or len(parts[1]) > 200:
        raise ValueError("Caption must be: GB | product | original Telegram message link")
    gb, product, reference = parts
    topic = None
    if reference.isascii() and reference.isdigit():
        chat, mid = SRC, int(reference)
    else:
        url = urlsplit(reference)
        if url.scheme != "https" or url.netloc != "t.me":
            raise ValueError("Use the original https://t.me/c/… link")
        match = re.fullmatch(r"/c/(\d+)/(\d+)(?:/(\d+))?/?", url.path)
        if not match:
            raise ValueError("Use the original private-group message link")
        chat = int("-100" + match[1])
        if match[3]:
            topic, mid = int(match[2]), int(match[3])
        else:
            mid = int(match[2])
            thread = parse_qs(url.query).get("thread", [None])[0]
            if thread is not None:
                if not thread.isdigit():
                    raise ValueError("Invalid topic in source link")
                topic = int(thread)
    if chat != SRC or mid <= 0:
        raise ValueError("Source must be the configured Tegridy group")
    return gb, product, chat, topic, mid


def validate_source(db, caption):
    gb, product, chat, topic, mid = parse_caption(caption)
    row = source_post(db, chat, mid)
    if row is None or (topic is not None and row["topic"] != topic):
        raise ValueError("Original post not found in an enabled COA source; check the link or wait for sync")
    return gb, product, chat, mid


def install(client, authorized):
    from telethon import events
    guard = asyncio.Lock()
    pending = 0

    @client.on(events.NewMessage(incoming=True))
    async def intake(event):
        nonlocal pending
        if not event.is_private or event.sender_id not in authorized:
            return
        raw = event.raw_text or ""
        parts = raw.split(maxsplit=1)
        command = parts[0] if parts else ""
        value = parts[1] if len(parts) > 1 else ""
        command = command.split("@")[0].lower()
        if command == "/coa_supplied":
            from tegridy_coa_supplied import HELP as SUPPLIED_HELP, register_supplied
            if not value.strip():
                await event.respond(SUPPLIED_HELP, parse_mode=None)
                return
            db = connect()
            try:
                results = register_supplied(db, value, event.sender_id)
                await event.respond("Registered " + str(len(results)) + " admin-supplied lab links. No Telegram source claimed; lab results not read or verified.\n" +
                                    "\n".join(f"Supplied #{r['id']} · {r['product']} · {r['kind']}" for r in results), parse_mode=None)
            except ValueError as exc:
                await event.respond(str(exc), parse_mode=None)
            finally:
                db.close()
            return
        if command == "/coa_supplied_remove":
            db = connect()
            try:
                if not value.strip().isdigit():
                    await event.respond("Use /coa_supplied_remove supplied_registration_id")
                    return
                with db:
                    changed = db.execute("UPDATE admin_report_links SET enabled=0 WHERE id=?", (int(value),)).rowcount
                await event.respond("Supplied link removed." if changed else "Supplied registration not found.")
            finally:
                db.close()
            return
        if command == "/coa_links":
            from tegridy_coa_links import HELP as LINK_HELP, register_batch
            if not value.strip():
                await event.respond(LINK_HELP, parse_mode=None)
                return
            db = connect()
            try:
                results = register_batch(db, value, event.sender_id)
                await event.respond("Registered " + str(len(results)) + " report links. Lab results have not been read or verified.\n" +
                                    "\n".join(f"#{r['id']} · {r['product']} · {r['kind']}" for r in results), parse_mode=None)
            except ValueError as exc:
                await event.respond(str(exc), parse_mode=None)
            finally:
                db.close()
            return
        if command == "/coa_unlink":
            db = connect()
            try:
                if not value.strip().isdigit():
                    await event.respond("Use /coa_unlink registration_id")
                    return
                with db:
                    changed = db.execute("UPDATE report_links SET enabled=0 WHERE id=?", (int(value),)).rowcount
                await event.respond("Link registration removed." if changed else "Registration not found.")
            finally:
                db.close()
            return
        if command == "/coa_import":
            await event.respond(HELP, parse_mode=None)
            return
        if command == "/coa_remove":
            db = connect()
            try:
                if not value.strip().isdigit():
                    await event.respond("Use /coa_remove import_id")
                    return
                with db:
                    changed = db.execute("UPDATE media_imports SET enabled=0 WHERE id=?", (int(value),)).rowcount
                await event.respond("Import removed from lookup. Its source label is retained; use /coa_label if it needs correction." if changed else "Import not found.")
            finally:
                db.close()
            return
        message = event.message
        if not (message.photo or message.document):
            return
        if getattr(message, "noforwards", False):
            await event.respond("Please supply an original file rather than protected media.")
            return
        mime = (getattr(message.document, "mime_type", "") or "").lower()
        image = bool(message.photo) or mime.startswith("image/")
        if not image and mime != "application/pdf":
            await event.respond("Original PDFs and images are supported. Send /coa_import for instructions.")
            return
        if getattr(message.file, "size", 0) and message.file.size > 15 * 1024 * 1024:
            await event.respond("File exceeds the 15 MiB import limit.")
            return
        db = connect()
        try:
            metadata = validate_source(db, raw)
        except ValueError as exc:
            await event.respond(str(exc) + "\nSend /coa_import for the format.", parse_mode=None)
            return
        finally:
            db.close()
        if pending >= 10:
            await event.respond("The import queue is full. Please resend after the current files finish.")
            return
        pending += 1
        try:
            async with guard:
                from tegridy_coa_sync import extract_pdf, ocr_file
                with tempfile.TemporaryDirectory(prefix="coa-original-") as folder:
                    downloaded = await asyncio.wait_for(client.download_media(message, file=folder + "/"), timeout=90)
                    if not downloaded or Path(downloaded).stat().st_size > 15 * 1024 * 1024:
                        await event.respond("The original could not be read or exceeds 15 MiB.")
                        return
                    if mime == "application/pdf":
                        text, status = await asyncio.to_thread(extract_pdf, downloaded)
                    else:
                        text = await asyncio.to_thread(ocr_file, downloaded)
                        status = "ocr_text" if text.strip() else "ocr_empty"
                    digest = hashlib.sha256(Path(downloaded).read_bytes()).hexdigest()
                    filename = Path(downloaded).name
                    db = connect()
                    try:
                        gb, product, chat, mid = metadata
                        import_id = add_import(db, chat, mid, gb, product, text, status, digest, filename,
                                               mime or "image/jpeg", event.sender_id, message.id)
                        original = source_post(db, chat, mid)
                        original_link = telegram_link(chat, original["topic"], mid)
                    finally:
                        db.close()
                await event.respond(f"Original indexed · import #{import_id}\n{gb} | {product}\nLinked to original post {mid}." +
                                    "\n" + original_link +
                                    ("\nPDF was only partially read; review the original." if status == "pdf_partial" else ""), parse_mode=None)
                log.info("COA private original indexed import=%s source_message=%s extraction=%s", import_id, mid, status)
        except ValueError as exc:
            await event.respond(str(exc), parse_mode=None)
        except Exception as exc:
            log.warning("COA private import failed: %s", type(exc).__name__)
            await event.respond("The original could not be indexed. Please retry later or check the file.")
        finally:
            pending -= 1

