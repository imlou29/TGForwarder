"""COA evidence boundaries, album association and authenticated API checks."""
import json
import os
import tempfile
import io
import unittest
from types import SimpleNamespace
from pathlib import Path

from tegridy_coa_store import connect, label, normalized, search
from tegridy_coa_service import handler

CHAT = -1004350769620


class CatalogueTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "coas.sqlite3")
        self.db = connect(self.path)
        self.db.execute("INSERT INTO sources(chat,topic,title) VALUES (?,85,'COAs')", (CHAT,))
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def post(self, mid, caption="", ocr="", album="", deleted=0):
        self.db.execute("INSERT INTO messages VALUES (?,?,85,?,?,'fp',?,?,'ocr_text','[]',1,?)",
                        (CHAT, mid, album, mid, caption, ocr, deleted))
        self.db.commit()

    def test_missing_gb_stops_before_candidates(self):
        self.post(100, "Lobster RT10")
        self.assertEqual(search(self.db, "RT10", "")["status"], "needs_gb")

    def test_no_product_name_or_unique_result_implies_gb(self):
        self.post(100, "RT10 purity report")
        self.assertEqual(search(self.db, "RT10", "Lobster")["results"], [])

    def test_album_caption_applies_only_to_same_album(self):
        self.post(100, "Lobster", album="77")
        self.post(101, ocr="RT 10 report 99.9%", album="77")
        self.post(102, ocr="RT30 report")
        result = search(self.db, "rt10", "Lobster")
        self.assertEqual(len(result["results"]), 1)
        self.assertIn("/85/100", result["results"][0]["telegram_url"])
        self.assertEqual(search(self.db, "RT30", "Lobster")["results"], [])

    def test_admin_label_is_audited_and_overrides_mentions(self):
        self.post(100, "Old Lobster reference RT10")
        label(self.db, CHAT, 100, "YuanTai", "RT10", 1234)
        self.assertEqual(search(self.db, "RT10", "Lobster")["results"], [])
        self.assertEqual(search(self.db, "RT10", "YuanTai")["results"][0]["gb_basis"], "admin_label")
        self.assertEqual(self.db.execute("SELECT admin FROM labels").fetchone()[0], 1234)

    def test_conflicting_album_labels_stay_unresolved(self):
        self.post(100, "RT10", album="77")
        self.post(101, "RT10", album="77")
        label(self.db, CHAT, 100, "Lobster", "RT10", 1234)
        label(self.db, CHAT, 101, "YuanTai", "RT10", 1234)
        self.assertEqual(search(self.db, "RT10", "Lobster")["results"], [])

    def test_disabled_sources_deleted_posts_and_strength_boundaries(self):
        self.post(100, "Lobster RT100")
        self.post(101, "Lobster RT10", deleted=1)
        self.assertEqual(search(self.db, "RT10", "Lobster")["results"], [])
        self.post(102, "Lobster RT10")
        self.db.execute("UPDATE sources SET enabled=0")
        self.db.commit()
        self.assertEqual(search(self.db, "RT10", "Lobster")["results"], [])

    def test_keeps_multiple_tests_instead_of_replacing_with_latest(self):
        self.post(100, "Lobster RT10 identity")
        self.post(101, "Lobster RT10 endotoxin")
        self.assertEqual(len(search(self.db, "RT10", "Lobster")["results"]), 2)

    def test_search_is_parameterized(self):
        search(self.db, "RT10' OR 1=1 --", "Lobster")
        self.assertEqual(self.db.execute("SELECT count(*) FROM sources").fetchone()[0], 1)

    def test_http_requires_token_and_gb(self):
        old = os.environ.get("COA_DB_PATH")
        os.environ["COA_DB_PATH"] = self.path
        class Socket:
            def __init__(self, headers):
                self.request = io.BytesIO(("GET /v1/coas/search?q=RT10 HTTP/1.0\r\n" + headers + "\r\n").encode())
                self.response = bytearray()
            def makefile(self, *args):
                return self.request
            def sendall(self, data):
                self.response.extend(data)
        try:
            sock = Socket("")
            handler("x" * 48)(sock, ("127.0.0.1", 1234), SimpleNamespace())
            self.assertIn(b"401 Unauthorized", sock.response)
            sock = Socket("Authorization: Bearer " + "x" * 48 + "\r\n")
            handler("x" * 48)(sock, ("127.0.0.1", 1234), SimpleNamespace())
            self.assertIn(b"200 OK", sock.response)
            self.assertEqual(json.loads(bytes(sock.response).split(b"\r\n\r\n")[1])["status"], "needs_gb")
        finally:
            if old is None:
                os.environ.pop("COA_DB_PATH", None)
            else:
                os.environ["COA_DB_PATH"] = old


if __name__ == "__main__":
    unittest.main()
