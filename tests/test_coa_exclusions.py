"""Excluded custom certificates must not leak through other result fields."""
import json
import tempfile
import unittest
from pathlib import Path
from tegridy_coa_store import connect, search
from tegridy_coa_exclusions import EXCLUDED_JANOSHIK_IDS, excluded_url
from tegridy_coa_links import register_batch
from tegridy_coa_supplied import register_supplied
from tegridy_state import SRC

def url(number):
    return f"https://verify.janoshik.com/tests/{number}-Eloralintide_10mg_TOKEN"

class ExclusionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = connect(Path(self.tmp.name) / "catalog.sqlite")
        self.db.execute("INSERT INTO sources(chat,topic,title) VALUES (?,7031,'COAs')", (SRC,))
        self.urls = [url(250971)] + [url(n) for n in range(250973,251009)]
        caption = "YuanTai Eloralintide 10mg Endo\n" + "\n".join(self.urls)
        self.db.execute("INSERT INTO messages VALUES (?,98675,7031,'',100,'fp',?,'','link_or_text',?,1,0)", (SRC, caption, json.dumps(self.urls)))
        self.db.commit()
    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()
    def assert_clean(self, result):
        serialized = json.dumps(result)
        for n in EXCLUDED_JANOSHIK_IDS:
            self.assertNotIn("/tests/" + n + "-", serialized)
        self.assertIn(url(250971), serialized)
    def test_unregistered_post_exposes_only_main_url_and_clean_caption(self):
        result = search(self.db, "Eloralintide", "YuanTai")
        self.assert_clean(result)
        self.assertEqual(result["results"][0]["links"], [url(250971)])
        self.assertEqual(len(json.loads(self.db.execute("SELECT links FROM messages").fetchone()[0])), 37)
    def test_registered_main_does_not_leak_excluded_urls_in_original_caption(self):
        register_batch(self.db, f"YuanTai | Eloralintide 10mg Elora | {url(250971)} | Endo | 98675", 123)
        result = search(self.db, "Elora", "YuanTai")
        self.assert_clean(result)
        self.assertEqual(result["matches_total"], 1)
    def test_excluded_registered_and_supplied_urls_are_never_returned(self):
        register_batch(self.db, f"YuanTai | Eloralintide 10mg | {url(250973)} | Endo | 98675", 123)
        register_supplied(self.db, f"YuanTai | Eloralintide 10mg | {url(251008)} | Endo", 123)
        self.assert_clean(search(self.db, "Eloralintide", "YuanTai"))
    def test_sync_refresh_cannot_restore_excluded_urls(self):
        for _ in range(2):
            self.db.execute("UPDATE messages SET links=?", (json.dumps(self.urls),))
            self.db.commit()
            self.assert_clean(search(self.db, "Eloralintide", "YuanTai"))
    def test_ocr_and_import_text_are_also_filtered(self):
        self.db.execute("UPDATE messages SET ocr=?", (url(250974),))
        self.db.execute("""INSERT INTO media_imports(chat,message,sha256,filename,mime,gb,product,text,status,uploaded_by,upload_message,received_at)
            VALUES (?,98675,?,'image.png','image/png','YuanTai','Eloralintide',?,'ocr_text',123,1,100)""",
            (SRC, "a" * 64, url(250975)))
        self.db.commit()
        self.assert_clean(search(self.db, "Eloralintide", "YuanTai"))
    def test_exact_requested_range_only(self):
        self.assertEqual(len(EXCLUDED_JANOSHIK_IDS), 36)
        for n in (250969,250970,250971,250972,251009):
            self.assertFalse(excluded_url(url(n)))
        for n in range(250973,251009):
            self.assertTrue(excluded_url(url(n)))
        self.assertFalse(excluded_url("https://other.example/tests/250973-token"))
        self.assertFalse(excluded_url("https://chromate.org/verify?c=250973_TOKEN"))
