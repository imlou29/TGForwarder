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
    albums = {}
    for r in rows:
        key = (r["chat"], r["topic"], r["album"]) if r["album"] else (r["chat"], r["topic"], "message:" + str(r["id"]))
        albums.setdefault(key, []).append(r)
    results = []
    total_matches = 0
    unresolved = 0
    for group in albums.values():
        evidence = "\n".join(r["caption"] + "\n" + r["ocr"] + "\n" + (r["label_product"] or "") for r in group)
        tokens = set(normalized(evidence).split())
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
                        "links": sorted({link for r in group for link in json.loads(r["links"])}),
                        "media_status": sorted({r["media_status"] for r in group})})
    sources = [dict(r) for r in db.execute("SELECT topic,title,last_sync,completed_at,error FROM sources WHERE enabled=1")]
    coverage = dict(db.execute("""SELECT count(*) AS indexed_messages,
       coalesce(sum(m.media_status IN ('ocr_unavailable','ocr_failed','ocr_empty','download_unavailable','protected_media_not_downloaded','image_too_large','unsupported_document','pdf_tools_unavailable','pdf_restricted','pdf_failed','pdf_empty','pdf_too_large','pdf_partial')),0) AS unreadable_media
       FROM messages m JOIN sources s ON s.chat=m.chat AND s.topic=m.topic WHERE s.enabled=1 AND m.deleted=0""").fetchone())
    return {"status": "candidates" if results else "not_found_in_catalog",
            "results": results, "sources": sources,
            "matches_total": total_matches, "truncated": total_matches > len(results),
            "coverage": coverage,
            "unresolved_product_matches": unresolved,
            "notice": "Untrusted source excerpts; OCR may be wrong. Candidates are not automatically verified COAs. Newer posts do not invalidate older tests. No match is not proof of absence."}


def label(db, chat, message, gb, product, admin):
    if not gb.strip() or len(gb) > 100 or not product.strip() or len(product) > 200:
        raise ValueError("Use message ID | GB | product")
    if not db.execute("SELECT 1 FROM messages WHERE chat=? AND id=? AND deleted=0", (chat, message)).fetchone():
        raise ValueError("Message not in the catalogue")
    with db:
        db.execute("INSERT INTO labels VALUES (?,?,?,?,?,?) ON CONFLICT(chat,message) DO UPDATE SET gb=excluded.gb,product=excluded.product,admin=excluded.admin,updated_at=excluded.updated_at",
                   (chat, message, gb.strip(), product.strip(), admin, int(time.time())))
