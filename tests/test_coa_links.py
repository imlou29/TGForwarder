"""Exact-source link association, atomic batches and separate report types."""
import json
import tempfile
import unittest
from pathlib import Path

from tegridy_coa_links import register_batch
from tegridy_coa_store import connect, search
from tegridy_state import SRC

URL = "https://chromate.org/verify?c=123_TESTONLY"
BRIDGE = "https://verify.janoshik.com/tests/123-Example_TESTONLY"
ENDO = "https://verify.janoshik.com/tests/124-Example_TESTONLY"


class LinkTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = connect(Path(self.tmp.name) / "coas.sqlite3")
        self.db.execute("INSERT INTO sources(chat,topic,title) VALUES (?,7031,'COAs')", (SRC,))
        self.post(100, [URL])
        self.post(101, [BRIDGE, ENDO])
    def post(self, mid, urls):
        self.db.execute("INSERT INTO messages VALUES (?,?,7031,'',100,'fp','','','link_or_text',?,1,0)", (SRC, mid, json.dumps(urls)))
        self.db.commit()
    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()
    def test_registers_exact_source_link_without_claiming_report_results(self):
        result = register_batch(self.db, f"YuanTai | Retatrutide 30mg RT30 | {URL} | general COA", 123)
        self.assertIn("/7031/100", result[0]["telegram_url"])
        answer = search(self.db, "RT30", "YuanTai")
        self.assertEqual(answer["matches_total"], 1)
        self.assertEqual(answer["results"][0]["links"], [URL])
        self.assertIn("NOT been fetched or verified", answer["results"][0]["evidence"])
        self.assertEqual(search(self.db, "RT30", "Lobster")["results"], [])

    def test_report_id_selects_exact_stored_url_without_reconstructing_tokens(self):
        register_batch(self.db, "YuanTai | RT30 | chromate:123 | COA\nYuanTai | Elora | janoshik:124 | M&P Endo", 123)
        self.assertEqual(search(self.db, "RT30", "YuanTai")["results"][0]["links"], [URL])
        self.assertEqual(search(self.db, "Elora", "YuanTai")["results"][0]["links"], [ENDO])
    def test_two_tests_in_one_post_stay_separate(self):
        register_batch(self.db, f"YuanTai | Eloralintide 10mg Elora | {BRIDGE} | bridge\nYuanTai | Eloralintide 10mg Elora | {ENDO} | M&P Endo", 123)
        answer = search(self.db, "Elora", "YuanTai")
        self.assertEqual(answer["matches_total"], 2)
        self.assertEqual({r["reports"][0]["kind"] for r in answer["results"]}, {"bridge", "M&P Endo"})
        self.assertEqual(search(self.db, "Elora bridge", "YuanTai")["results"][0]["links"], [BRIDGE])
    def test_entire_batch_rolls_back_when_one_url_does_not_match(self):
        with self.assertRaisesRegex(ValueError, "Line 2"):
            register_batch(self.db, f"YuanTai | RT30 | {URL} | COA\nYuanTai | RT10 | https://chromate.org/verify?c=WRONG | COA", 123)
        self.assertEqual(self.db.execute("SELECT count(*) FROM report_links").fetchone()[0], 0)
    def test_ambiguous_source_requires_explicit_message_id(self):
        self.post(102, [URL])
        with self.assertRaisesRegex(ValueError, "multiple source"):
            register_batch(self.db, f"YuanTai | RT30 | {URL} | COA", 123)
        result = register_batch(self.db, f"YuanTai | RT30 | {URL} | COA | 102", 123)
        self.assertIn("/7031/102", result[0]["telegram_url"])
    def test_no_duplicates_and_soft_disable_restores_source_fallback(self):
        line = f"YuanTai | RT30 | {URL} | COA"
        register_batch(self.db, line, 123)
        register_batch(self.db, line, 123)
        self.assertEqual(self.db.execute("SELECT count(*) FROM report_links").fetchone()[0], 1)
        self.db.execute("UPDATE report_links SET enabled=0")
        self.db.commit()
        self.assertEqual(search(self.db, "RT30", "YuanTai")["results"], [])
    def test_disabled_sources_and_nonlab_urls_are_rejected(self):
        self.db.execute("UPDATE sources SET enabled=0")
        self.db.commit()
        with self.assertRaises(ValueError):
            register_batch(self.db, f"YuanTai | RT30 | {URL} | COA", 123)
        with self.assertRaises(ValueError):
            register_batch(self.db, "YuanTai | RT30 | https://evil.example/report | COA", 123)


if __name__ == "__main__":
    unittest.main()
