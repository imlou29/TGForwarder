"""Private intake permissions, source binding, provenance and deduplication."""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

from tegridy_coa_store import add_import, connect, search
import tegridy_coa_intake as intake
from test_coa_sync import sync

CHAT = -1004350769620


class ImportStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = connect(Path(self.tmp.name) / "coas.sqlite3")
        self.db.execute("INSERT INTO sources(chat,topic,title) VALUES (?,7031,'COAs')", (CHAT,))
        self.db.execute("INSERT INTO messages VALUES (?,100,7031,'',100,'fp','','','protected_media_not_downloaded','[]',1,0)", (CHAT,))
        self.db.commit()
    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()
    def put(self, gb="Lobster", product="RT10", message=100, status="pdf_text"):
        return add_import(self.db, CHAT, message, gb, product, "RT10 identity purity 99.8", status, "a"*64, "report.pdf", "application/pdf", 123, 500)

    def test_linked_original_is_searchable_and_audited_without_private_ids_in_results(self):
        mid = self.put()
        result = search(self.db, "RT10 identity", "Lobster")
        self.assertEqual(result["results"][0]["supplied_copies"][0]["import_id"], mid)
        self.assertIn("/7031/100", result["results"][0]["telegram_url"])
        self.assertEqual(result["coverage"]["unreadable_media"], 0)
        self.assertNotIn("uploaded_by", str(result))
        self.assertEqual(self.db.execute("SELECT uploaded_by FROM media_imports").fetchone()[0], 123)

    def test_repeat_file_dedupes_and_sync_does_not_erase_import_text(self):
        first = self.put()
        self.assertEqual(first, self.put())
        self.assertEqual(self.db.execute("SELECT count(*) FROM media_imports").fetchone()[0], 1)
        self.db.execute("UPDATE messages SET ocr='',media_status='protected_media_not_downloaded'")
        self.db.commit()
        self.assertEqual(search(self.db, "identity", "Lobster")["status"], "candidates")

    def test_disabled_deleted_or_unknown_sources_cannot_receive_imports(self):
        with self.assertRaises(ValueError):
            self.put(message=999)
        self.db.execute("UPDATE sources SET enabled=0")
        self.db.commit()
        with self.assertRaises(ValueError):
            self.put()

    def test_conflicting_labels_and_unreadable_files_are_rejected(self):
        self.put()
        with self.assertRaises(ValueError):
            self.put(gb="YuanTai")
        with self.assertRaises(ValueError):
            self.put(status="pdf_failed")

    def test_soft_disabled_import_drops_out_of_evidence(self):
        self.put()
        self.db.execute("UPDATE media_imports SET enabled=0")
        self.db.commit()
        self.assertEqual(search(self.db, "identity", "Lobster")["results"], [])
        self.assertEqual(search(self.db, "RT10", "Lobster")["coverage"]["unreadable_media"], 1)

    def test_source_links_and_message_id_shorthand(self):
        for caption in ("Lobster | RT10 | 100", "Lobster | RT10 | https://t.me/c/4350769620/7031/100", "Lobster | RT10 | https://t.me/c/4350769620/100?thread=7031"):
            self.assertEqual(intake.validate_source(self.db, caption), ("Lobster", "RT10", CHAT, 100))
        for caption in ("Lobster | RT10 | https://evil.example/c/4350769620/100", "Lobster | RT10 | https://t.me/c/1234/100", "Lobster | RT10 | https://t.me/c/4350769620/9999/100", "RT10 | 100"):
            with self.assertRaises(ValueError):
                intake.validate_source(self.db, caption)


class IntakePermissionsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.prior = os.environ.get("COA_DB_PATH")
        os.environ["COA_DB_PATH"] = str(Path(self.tmp.name) / "coas.sqlite3")
        db = connect()
        db.execute("INSERT INTO sources(chat,topic,title) VALUES (?,7031,'COAs')", (CHAT,))
        db.execute("INSERT INTO messages VALUES (?,100,7031,'',100,'fp','','','protected_media_not_downloaded','[]',1,0)", (CHAT,))
        db.commit()
        db.close()
        self.handlers = []
        self.downloads = 0
        owner = self
        class Client:
            def on(self, event):
                def register(callback):
                    owner.handlers.append(callback)
                    return callback
                return register
            async def download_media(self, message, file):
                owner.downloads += 1
                dest = Path(file) / "original.pdf"
                dest.write_bytes(b"%PDF-offline-fixture")
                return str(dest)
        self.client = Client()
        module = ModuleType("telethon")
        module.events = SimpleNamespace(NewMessage=lambda **kwargs: None)
        with patch.dict(sys.modules, {"telethon": module}):
            intake.install(self.client, {123})

    def tearDown(self):
        if self.prior is None:
            os.environ.pop("COA_DB_PATH", None)
        else:
            os.environ["COA_DB_PATH"] = self.prior
        self.tmp.cleanup()

    def event(self, user=123, private=True, caption="Lobster | RT10 | 100", protected=False):
        responses = []
        async def respond(text, **kwargs):
            responses.append(text)
        return SimpleNamespace(sender_id=user, is_private=private, raw_text=caption, respond=respond, responses=responses,
                               message=SimpleNamespace(id=500, photo=None, document=SimpleNamespace(mime_type="application/pdf"),
                                                       file=SimpleNamespace(size=20), noforwards=protected))

    def run_event(self, event):
        async def wait_for(coroutine, timeout):
            return await coroutine
        async def to_thread(function, *args):
            return function(*args)
        with patch.dict(sys.modules, {"tegridy_coa_sync": sync}), patch.object(sync, "extract_pdf", return_value=("RT10 identity report", "pdf_text")), patch.object(intake.asyncio, "wait_for", wait_for), patch.object(intake.asyncio, "to_thread", to_thread):
            coroutine = self.handlers[0](event)
            try:
                coroutine.send(None)
            except StopIteration:
                return
            finally:
                coroutine.close()
            self.fail("Unexpected live I/O")

    def test_unauthorized_users_and_group_messages_are_ignored(self):
        for event in (self.event(user=999), self.event(private=False)):
            self.run_event(event)
            self.assertEqual(event.responses, [])
        self.assertEqual(self.downloads, 0)

    def test_batch_link_commands_require_private_admin_access(self):
        for event in (self.event(user=999, caption="/coa_links"), self.event(private=False, caption="/coa_links")):
            self.run_event(event)
            self.assertEqual(event.responses, [])
        self.assertEqual(self.downloads, 0)

    def test_batch_link_command_handles_newline_and_does_not_download_media(self):
        import json
        url = "https://chromate.org/verify?c=123_TESTONLY"
        db = connect()
        db.execute("UPDATE messages SET links=? WHERE id=100", (json.dumps([url]),))
        db.commit()
        db.close()
        event = self.event(caption=f"/coa_links\nYuanTai | RT30 | {url} | COA")
        self.run_event(event)
        self.assertEqual(self.downloads, 0)
        self.assertIn("Registered 1 report links", event.responses[0])

    def test_invalid_caption_and_protected_media_do_not_download(self):
        for event in (self.event(caption="RT10"), self.event(protected=True)):
            self.run_event(event)
            self.assertTrue(event.responses)
        self.assertEqual(self.downloads, 0)

    def test_authorized_private_original_is_imported_without_sending_to_groups(self):
        event = self.event()
        self.run_event(event)
        self.assertEqual(self.downloads, 1)
        self.assertIn("Original indexed", event.responses[0])
        db = connect()
        try:
            self.assertEqual(search(db, "RT10 identity", "Lobster")["status"], "candidates")
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main()

