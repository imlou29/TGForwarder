"""SQLite configuration/state for Tegridy, isolated from OpenClaw."""
import json
import os
import sqlite3
import time
from pathlib import Path

DB_PATH = os.environ.get("BACKUP_DB_PATH", "/data/tgforwarder/forwarded.sqlite3")
FILTERS = ("text", "photos", "photo_caption", "all")
SRC = -1004350769620
DST = -1003900751944


def connect():
    path = Path(os.environ.get("BACKUP_DB_PATH", DB_PATH))
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(path), timeout=15)
    db.execute("PRAGMA busy_timeout=15000")
    db.execute("""CREATE TABLE IF NOT EXISTS forwarded (
        source_chat INTEGER NOT NULL, source_msg INTEGER NOT NULL,
        target_chat INTEGER NOT NULL, target_topic INTEGER NOT NULL, target_msg INTEGER,
        PRIMARY KEY(source_chat,source_msg,target_chat,target_topic))""")
    db.execute("""CREATE TABLE IF NOT EXISTS topic_routes (
        source_chat INTEGER NOT NULL, source_topic INTEGER NOT NULL,
        target_chat INTEGER NOT NULL, target_topic INTEGER NOT NULL,
        source_name TEXT NOT NULL DEFAULT '', target_name TEXT NOT NULL DEFAULT '',
        content_filter TEXT NOT NULL DEFAULT 'photo_caption',
        exclude_fun INTEGER NOT NULL DEFAULT 1, enabled INTEGER NOT NULL DEFAULT 1,
        PRIMARY KEY(source_chat,source_topic),
        UNIQUE(target_chat,target_topic))""")
    db.execute("""CREATE TABLE IF NOT EXISTS topic_inventory (
        chat INTEGER NOT NULL, topic INTEGER NOT NULL, title TEXT NOT NULL,
        is_general INTEGER NOT NULL DEFAULT 0, scanned_at INTEGER NOT NULL,
        PRIMARY KEY(chat,topic))""")
    db.execute("""CREATE TABLE IF NOT EXISTS topic_selections (
        admin_id INTEGER NOT NULL, source_chat INTEGER NOT NULL,
        source_topic INTEGER NOT NULL, PRIMARY KEY(admin_id,source_chat,source_topic))""")
    db.execute("""CREATE TABLE IF NOT EXISTS backup_settings (
        key TEXT PRIMARY KEY, value TEXT NOT NULL)""")
    db.execute("""CREATE TABLE IF NOT EXISTS topic_rescan (
        source_chat INTEGER NOT NULL, source_topic INTEGER NOT NULL,
        PRIMARY KEY (source_chat,source_topic))""")
    db.execute("""CREATE TABLE IF NOT EXISTS backup_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT, admin_id INTEGER,
        source_chat INTEGER, source_topic INTEGER, target_chat INTEGER,
        target_topic INTEGER, phase TEXT NOT NULL, started_at INTEGER NOT NULL,
        finished_at INTEGER, status TEXT NOT NULL, copied INTEGER NOT NULL DEFAULT 0,
        error TEXT NOT NULL DEFAULT '')""")
    db.execute("""CREATE TABLE IF NOT EXISTS copy_events (
        source_chat INTEGER NOT NULL, source_msg INTEGER NOT NULL,
        target_chat INTEGER NOT NULL, target_topic INTEGER NOT NULL,
        sent_at REAL NOT NULL,
        PRIMARY KEY(source_chat,source_msg,target_chat,target_topic))""")
    db.execute("""CREATE TABLE IF NOT EXISTS batch_state (
        source_chat INTEGER NOT NULL, source_topic INTEGER NOT NULL,
        target_chat INTEGER NOT NULL, target_topic INTEGER NOT NULL,
        initial_cutoff INTEGER NOT NULL, initial_complete INTEGER NOT NULL DEFAULT 0,
        last_complete INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY(source_chat,source_topic,target_chat,target_topic))""")
    # Existing TOPIC_ROUTES may contain Touchdowns and must be preserved.
    for entry in json.loads(os.environ.get("TOPIC_ROUTES", "[]")):
        sc, st, dc, dt = (int(entry[k]) for k in ("source_chat", "source_topic", "target_chat", "target_topic"))
        name = "Touchdowns" if (sc, st) == (SRC, 501) else ""
        db.execute("""INSERT OR IGNORE INTO topic_routes
            (source_chat,source_topic,target_chat,target_topic,source_name,target_name,content_filter)
            VALUES (?,?,?,?,?,?,?)""", (sc, st, dc, dt, name, name, "photo_caption"))
    db.execute("INSERT OR IGNORE INTO backup_settings VALUES ('auto_forwarding','off')")
    db.commit()
    return db


def settings(db, key, default=""):
    row = db.execute("SELECT value FROM backup_settings WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def set_setting(db, key, value):
    db.execute("INSERT INTO backup_settings(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
    db.commit()


def mappings(db, enabled_only=True):
    sql = """SELECT source_chat,source_topic,target_chat,target_topic,source_name,
        content_filter,exclude_fun FROM topic_routes"""
    if enabled_only:
        sql += " WHERE enabled=1"
    return [dict(zip(("source_chat","source_topic","target_chat","target_topic","name","filter","exclude_fun"), row))
            for row in db.execute(sql + " ORDER BY source_chat,source_topic")]


def selected(db, admin_id):
    rows = db.execute("SELECT source_chat,source_topic FROM topic_selections WHERE admin_id=?", (admin_id,)).fetchall()
    if rows:
        return set((int(a), int(b)) for a,b in rows if int(b)>=0)
    # Default Touchdowns only. Empty selection after explicit clearing uses sentinel.
    sentinel = db.execute("SELECT 1 FROM topic_selections WHERE admin_id=? AND source_topic=-1", (admin_id,)).fetchone()
    return set() if sentinel else {(SRC,501)}


def set_selected(db, admin_id, selected_routes):
    db.execute("DELETE FROM topic_selections WHERE admin_id=?", (admin_id,))
    if not selected_routes:
        db.execute("INSERT INTO topic_selections VALUES (?,?,?)", (admin_id,SRC,-1))
    else:
        db.executemany("INSERT INTO topic_selections VALUES (?,?,?)", [(admin_id,*pair) for pair in selected_routes])
    db.commit()


def filter_message(m, mode="photo_caption", exclude_fun=True):
    if getattr(m, "action", None):
        return False
    if exclude_fun and (getattr(m, "sticker", None) or getattr(m, "gif", None)):
        return False
    has_text = bool((getattr(m, "message", None) or "").strip())
    photo = bool(getattr(m, "photo", None))
    if mode == "text":
        return bool(has_text and not getattr(m, "media", None))
    if mode == "photos":
        return photo
    if mode == "photo_caption":
        return photo and has_text
    if mode == "all":
        return bool(has_text or photo or getattr(m, "video", None) or getattr(m, "document", None))
    return False


def add_copy(db, source_chat, source_msg, target_chat, target_topic, target_msg):
    with db:
        result=db.execute("INSERT OR IGNORE INTO forwarded VALUES (?,?,?,?,?)",
                          (source_chat, source_msg, target_chat, target_topic, target_msg))
        if result.rowcount:
            db.execute("INSERT OR IGNORE INTO copy_events VALUES (?,?,?,?,?)",
                       (source_chat, source_msg, target_chat, target_topic, time.time()))


def count_copies(db, route):
    row=db.execute("""SELECT count(*) FROM forwarded WHERE source_chat=? AND target_chat=?
                      AND target_topic=?""", (route["source_chat"],route["target_chat"],route["target_topic"])).fetchone()
    return row[0]


def route_filter(db, source_chat, source_topic):
    row=db.execute("SELECT content_filter,exclude_fun FROM topic_routes WHERE source_chat=? AND source_topic=? AND enabled=1",
                   (source_chat,source_topic)).fetchone()
    return (row[0],bool(row[1])) if row else None


def register_run(db, admin_id, route, phase):
    with db:
        cursor=db.execute("""INSERT INTO backup_runs
          (admin_id,source_chat,source_topic,target_chat,target_topic,phase,started_at,status)
          VALUES (?,?,?,?,?,?,?,'running')""",
          (admin_id,route["source_chat"],route["source_topic"],route["target_chat"],route["target_topic"],phase,int(time.time())))
    return cursor.lastrowid


def finish_run(db, run_id, status, error=""):
    with db:
        db.execute("UPDATE backup_runs SET finished_at=?,status=?,error=? WHERE id=?",
                   (int(time.time()),status,str(error)[:300],run_id))


def set_run_copied(db, run_id, count):
    with db:
        db.execute("UPDATE backup_runs SET copied=? WHERE id=?", (count,run_id))


def recent_events(db, route, since, cap=20):
    return [x[0] for x in db.execute("""SELECT sent_at FROM copy_events
        WHERE source_chat=? AND target_chat=? AND target_topic=? AND sent_at>=?
        ORDER BY sent_at DESC LIMIT ?""",
        (route["source_chat"],route["target_chat"],route["target_topic"],since,cap)).fetchall()]


def history(db, limit=8):
    return db.execute("""SELECT id,source_topic,phase,started_at,status,copied,error
       FROM backup_runs ORDER BY id DESC LIMIT ?""", (limit,)).fetchall()
