"""Per-source catalogue controls; independent from backup settings and receipts."""
import json
from urllib.parse import urlsplit

LAB_HOSTS = {"chromate.org", "www.chromate.org", "verify.janoshik.com", "janoshik.com", "www.janoshik.com", "verifiedbyvanguard.com", "uzorak.com"}


def key(kind, chat, topic):
    return f"coa:{kind}:{chat}:{topic}"


def value(db, name, default):
    row = db.execute("SELECT value FROM state WHERE key=?", (name,)).fetchone()
    return row[0] if row else default


def paused(db, chat, topic):
    return value(db, key("paused", chat, topic), "0") == "1"


def mode(db, chat, topic):
    return value(db, key("mode", chat, topic), "full")


def set_paused(db, chat, topic, enabled):
    with db:
        db.execute("INSERT OR REPLACE INTO state VALUES (?,?)", (key("paused", chat, topic), "1" if enabled else "0"))


def set_mode(db, chat, topic, setting):
    if setting not in ("full", "recent_links"):
        raise ValueError("Unsupported catalogue mode")
    with db:
        db.execute("INSERT OR REPLACE INTO state VALUES (?,?)", (key("mode", chat, topic), setting))


def seed(db, chat, paused_topics="", recent_topics=""):
    # Initial defaults never override later explicit changes in the admin panel.
    with db:
        for kind, raw, setting in (("paused", paused_topics, "1"), ("mode", recent_topics, "recent_links")):
            for part in raw.split(","):
                if part.strip().isascii() and part.strip().isdigit():
                    db.execute("INSERT OR IGNORE INTO state VALUES (?,?)", (key(kind, chat, int(part)), setting))


def has_lab_link(links):
    if isinstance(links, str):
        links = json.loads(links)
    for link in links:
        try:
            if urlsplit(link).hostname in LAB_HOSTS:
                return True
        except (ValueError, TypeError):
            continue
    return False
