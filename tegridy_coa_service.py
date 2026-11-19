"""COA catalogue lifecycle, authenticated search and private admin controls."""
import asyncio
import hmac
import json
import logging
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from tegridy_coa_store import connect, label, search
from tegridy_coa_scope import mode as source_mode, paused as source_paused, set_mode, set_paused, seed, has_lab_link

log = logging.getLogger("tegridy-coas")


def handler(token):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # Never put query strings, tokens or source text into HTTP logs.

        def send_json(self, status, body):
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path == "/health":
                self.send_json(200, {"ok": True})
                return
            if not hmac.compare_digest(self.headers.get("Authorization", ""), "Bearer " + token):
                self.send_json(401, {"error": "Unauthorized"})
                return
            parts = urlsplit(self.path)
            if parts.path != "/v1/coas/search":
                self.send_json(404, {"error": "Not found"})
                return
            if len(self.path) > 1500:
                self.send_json(400, {"error": "Query too long"})
                return
            query = parse_qs(parts.query)
            try:
                db = connect()
                try:
                    result = search(db, query.get("q", [""])[0], query.get("gb", [""])[0])
                finally:
                    db.close()
                self.send_json(200, result)
            except Exception:
                self.send_json(503, {"error": "Catalogue temporarily unavailable"})
    return Handler


async def sync_loop(jobs):
    interval = max(60, int(os.getenv("COA_SYNC_INTERVAL", "120")))
    while True:
        if not jobs.running():
            db = connect()
            try:
                active = db.execute("SELECT 1 FROM sources WHERE enabled=1 LIMIT 1").fetchone()
            finally:
                db.close()
            if active:
                process = await asyncio.create_subprocess_exec(sys.executable, "-u", str(Path(__file__).with_name("tegridy_coa_sync.py")))
                try:
                    await asyncio.wait_for(process.wait(), timeout=600)
                except (asyncio.TimeoutError, asyncio.CancelledError) as exc:
                    process.terminate()
                    await process.wait()
                    if isinstance(exc, asyncio.CancelledError):
                        raise
                if process.returncode:
                    log.warning("COA sync deferred or failed; existing backup lock and checkpoints preserved")
        await asyncio.sleep(interval)


def panel():
    from telethon import Button
    db = connect()
    try:
        sources = db.execute("SELECT * FROM sources WHERE enabled=1").fetchall()
        total = db.execute("SELECT count(*) FROM messages WHERE deleted=0").fetchone()[0]
        lines = ["COA catalogue", f"Indexed messages: {total}",
                 "Reads selected source topics; does not forward posts."]
        supplied = db.execute("SELECT count(*) FROM media_imports WHERE enabled=1").fetchone()[0]
        lines.append(f"Privately supplied originals: {supplied} · /coa_import for instructions")
        registered = db.execute("SELECT count(*) FROM report_links WHERE enabled=1").fetchone()[0]
        lines.append(f"Registered lab links: {registered} · /coa_links for batch registration")
        controls = []
        for s in sources:
            state = "history loading" if s["cutoff"] or not s["completed_at"] else "history loaded"
            paused_now = source_paused(db, s["chat"], s["topic"])
            current_mode = source_mode(db, s["chat"], s["topic"])
            if paused_now:
                state = "PAUSED"
            elif current_mode == "recent_links":
                state = "recent links only (last 200 posts)"
            last = time.strftime("%d %b %H:%M UTC", time.gmtime(s["last_sync"])) if s["last_sync"] else "not yet"
            lines.append(f"\n{s['title']} ({s['topic']}) · {state}\nLast sync: {last}" + ("\n" + s["error"] if s["error"] else ""))
            coverage = db.execute("""SELECT count(*),coalesce(sum(media_status='ocr_text'),0),
                coalesce(sum(media_status IN ('ocr_empty','ocr_failed','ocr_unavailable','download_unavailable','protected_media_not_downloaded','image_too_large','unsupported_document','pdf_tools_unavailable','pdf_restricted','pdf_failed','pdf_empty','pdf_too_large')),0),
                coalesce(sum(media_status IN ('pdf_text','pdf_ocr_text')),0),coalesce(sum(media_status='pdf_partial'),0)
                FROM messages WHERE chat=? AND topic=? AND deleted=0""", (s["chat"], s["topic"])).fetchone()
            lines.append(f"Posts: {coverage[0]} · Images read: {coverage[1]} · PDFs read: {coverage[3]}\nUnreadable/protected media: {coverage[2]} · Partial PDFs: {coverage[4]}")
            if current_mode == "recent_links":
                eligible = sum(has_lab_link(r[0]) for r in db.execute("SELECT links FROM messages WHERE chat=? AND topic=? AND deleted=0", (s["chat"], s["topic"])))
                lines.append(f"Lab-link posts stored: {eligible}. Old records retained; ordinary updates excluded from lookup.")
            title = s["title"][:18]
            controls.append([Button.inline(("Resume " if paused_now else "Pause ") + title, f"coa:pause:{s['topic']}".encode())])
            controls.append([Button.inline("Recent lab links", f"coa:mode:{s['topic']}:recent_links".encode()),
                             Button.inline("Full history", f"coa:mode:{s['topic']}:full".encode())])
        if not sources:
            lines.append("\nChoose the COA topic to begin. No topic is selected automatically.")
        lines.append("\nImages use OCR when content can be downloaded. Unreadable or unlabelled posts may need an admin label.")
        return "\n".join(lines), controls + [[Button.inline("Choose source topics", b"coa:topics:0")],
                                  [Button.inline("Import originals", b"coa:import")],
                                  [Button.inline("Refresh", b"coa:menu"), Button.inline("Backup menu", b"tg:menu")]]
    finally:
        db.close()


def topic_panel(page):
    from telethon import Button
    from tegridy_state import connect as backup_connect, SRC
    db = backup_connect()
    try:
        rows = db.execute("SELECT topic,title FROM topic_inventory WHERE chat=? ORDER BY title", (SRC,)).fetchall()
    finally:
        db.close()
    coa = connect()
    try:
        enabled = {r[0] for r in coa.execute("SELECT topic FROM sources WHERE enabled=1")}
    finally:
        coa.close()
    buttons = [[Button.inline(("ON · " if topic in enabled else "OFF · ") + title[:32], f"coa:toggle:{topic}:{page}".encode())] for topic, title in rows[page*8:page*8+8]]
    nav = []
    if page:
        nav.append(Button.inline("Previous", f"coa:topics:{page-1}".encode()))
    if (page+1)*8 < len(rows):
        nav.append(Button.inline("Next", f"coa:topics:{page+1}".encode()))
    if nav:
        buttons.append(nav)
    buttons.append([Button.inline("Catalogue", b"coa:menu")])
    return ("Select only the COA source topic(s). ON begins read-only indexing.\nIf the inventory is empty, use Sync Topics in the backup menu first.", buttons)


def install_controls(client, authorized):
    from telethon import events
    from tegridy_state import connect as backup_connect, SRC

    @client.on(events.CallbackQuery(pattern=b"^coa:"))
    async def callbacks(event):
        if not event.is_private or event.sender_id not in authorized:
            await event.answer("Not authorized", alert=True)
            return
        await event.answer()
        parts = event.data.decode().split(":")
        if parts[1] in ("pause", "mode"):
            topic = int(parts[2])
            db = connect()
            try:
                if not db.execute("SELECT 1 FROM sources WHERE chat=? AND topic=?", (SRC, topic)).fetchone():
                    return
                if parts[1] == "pause":
                    set_paused(db, SRC, topic, not source_paused(db, SRC, topic))
                    log.info("COA source topic=%s paused=%s", topic, source_paused(db, SRC, topic))
                else:
                    set_mode(db, SRC, topic, parts[3])
            finally:
                db.close()
            text, buttons = panel()
        elif parts[1] == "toggle":
            topic, page = int(parts[2]), max(0, int(parts[3]))
            source = backup_connect()
            try:
                row = source.execute("SELECT title FROM topic_inventory WHERE chat=? AND topic=?", (SRC, topic)).fetchone()
            finally:
                source.close()
            if not row:
                return
            db = connect()
            try:
                with db:
                    db.execute("INSERT INTO sources(chat,topic,title) VALUES (?,?,?) ON CONFLICT(chat,topic) DO UPDATE SET enabled=1-enabled,title=excluded.title", (SRC, topic, row[0]))
            finally:
                db.close()
            text, buttons = topic_panel(page)
        elif parts[1] == "topics":
            text, buttons = topic_panel(max(0, int(parts[2])))
        elif parts[1] == "import":
            from telethon import Button
            from tegridy_coa_intake import HELP
            text, buttons = HELP, [[Button.inline("Catalogue", b"coa:menu")]]
        else:
            text, buttons = panel()
        from telethon.errors import MessageNotModifiedError
        try:
            await event.edit(text[:3900], buttons=buttons, parse_mode=None)
        except MessageNotModifiedError:
            pass

    @client.on(events.NewMessage(incoming=True))
    async def commands(event):
        if not event.is_private or event.sender_id not in authorized:
            return
        command, _, value = (event.raw_text or "").partition(" ")
        if command.split("@")[0].lower() == "/coas":
            text, buttons = panel()
            await event.respond(text[:3900], buttons=buttons, parse_mode=None)
        elif command.split("@")[0].lower() == "/coa_label":
            db = connect()
            try:
                message, gb, product = [s.strip() for s in value.split("|", 2)]
                label(db, SRC, int(message), gb, product, event.sender_id)
                await event.respond("COA label saved.")
            except ValueError:
                await event.respond("Use /coa_label message_id | GB | product. The message must already be indexed.")
            finally:
                db.close()


def start(client, jobs, authorized):
    from tegridy_state import SRC
    db = connect()
    try:
        seed(db, SRC, os.getenv("COA_INITIAL_PAUSE_TOPICS", ""), os.getenv("COA_RECENT_LINK_TOPICS", ""))
        for source in db.execute("SELECT chat,topic,enabled FROM sources"):
            log.info("COA source topic=%s enabled=%s paused=%s mode=%s", source["topic"], bool(source["enabled"]),
                     source_paused(db, source["chat"], source["topic"]), source_mode(db, source["chat"], source["topic"]))
    finally:
        db.close()
    install_controls(client, authorized)
    from tegridy_coa_intake import install as install_intake
    install_intake(client, authorized)
    token = os.getenv("COA_API_TOKEN", "")
    server = None
    if len(token) >= 32:
        server = ThreadingHTTPServer(("0.0.0.0", int(os.getenv("PORT", "8080"))), handler(token))
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        log.info("Authenticated COA catalogue API started")
    else:
        log.info("COA API disabled until a private token of at least 32 characters is configured")
    task = asyncio.create_task(sync_loop(jobs))
    return task, server

