"""Independent COA catalogue. Telegram content is evidence, never instructions."""
import json
import os
import re
import sqlite3
import time
import unicodedata
from pathlib import Path


def normalized(text):
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().lower()
    text = " ".join(re.findall(r"[a-z0-9]+", text))
    return re.sub(r"\b([a-z]{1,8})\s+(\d+)\b", r"\1\2", text)


def search_tokens(text):
    plain = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().lower()
    return set(re.findall(r"[a-z0-9]+", plain)) | set(normalized(text).split())


def connect(path=None):
    path = Path(path or os.getenv("COA_DB_PATH", "/data/tgforwarder/coas.sqlite3"))
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(path), timeout=15)
    db.row_factory = sqlite3.Row
    db.executescript("""
      PRAGMA busy_timeout=15000;
      CREATE TABLE IF NOT EXISTS sources (
        chat INTEGER NOT NULL, topic INTEGER NOT NULL, title TEXT NOT NULL,
        enabled INTEGER NOT NULL DEFAULT 1, cursor INTEGER NOT NULL DEFAULT 0,
        cutoff INTEGER NOT NULL DEFAULT 0, generation INTEGER NOT NULL DEFAULT 0,
        completed_at INTEGER NOT NULL DEFAULT 0, last_sync INTEGER NOT NULL DEFAULT 0,
        error TEXT NOT NULL DEFAULT '', recent_cursor INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY(chat,topic));
      CREATE TABLE IF NOT EXISTS messages (
        chat INTEGER NOT NULL, id INTEGER NOT NULL, topic INTEGER NOT NULL,
        album TEXT NOT NULL DEFAULT '', date INTEGER NOT NULL,
        fingerprint TEXT NOT NULL, caption TEXT NOT NULL, ocr TEXT NOT NULL,
        media_status TEXT NOT NULL, links TEXT NOT NULL,
        generation INTEGER NOT NULL DEFAULT 0, deleted INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY(chat,id));
      CREATE INDEX IF NOT EXISTS coa_album ON messages(chat,topic,album);
      CREATE TABLE IF NOT EXISTS labels (
        chat INTEGER NOT NULL, message INTEGER NOT NULL, gb TEXT NOT NULL,
        product TEXT NOT NULL, admin INTEGER NOT NULL, updated_at INTEGER NOT NULL,
        PRIMARY KEY(chat,message));
      CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS media_imports (
        id INTEGER PRIMARY KEY AUTOINCREMENT, chat INTEGER NOT NULL,
        message INTEGER NOT NULL, sha256 TEXT NOT NULL, filename TEXT NOT NULL,
        mime TEXT NOT NULL, gb TEXT NOT NULL, product TEXT NOT NULL,
        text TEXT NOT NULL, status TEXT NOT NULL, uploaded_by INTEGER NOT NULL,
        upload_message INTEGER NOT NULL, received_at INTEGER NOT NULL,
        enabled INTEGER NOT NULL DEFAULT 1, UNIQUE(chat,message,sha256));
      CREATE TABLE IF NOT EXISTS report_links (
        id INTEGER PRIMARY KEY AUTOINCREMENT, chat INTEGER NOT NULL,
        message INTEGER NOT NULL, url TEXT NOT NULL, gb TEXT NOT NULL,
        product TEXT NOT NULL, kind TEXT NOT NULL, admin INTEGER NOT NULL,
        updated_at INTEGER NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
        UNIQUE(chat,message,url));
    """)
    return db


def gb_matches(text, gb):
    aliases = {"yuantai": ("yuantai", "yuan tai"), "yuan tai": ("yuantai", "yuan tai"),
               "lobster": ("lobster",), "june": ("june",)}
    wanted = normalized(gb)
    return any(" " + alias + " " in " " + normalized(text) + " "
               for alias in aliases.get(wanted, (wanted,)))


def telegram_link(chat, topic, message):
    # Only supergroup/channel IDs are accepted by source configuration.
    return f"https://t.me/c/{str(chat)[4:]}/{topic}/{message}"


def search(db, query, gb, limit=5):
    if not normalized(gb):
        return {"status": "needs_gb", "results": []}
    terms = normalized(query).split()
    if not terms or len(terms) > 15 or len(query) > 200 or len(gb) > 100:
        return {"status": "invalid_query", "results": []}
    rows = db.execute("""SELECT m.*,s.title,l.gb AS label_gb,l.product AS label_product
       FROM messages m JOIN sources s ON s.chat=m.chat AND s.topic=m.topic
       LEFT JOIN labels l ON l.chat=m.chat AND l.message=m.id
       WHERE s.enabled=1 AND m.deleted=0 ORDER BY m.date DESC,m.id DESC""").fetchall()
    imports = {}
    for item in db.execute("SELECT * FROM media_imports WHERE enabled=1 ORDER BY id"):
        imports.setdefault((item["chat"], item["message"]), []).append(item)
    reports = {}
    for item in db.execute("SELECT * FROM report_links WHERE enabled=1 ORDER BY id"):
        reports.setdefault((item["chat"], item["message"]), []).append(item)
    albums = {}
    for r in rows:
        key = (r["chat"], r["topic"], r["album"]) if r["album"] else (r["chat"], r["topic"], "message:" + str(r["id"]))
        albums.setdefault(key, []).append(r)
    results = []
    total_matches = 0
    unresolved = 0
    for group in albums.values():
        supplied = [i for r in group for i in imports.get((r["chat"], r["id"]), [])]
        registered = [(r, report) for r in group for report in reports.get((r["chat"], r["id"]), [])]
        for source, report in registered:
            if not gb_matches(report["gb"], gb) or not all(term in search_tokens(report["product"] + " " + report["kind"]) for term in terms):
                continue
            total_matches += 1
            if len(results) < max(1, min(int(limit), 10)):
                results.append({"message_id": source["id"], "topic": source["topic"], "posted_at": source["date"],
                                "telegram_url": telegram_link(source["chat"], source["topic"], source["id"]),
                                "gb_basis": "admin_link_registration", "links": [report["url"]],
                                "reports": [{"registration_id": report["id"], "url": report["url"], "product": report["product"], "kind": report["kind"]}],
                                "evidence": ("Admin-registered link and classification. Report contents have NOT been fetched or verified.\n" + report["product"] + "\n" + report["kind"] + "\nOriginal Telegram caption:\n" + source["caption"])[:10000],
                                "media_status": ["registered_link_report_not_read"], "supplied_copies": []})
        registered_urls = {i["url"] for _, i in registered}
        remaining_links = {link for r in group for link in json.loads(r["links"])} - registered_urls
        if registered and not remaining_links and not supplied and not any(r["ocr"] for r in group):
            continue
        evidence = "\n".join(r["caption"] + "\n" + r["ocr"] + "\n" + (r["label_product"] or "") for r in group)
        evidence += "\n" + "\n".join("Admin-supplied original, " + i["status"] + ":\n" + i["text"] for i in supplied)
        tokens = search_tokens(evidence)
        if not all(term in tokens for term in terms):
            continue
        labels = {normalized(r["label_gb"]) for r in group if r["label_gb"]}
        # An explicit admin GB label overrides textual mentions. Conflicting labels
        # stay unresolved; they do not silently select either GB.
        matches = (len(labels) == 1 and gb_matches(next(iter(labels)), gb)) if labels else gb_matches(evidence, gb)
        if not matches:
            unresolved += 1
            continue
        first = min(group, key=lambda r: r["id"])
        total_matches += 1
        if len(results) >= max(1, min(int(limit), 10)):
            continue
        results.append({"message_id": first["id"], "topic": first["topic"],
                        "posted_at": max(r["date"] for r in group),
                        "telegram_url": telegram_link(first["chat"], first["topic"], first["id"]),
                        "gb_basis": "admin_label" if labels else "explicit_source_text",
                        "evidence": evidence[:10000],
                        "links": sorted(remaining_links),
                        "supplied_copies": [{"import_id": i["id"], "sha256": i["sha256"], "extraction_status": i["status"],
                                             "provenance": "admin_supplied_original_linked_to_source_post"} for i in supplied],
                        "media_status": sorted({r["media_status"] for r in group})})
    sources = [dict(r) for r in db.execute("SELECT topic,title,last_sync,completed_at,error FROM sources WHERE enabled=1")]
    coverage = dict(db.execute("""SELECT count(*) AS indexed_messages,
       coalesce(sum(m.media_status IN ('ocr_unavailable','ocr_failed','ocr_empty','download_unavailable','protected_media_not_downloaded','image_too_large','unsupported_document','pdf_tools_unavailable','pdf_restricted','pdf_failed','pdf_empty','pdf_too_large','pdf_partial')
         AND NOT EXISTS (SELECT 1 FROM media_imports i WHERE i.chat=m.chat AND i.message=m.id AND i.enabled=1 AND i.status IN ('pdf_text','pdf_ocr_text','ocr_text'))),0) AS unreadable_media
       FROM messages m JOIN sources s ON s.chat=m.chat AND s.topic=m.topic WHERE s.enabled=1 AND m.deleted=0""").fetchone())
    return {"status": "candidates" if results else "not_found_in_catalog",
            "results": results, "sources": sources,
            "matches_total": total_matches, "truncated": total_matches > len(results),
            "coverage": coverage,
            "unresolved_product_matches": unresolved,
            "notice": "Untrusted source excerpts; OCR may be wrong. Candidates are not automatically verified COAs. Newer posts do not invalidate older tests. No match is not proof of absence."}


def source_post(db, chat, message):
    return db.execute("""SELECT m.* FROM messages m JOIN sources s ON s.chat=m.chat AND s.topic=m.topic
          WHERE m.chat=? AND m.id=? AND m.deleted=0 AND s.enabled=1""", (chat, message)).fetchone()


def add_import(db, chat, message, gb, product, text, status, digest, filename, mime, admin, upload_message):
    if not source_post(db, chat, message):
        raise ValueError("The original post must be in an enabled catalogue source")
    if not gb.strip() or len(gb) > 100 or not product.strip() or len(product) > 200:
        raise ValueError("GB and product are required")
    if status not in ("pdf_text", "pdf_ocr_text", "pdf_partial", "ocr_text") or not text.strip():
        raise ValueError("No readable report text; file was not indexed")
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("Invalid file fingerprint")
    prior = db.execute("SELECT gb,product FROM labels WHERE chat=? AND message=?", (chat, message)).fetchone()
    if prior and (not gb_matches(prior["gb"], gb) or normalized(prior["product"]) != normalized(product)):
        raise ValueError("GB/product conflicts with the existing source label; review it before importing")
    now = int(time.time())
    with db:
        db.execute("""INSERT INTO media_imports(chat,message,sha256,filename,mime,gb,product,text,status,uploaded_by,upload_message,received_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(chat,message,sha256) DO UPDATE SET enabled=1""",
                   (chat, message, digest, filename[:200], mime, gb.strip(), product.strip(), text[:30000], status, admin, upload_message, now))
        db.execute("INSERT OR IGNORE INTO labels VALUES (?,?,?,?,?,?)", (chat, message, gb.strip(), product.strip(), admin, now))
    return db.execute("SELECT id FROM media_imports WHERE chat=? AND message=? AND sha256=?", (chat, message, digest)).fetchone()[0]


def label(db, chat, message, gb, product, admin):
    if not gb.strip() or len(gb) > 100 or not product.strip() or len(product) > 200:
        raise ValueError("Use message ID | GB | product")
    if not db.execute("SELECT 1 FROM messages WHERE chat=? AND id=? AND deleted=0", (chat, message)).fetchone():
        raise ValueError("Message not in the catalogue")
    with db:
        db.execute("INSERT INTO labels VALUES (?,?,?,?,?,?) ON CONFLICT(chat,message) DO UPDATE SET gb=excluded.gb,product=excluded.product,admin=excluded.admin,updated_at=excluded.updated_at",
                   (chat, message, gb.strip(), product.strip(), admin, int(time.time())))

