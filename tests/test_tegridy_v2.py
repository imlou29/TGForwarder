"""Offline smoke tests: never contact Telegram or send a backup."""
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from telethon import functions
import tegridy_batch
import tegridy_forwarder
import tegridy_topics
from tegridy_state import (SRC,DST,connect,settings,set_setting,selected,set_selected,
                          mappings,filter_message,add_copy,count_copies)
from tegridy_controller import BackupJobs


class BotSmokeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.prior={k:os.environ.get(k) for k in ("BACKUP_DB_PATH","TOPIC_ROUTES")}
        os.environ["BACKUP_DB_PATH"]=str(Path(self.tmp.name)/"forwarded.sqlite3")
        os.environ["TOPIC_ROUTES"]='[{"source_chat":-1004350769620,"source_topic":501,"target_chat":-1003900751944,"target_topic":3962}]'
        self.db=connect()

    def tearDown(self):
        self.db.close()
        for k,v in self.prior.items():
            if v is None:os.environ.pop(k,None)
            else:os.environ[k]=v
        self.tmp.cleanup()

    def fake(self,text="",photo=False,media=False,video=False,document=False,sticker=False,gif=False):
        return SimpleNamespace(action=None,message=text,photo=photo,media=media,video=video,
                               document=document,sticker=sticker,gif=gif)

    def test_telegram_forum_api_availability(self):
        self.assertTrue(hasattr(functions.channels,"GetForumTopicsRequest"))
        self.assertTrue(hasattr(functions.channels,"CreateForumTopicRequest"))

    def test_default_off_and_touchdowns_route(self):
        self.assertEqual(settings(self.db,"auto_forwarding"),"off")
        routes=mappings(self.db)
        self.assertEqual(len(routes),1)
        self.assertEqual(routes[0]["filter"],"photo_caption")
        set_setting(self.db,"auto_forwarding","on")
        self.assertEqual(settings(self.db,"auto_forwarding"),"on")

    def test_selection_clear_and_all(self):
        self.assertEqual(selected(self.db,6570407118),{(SRC,501)})
        set_selected(self.db,6570407118,set())
        self.assertEqual(selected(self.db,6570407118),set())
        set_selected(self.db,6570407118,{(SRC,501)})
        self.assertEqual(selected(self.db,6570407118),{(SRC,501)})

    def test_filter_types_and_default_sticker_exclusion(self):
        photo=self.fake("title",photo=True,media=True)
        photo_empty=self.fake("",photo=True,media=True)
        txt=self.fake("hi")
        link_preview=self.fake("website",media=True)
        file_msg=self.fake("doc",document=True,media=True)
        sticker=self.fake("",sticker=True,document=True,media=True)
        self.assertTrue(filter_message(photo,"photo_caption",True))
        self.assertFalse(filter_message(photo_empty,"photo_caption",True))
        self.assertTrue(filter_message(photo_empty,"photos",True))
        self.assertTrue(filter_message(txt,"text",True))
        self.assertTrue(filter_message(link_preview,"text",True))
        self.assertFalse(filter_message(file_msg,"text",True))
        self.assertTrue(filter_message(file_msg,"all",True))
        self.assertFalse(filter_message(sticker,"all",True))
        self.assertTrue(filter_message(sticker,"all",False))

    def test_copy_receipts_count_and_dedupe_events(self):
        route=mappings(self.db)[0]
        add_copy(self.db,SRC,88100,DST,3962,99100)
        add_copy(self.db,SRC,88100,DST,3962,99100)
        self.assertEqual(count_copies(self.db,route),1)
        self.assertEqual(self.db.execute("SELECT count(*) FROM copy_events").fetchone()[0],1)

    def test_initial_rescan_incremental_phase_selection(self):
        route=mappings(self.db)[0]
        jobs=BackupJobs()
        self.assertEqual(jobs.next_phase(self.db,route),"initial")
        self.db.execute("INSERT INTO batch_state VALUES (?,?,?,?,?,1,?)",(SRC,501,DST,3962,88000,88000))
        self.db.commit()
        self.assertEqual(jobs.next_phase(self.db,route),"incremental")
        self.db.execute("INSERT INTO topic_rescan VALUES (?,?)",(SRC,501))
        self.db.commit()
        self.assertEqual(jobs.next_phase(self.db,route),"rescan")


if __name__=="__main__":
    unittest.main()
