"""Network-free tests of real catalogue sync with a fake Telegram history."""
import importlib.util
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

from tegridy_coa_store import connect
from tegridy_coa_scope import set_paused, set_mode, seed, paused, mode

# Load the indexer with only its external Telegram adapters stubbed. This lets
# the durable sync algorithm run on Windows too (production uses Linux fcntl).
adapters = {}
for name in ("dotenv", "telethon", "telethon.errors", "tegridy_topics"):
    adapters[name] = ModuleType(name)
adapters["dotenv"].load_dotenv = lambda: None
adapters["telethon.errors"].FloodWaitError = type("FloodWaitError", (Exception,), {})
adapters["tegridy_topics"].locked_user_session = lambda: None
adapters["tegridy_topics"].user_client = lambda: None
spec = importlib.util.spec_from_file_location("coa_sync_under_test", Path(__file__).resolve().parents[1] / "tegridy_coa_sync.py")
sync = importlib.util.module_from_spec(spec)
with patch.dict(sys.modules, adapters):
    spec.loader.exec_module(sync)

CHAT = -1004350769620


def message(mid, topic=85, text="Lobster RT10"):
    return SimpleNamespace(id=mid, message=text, edit_date=None, photo=None,
                           document=None, grouped_id=None, entities=[], action=None,
                           date=datetime.now(timezone.utc),
                           reply_to=SimpleNamespace(forum_topic=True, reply_to_top_id=topic))


class Client:
    def __init__(self, messages):
        self.messages = messages
    async def get_entity(self, chat):
        return SimpleNamespace(noforwards=False)
    async def get_messages(self, chat, ids=None, limit=None):
        if ids is not None:
            return next((m for m in self.messages if m.id == ids), None)
        return sorted(self.messages, key=lambda m: m.id, reverse=True)[:limit]
    async def iter_messages(self, chat, limit, reverse=False, min_id=0, max_id=10**12, reply_to=None):
        rows = [m for m in self.messages if min_id < m.id < max_id and
                (reply_to is None or m.reply_to.reply_to_top_id == reply_to)]
        for m in sorted(rows, key=lambda m: m.id, reverse=not reverse)[:limit]:
            yield m


class SyncTests(unittest.TestCase):
    def _callTestMethod(self, method):
        # All fake Telegram methods finish synchronously. Drive these coroutines
        # without an OS event-loop socket, so tests work in restricted sandboxes.
        coroutine = method()
        try:
            coroutine.send(None)
        except StopIteration:
            return
        finally:
            coroutine.close()
        self.fail("Unexpected real asynchronous I/O in an offline test")
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = connect(Path(self.tmp.name) / "coas.sqlite3")
        self.db.execute("INSERT INTO sources(chat,topic,title) VALUES (?,85,'COAs')", (CHAT,))
        self.db.commit()
    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()
    def source(self):
        return self.db.execute("SELECT * FROM sources").fetchone()

    async def test_paged_history_keeps_progress_and_never_leaks_other_topics(self):
        client = Client([message(i) for i in range(100, 107)] + [message(110, topic=99)])
        await sync.sync_source(self.db, client, self.source(), 2)
        self.assertEqual(self.source()["cursor"], 101)
        for _ in range(4):
            await sync.sync_source(self.db, client, self.source(), 2)
        self.assertGreater(self.source()["completed_at"], 0)
        self.assertEqual(self.db.execute("SELECT count(*) FROM messages").fetchone()[0], 7)
        self.assertIsNone(self.db.execute("SELECT 1 FROM messages WHERE id=110").fetchone())

    async def test_catches_more_new_posts_than_recent_window(self):
        client = Client([message(100)])
        await sync.sync_source(self.db, client, self.source(), 100)
        client.messages.extend(message(i) for i in range(101, 145))
        for _ in range(5):
            await sync.sync_source(self.db, client, self.source(), 10)
        self.assertEqual(self.db.execute("SELECT count(*) FROM messages").fetchone()[0], 45)
        self.assertEqual(self.source()["recent_cursor"], 144)

    async def test_daily_rescan_updates_edits_and_marks_deleted(self):
        client = Client([message(100), message(101)])
        await sync.sync_source(self.db, client, self.source(), 100)
        client.messages = [message(100, text="YuanTai RT10 correction")]
        self.db.execute("UPDATE sources SET completed_at=1")
        self.db.commit()
        await sync.sync_source(self.db, client, self.source(), 100)
        self.assertEqual(self.db.execute("SELECT caption FROM messages WHERE id=100").fetchone()[0], "YuanTai RT10 correction")
        self.assertEqual(self.db.execute("SELECT deleted FROM messages WHERE id=101").fetchone()[0], 1)

    async def test_protected_image_never_downloaded(self):
        m = message(100)
        m.photo = SimpleNamespace(id=123)
        text, status = await sync.extract_media(None, m, True)
        self.assertEqual(status, "protected_media_not_downloaded")
        self.assertEqual(text, "")

    async def test_protected_pdf_never_downloaded(self):
        m = message(100)
        m.document = SimpleNamespace(id=123, mime_type="application/pdf")
        text, status = await sync.extract_media(None, m, True)
        self.assertEqual((text, status), ("", "protected_media_not_downloaded"))

    async def test_existing_unsupported_pdf_is_retried(self):
        m = message(100)
        m.document = SimpleNamespace(id=123, mime_type="application/pdf")
        self.db.execute("INSERT INTO messages VALUES (?,100,85,'',100,?,'','','unsupported_document','[]',1,0)", (CHAT, sync.fingerprint(m)))
        self.db.commit()
        async def extract(*args):
            return "PDF report RT10", "pdf_text"
        with patch.object(sync, "extract_media", extract):
            await sync.save_message(self.db, None, self.source(), m, 2, False)
        self.assertEqual(self.db.execute("SELECT media_status,ocr FROM messages").fetchone()[0], "pdf_text")

    async def test_opening_read_window_retries_historical_files(self):
        client = Client([message(100), message(101)])
        self.db.execute("INSERT INTO state VALUES (?, '1')", (f"protection:{CHAT}:85",))
        self.db.execute("UPDATE sources SET completed_at=?,recent_cursor=101", (int(datetime.now(timezone.utc).timestamp()),))
        self.db.commit()
        await sync.sync_source(self.db, client, self.source(), 100)
        self.assertEqual(self.db.execute("SELECT count(*) FROM messages").fetchone()[0], 2)
        self.assertEqual(self.db.execute("SELECT value FROM state WHERE key=?", (f"protection:{CHAT}:85",)).fetchone()[0], "0")

    async def test_root_post_is_included(self):
        client = Client([message(85)])
        await sync.sync_source(self.db, client, self.source(), 100)
        self.assertIsNotNone(self.db.execute("SELECT 1 FROM messages WHERE id=85").fetchone())

    async def test_pause_prevents_any_telegram_access_and_keeps_records(self):
        self.db.execute("INSERT INTO messages VALUES (?,100,85,'',100,'fp','keep','','link_or_text','[]',1,0)", (CHAT,))
        self.db.commit()
        set_paused(self.db, CHAT, 85, True)
        with self.assertRaises(sync.SourcePaused):
            await sync.sync_source(self.db, None, self.source(), 100)
        self.assertEqual(self.db.execute("SELECT caption FROM messages").fetchone()[0], "keep")

    async def test_pause_takes_effect_between_messages(self):
        owner = self
        class PauseClient(Client):
            async def iter_messages(self, *args, **kwargs):
                yield message(100, text="first")
                set_paused(owner.db, CHAT, 85, True)
                yield message(101, text="second")
        with self.assertRaises(sync.SourcePaused):
            await sync.sync_source(self.db, PauseClient([]), self.source(), 100)
        self.assertEqual(self.db.execute("SELECT count(*) FROM messages").fetchone()[0], 1)

    async def test_recent_links_reads_only_latest_200_and_never_downloads_media(self):
        set_mode(self.db, CHAT, 85, "recent_links")
        messages = [message(i, text="ordinary update") for i in range(100, 401)]
        messages[0].message = "https://chromate.org/verify?c=OLD"
        messages[-1].message = "YuanTai report https://chromate.org/verify?c=NEW"
        messages[-1].photo = SimpleNamespace(id=99)
        with patch.object(sync, "extract_media", side_effect=AssertionError("Must not download media")):
            await sync.sync_source(self.db, Client(messages), self.source(), 100)
        self.assertEqual([r[0] for r in self.db.execute("SELECT id FROM messages")], [400])
        self.assertEqual(self.source()["cutoff"], 0)
        self.assertGreater(self.source()["completed_at"], 0)

    async def test_deployment_defaults_do_not_override_later_admin_choices(self):
        seed(self.db, CHAT, "85", "85")
        self.assertTrue(paused(self.db, CHAT, 85))
        self.assertEqual(mode(self.db, CHAT, 85), "recent_links")
        set_paused(self.db, CHAT, 85, False)
        set_mode(self.db, CHAT, 85, "full")
        seed(self.db, CHAT, "85", "85")
        self.assertFalse(paused(self.db, CHAT, 85))
        self.assertEqual(mode(self.db, CHAT, 85), "full")


if __name__ == "__main__":
    unittest.main()
