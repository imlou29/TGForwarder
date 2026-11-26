"""Exact Uzorak URLs work in source and admin-supplied registration."""
import json
import tempfile
import unittest
from pathlib import Path
from tegridy_coa_store import connect, search
from tegridy_coa_links import register_batch, lab_url
from tegridy_coa_supplied import register_supplied
from tegridy_coa_scope import has_lab_link
from tegridy_state import SRC

URL = "https://uzorak.com/verify/B90161_retatrutide_3x10mg_lobster"

class UzorakTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = connect(Path(self.tmp.name)/"coas.sqlite")
    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()
    def test_admin_supplied_third_opinion_preserves_exact_url_and_gb(self):
        register_supplied(self.db, f"Lobster | Retatrutide 10mg RT10 Reta | {URL} | M&P - Uzorak - third opinion", 123)
        item = search(self.db, "RT10", "Lobster")["results"][0]
        self.assertEqual(item["links"], [URL])
        self.assertIn("third opinion", item["reports"][0]["kind"])
        self.assertIsNone(item["telegram_url"])
        self.assertEqual(search(self.db,"RT10","YuanTai")["results"], [])
    def test_source_registration_still_requires_exact_indexed_post(self):
        with self.assertRaisesRegex(ValueError,"not found"):
            register_batch(self.db, f"Lobster | RT10 | {URL} | M&P", 123)
        self.db.execute("INSERT INTO sources(chat,topic,title) VALUES (?,7031,'COAs')", (SRC,))
        self.db.execute("INSERT INTO messages VALUES (?,100,7031,'',100,'fp','','','link_or_text',?,1,0)",(SRC,json.dumps([URL])))
        self.db.commit()
        register_batch(self.db, f"Lobster | RT10 | {URL} | M&P | 100", 123)
        self.assertEqual(search(self.db,"RT10","Lobster")["results"][0]["links"],[URL])
        self.assertTrue(has_lab_link([URL]))
    def test_similar_hosts_credentials_ports_and_http_stay_rejected(self):
        for bad in (URL.replace("uzorak.com","uzorak.com.evil.example"),
                    URL.replace("uzorak.com","user:password@uzorak.com"),
                    URL.replace("uzorak.com","uzorak.com:8443"),
                    URL.replace("https:","http:"), URL+"#fragment"):
            with self.subTest(url=bad), self.assertRaises(ValueError):
                lab_url(bad)
