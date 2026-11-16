"""Admin supplied links must never fabricate Telegram provenance."""
import tempfile
import unittest
from pathlib import Path
from tegridy_coa_store import connect, search
from tegridy_coa_supplied import register_supplied
from tegridy_coa_links import register_batch

URL = "https://verifiedbyvanguard.com/verify/bfe42767-52c0-41a4-b00b-cdecc8404e12"

class SuppliedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = connect(Path(self.tmp.name) / "catalog.sqlite")
    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()
    def test_supplied_link_search_has_no_invented_telegram_post(self):
        register_supplied(self.db, f"Lobster | Cartalax | {URL} | COA - Vanguard", 123)
        result = search(self.db, "Cartalax", "Lobster")
        self.assertEqual(result["matches_total"], 1)
        item = result["results"][0]
        self.assertEqual(item["links"], [URL])
        self.assertIsNone(item["telegram_url"])
        self.assertIsNone(item["message_id"])
        self.assertIsNone(item["posted_at"])
        self.assertFalse(item["provenance"]["has_telegram_source"])
        self.assertIn("NOT been fetched or verified", item["evidence"])
        self.assertEqual(self.db.execute("SELECT count(*) FROM messages").fetchone()[0], 0)
        self.assertEqual(search(self.db, "Cartalax", "YuanTai")["results"], [])
        self.assertEqual(search(self.db, "Cartalax", "")["status"], "needs_gb")
    def test_bad_batch_writes_nothing(self):
        with self.assertRaises(ValueError):
            register_supplied(self.db, f"Lobster | Cartalax | {URL} | COA\nLobster | RT10 | https://evil.example/report | COA", 123)
        self.assertEqual(self.db.execute("SELECT count(*) FROM admin_report_links").fetchone()[0], 0)
    def test_reregistration_updates_and_removal_hides_it(self):
        line = f"Lobster | Cartalax | {URL} | COA"
        register_supplied(self.db, line, 123)
        register_supplied(self.db, line, 456)
        self.assertEqual(self.db.execute("SELECT count(*) FROM admin_report_links").fetchone()[0], 1)
        self.assertEqual(self.db.execute("SELECT admin FROM admin_report_links").fetchone()[0], 456)
        self.db.execute("UPDATE admin_report_links SET enabled=0")
        self.db.commit()
        self.assertEqual(search(self.db, "Cartalax", "Lobster")["results"], [])
    def test_normal_registration_still_requires_original_source(self):
        with self.assertRaisesRegex(ValueError, "not found"):
            register_batch(self.db, f"Lobster | Cartalax | {URL} | COA", 123)
    def test_limits_duplicate_and_url_validation(self):
        line = f"Lobster | Cartalax | {URL} | COA"
        for text in ("", "\n".join([line] * 26), line + "\n" + line,
                     line.replace(URL, "https://verifiedbyvanguard.com.evil.example/verify/token"),
                     line + " | 999"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                register_supplied(self.db, text, 123)
    def test_counts_and_truncation_include_supplied_links(self):
        for n in range(3):
            register_supplied(self.db, f"Lobster | Cartalax | {URL}{n} | COA", 123)
        result = search(self.db, "Cartalax", "Lobster", limit=1)
        self.assertEqual(result["matches_total"], 3)
        self.assertTrue(result["truncated"])
        self.assertEqual(len(result["results"]), 1)
