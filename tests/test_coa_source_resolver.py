"""One-post intake resolution preserves source scope and never reads remote media."""
import tempfile
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch, Mock
from datetime import datetime, timezone
from tegridy_coa_store import connect, source_post
from tegridy_coa_scope import set_paused
from tegridy_coa_source_resolver import ensure_source, store_fetched_post
from test_coa_sync import sync, message
from tegridy_state import SRC

class ResolverTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.db=connect(Path(self.tmp.name)/"coas.sqlite")
        self.db.execute("INSERT INTO sources(chat,topic,title) VALUES (?,7031,'COAs')",(SRC,))
        self.db.commit()
    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()
    def save(self, mid=103029, topic=7031):
        with patch.dict("sys.modules",{"tegridy_coa_sync":sync}):
            store_fetched_post(self.db,SRC,topic,mid,message(mid,topic=topic,text="Actual publisher caption"),123)
    async def test_fetches_one_missing_post_without_history_or_media_download(self):
        owner=self
        class FakeClient:
            requested=[]
            closed=False
            async def connect(self): pass
            async def is_user_authorized(self): return True
            async def get_me(self): return SimpleNamespace(bot=False,id=9076133249)
            async def get_messages(self,chat,ids):
                self.requested.append((chat,ids))
                m=message(ids,topic=7031,text="Actual publisher caption")
                m.document=SimpleNamespace(id=42,mime_type="application/pdf")
                m.noforwards=True
                return m
            async def disconnect(self): self.closed=True
            def download_media(self,*args,**kwargs): raise AssertionError("Do not download source media")
            def iter_messages(self,*args,**kwargs): raise AssertionError("Do not scan history")
        client=FakeClient(); lock=Mock()
        adapter=ModuleType("tegridy_topics")
        adapter.locked_user_session=lambda:lock
        adapter.user_client=lambda:client
        with patch.dict("sys.modules",{"tegridy_topics":adapter,"tegridy_coa_sync":sync}), patch("tegridy_state.connect",return_value=Mock()), patch("tegridy_state.settings",return_value="0"), patch.dict("os.environ",{"EXPECTED_USER_ID":"9076133249"}):
            await ensure_source(self.db,SRC,7031,103029,123)
        row=source_post(self.db,SRC,103029)
        self.assertEqual(row["caption"],"Actual publisher caption")
        self.assertEqual(row["media_status"],"link_metadata_only")
        self.assertEqual(row["ocr"],"")
        self.assertEqual(client.requested,[(SRC,103029)])
        self.assertTrue(client.closed)
        lock.close.assert_called_once()
        self.assertIsNotNone(self.db.execute("SELECT value FROM state WHERE key=?",(f"coa:on_demand:{SRC}:103029",)).fetchone())
    async def test_existing_post_needs_no_connection_and_preserves_ocr(self):
        self.save()
        self.db.execute("UPDATE messages SET ocr='existing report',media_status='ocr_text'")
        self.db.commit()
        await ensure_source(self.db,SRC,7031,103029,123)
        self.assertEqual(source_post(self.db,SRC,103029)["ocr"],"existing report")
    async def test_disabled_or_unknown_topic_rejected_before_network(self):
        for topic in (210,48997):
            with self.assertRaises(ValueError):
                await ensure_source(self.db,SRC,topic,103029,123)
        self.db.execute("UPDATE sources SET enabled=0")
        self.db.commit()
        with self.assertRaises(ValueError):
            await ensure_source(self.db,SRC,7031,103029,123)
        self.assertEqual(self.db.execute("SELECT count(*) FROM messages").fetchone()[0],0)
    async def test_wrong_topic_missing_post_and_wrong_id_write_nothing(self):
        with patch.dict("sys.modules",{"tegridy_coa_sync":sync}):
            for m in (None,message(103029,topic=210),message(103030,topic=7031)):
                with self.subTest(message=m), self.assertRaises(ValueError):
                    store_fetched_post(self.db,SRC,7031,103029,m,123)
        self.assertEqual(self.db.execute("SELECT count(*) FROM messages").fetchone()[0],0)
    async def test_explicit_import_from_paused_enabled_source_does_not_resume_sync(self):
        set_paused(self.db,SRC,7031,True)
        self.save()
        self.assertIsNotNone(source_post(self.db,SRC,103029))
        self.assertEqual(self.db.execute("SELECT value FROM state WHERE key=?",(f"coa:paused:{SRC}:7031",)).fetchone()[0],"1")
        self.assertEqual(self.db.execute("SELECT cursor FROM sources").fetchone()[0],0)
    async def test_existing_link_topic_mismatch_is_not_silently_accepted(self):
        self.save()
        with self.assertRaises(ValueError):
            await ensure_source(self.db,SRC,210,103029,123)
