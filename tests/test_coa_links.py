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
        self.assertEqual(search(self.db, "Retatrutide", "YuanTai")["results"][0]["links"], [URL])
        self.assertEqual(search(self.db, "RT300", "YuanTai")["results"], [])
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


    def test_plain_janoshik_host_keeps_exact_stored_url(self):
        url = "https://janoshik.com/tests/246773-HGH_24iu_TESTTOKEN"
        self.post(110, [url])
        register_batch(self.db, "Lobster | HGH 24iu US | janoshik:246773 | COA | 110", 123)
        result = search(self.db, "HGH 24iu US", "Lobster")
        self.assertEqual(result["results"][0]["links"], [url])
        self.assertEqual(result["results"][0]["reports"][0]["url"], url)

    def test_vanguard_exact_url_requires_enabled_source_and_is_not_reconstructed(self):
        url = "https://verifiedbyvanguard.com/verify/30cff837-29f3-4183-92be-c06ff6a8e8f1"
        self.post(111, [url])
        register_batch(self.db, f"Lobster | HCG | {url} | COA - Vanguard | 111", 123)
        self.assertEqual(search(self.db, "HCG", "Lobster")["results"][0]["links"], [url])
        with self.assertRaisesRegex(ValueError, "not found"):
            register_batch(self.db, f"Lobster | HCG | {url[:-1]}0 | COA", 123)
        self.assertEqual(self.db.execute("SELECT count(*) FROM report_links").fetchone()[0], 1)

    def test_lookalike_domains_credentials_and_non_https_are_rejected(self):
        for url in [
            "https://janoshik.com.evil.example/tests/246773-token",
            "https://verifiedbyvanguard.com.evil.example/verify/token",
            "https://user:secret@verifiedbyvanguard.com/verify/token",
            "http://janoshik.com/tests/246773-token",
            "https://janoshik.com:8443/tests/246773-token",
            "https://verifiedbyvanguard.com/verify/token#fragment",
        ]:
            with self.subTest(url=url), self.assertRaises(ValueError):
                register_batch(self.db, f"Lobster | HCG | {url} | COA", 123)

    def test_new_hosts_do_not_allow_arbitrary_supplied_or_other_host_urls(self):
        url = "https://janoshik.com/tests/246773-HGH_24iu_TESTTOKEN"
        self.post(110, [url])
        with self.assertRaisesRegex(ValueError, "not found"):
            register_batch(self.db, "Lobster | HGH 24iu US | https://verify.janoshik.com/tests/246773-HGH_24iu_TESTTOKEN | COA | 110", 123)


    def hgh_post(self, mid, urls):
        self.post(mid, urls)
        self.db.execute("UPDATE messages SET caption=? WHERE id=?", ("Lobster HGH 11iu 24iu US EU UK " + " ".join(urls), mid))
        self.db.commit()

    def test_lobster_hgh_returns_four_confirmed_main_reports_not_custom_or_repeated_posts(self):
        reports = [
            (245776, "HGH 11iu EU UK", "11iu"),
            (245809, "HGH 24iu EU UK", "24iu"),
            (246773, "HGH 24iu US", "24iu"),
            (246794, "HGH 11iu US", "11iu"),
        ]
        for offset, (number, product, strength) in enumerate(reports):
            mid = 200 + offset
            url = f"https://verify.janoshik.com/tests/{number}-HGH_{strength}_TOKEN"
            self.hgh_post(mid, [url])
            register_batch(self.db, f"Lobster | {product} | {url} | COA | {mid}", 123)
            self.hgh_post(300 + offset, [url])
        custom = "https://janoshik.com/tests/246774-HGH_24iu_BUSINESS"
        self.hgh_post(400, [custom])
        register_batch(self.db, f"Lobster | HGH 24iu US | {custom} | Custom COA | 400", 123)
        result = search(self.db, "HGH", "Lobster")
        self.assertEqual(result["matches_total"], 4)
        self.assertEqual(len(result["results"]), 4)
        self.assertFalse(result["truncated"])
        self.assertTrue(result["curated_scope"]["registered_main_reports_only"])
        self.assertEqual({r["gb_basis"] for r in result["results"]}, {"admin_link_registration"})

    def test_hgh_region_strength_and_superh_alias_are_respected(self):
        us = "https://janoshik.com/tests/246773-HGH_24iu_TOKEN"
        eu = "https://verify.janoshik.com/tests/245809-HGH_24iu_TOKEN"
        self.hgh_post(210, [us])
        self.hgh_post(211, [eu])
        register_batch(self.db, f"Lobster | HGH 24iu US | {us} | COA | 210\nLobster | HGH 24iu EU UK | {eu} | COA | 211", 123)
        for query in ("HGH 24iu US", "SuperH 24iu US", "H.G.H. 24iu US"):
            result = search(self.db, query, "Lobster")
            self.assertEqual(result["matches_total"], 1)
            self.assertEqual(result["results"][0]["links"], [us])
        self.assertEqual(search(self.db, "HGH 24iu EU UK", "Lobster")["results"][0]["links"], [eu])

    def test_hgh_curation_does_not_hide_other_gbs_or_other_products(self):
        url = "https://janoshik.com/tests/999999-HGH_24iu_OTHERGB"
        self.hgh_post(220, [url])
        register_batch(self.db, f"YuanTai | HGH 24iu | {url} | COA | 220", 123)
        self.assertEqual(search(self.db, "HGH", "YuanTai")["results"][0]["links"], [url])
        register_batch(self.db, f"Lobster | RT30 | {URL} | COA | 100", 123)
        self.assertEqual(search(self.db, "RT30", "Lobster")["results"][0]["links"], [URL])

    def test_repeated_registrations_of_same_hgh_report_are_deduplicated(self):
        urls = ["https://verify.janoshik.com/tests/246794-HGH_11iu_TOKEN", "https://janoshik.com/tests/246794-HGH_11iu_TOKEN"]
        for mid, url in zip((230, 231), urls):
            self.hgh_post(mid, [url])
            register_batch(self.db, f"Lobster | HGH 11iu US | {url} | COA | {mid}", 123)
        result = search(self.db, "HGH", "Lobster")
        self.assertEqual(result["matches_total"], 1)



if __name__ == "__main__":
    unittest.main()
