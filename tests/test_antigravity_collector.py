"""Public fixtures use only synthetic SQLite/protobuf records."""
from contextlib import closing
import json
import sqlite3
import tempfile
import unittest
import uuid
from pathlib import Path

from knowledge_bridge.antigravity_source import _media
from knowledge_bridge.four_local_collectors import LocalCompletedCollector
from knowledge_bridge.workspace import initialize_workspace


def var(n):
    data = bytearray()
    while n > 127:
        data.append(128 | (n & 127))
        n >>= 7
    return bytes(data) + bytes([n])


def field(k, val):
    if isinstance(val, int):
        return var(k*8) + var(val)
    if isinstance(val, str):
        val = val.encode()
    return var(k*8+2) + var(len(val)) + val


def step(kind, text, mid, hidden=False):
    user = kind == 14
    md = field(3,4 if user else 2)
    body = field(2 if user else 1,text) + field(19 if user else 6,mid)
    if not user:
        body += field(4,"private thinking") + field(5,"tool arguments")
    return (kind,3,0,field(1,kind)+field(4,3)+field(5,md)+field(19 if user else 20,body),
            field(4,1) if hidden else b"")


class AntigravityCollectorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.context = initialize_workspace(self.root/"workspace")
        self.base = self.root/".gemini/antigravity"
        self.directory = self.base/"conversations"
        self.directory.mkdir(parents=True)
        self.index = self.base/"conversation_summaries.db"
        with closing(sqlite3.connect(self.index)) as db, db:
            db.execute("create table conversation_summaries(conversation_id,title,last_modified_time)")
        self.ad = LocalCompletedCollector("antigravity", self.context, source_root=self.root)

    def fixture(self, rows):
        sid = str(uuid.uuid4())
        path = self.directory/(sid+".db")
        with closing(sqlite3.connect(self.index)) as db, db:
            db.execute("insert into conversation_summaries values (?,?,?)",(sid,"test",1))
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("create table trajectory_meta(cascade_id)")
            db.execute("insert into trajectory_meta values (?)",(sid,))
            db.execute("create table steps(idx integer primary key,step_type,status,step_format,step_payload,render_info)")
            db.executemany("insert into steps values (?,?,?,?,?,?)",[(i,*r) for i,r in enumerate(rows)])
        db.close()
        return sid,path

    def enable(self, policy="all", ids=()):
        self.ad.configure(policy,ids,owner_confirmed=True)

    def test_visible_steps_preserve_code_and_intermediate_text_but_not_tools(self):
        sid,path = self.fixture([step(14,"保留 `<tool>` 和代码","u"),step(15,"Checking.","a1"),
                           (132,3,0,b"command output",b""),step(15,"hidden","x",True),
                           step(15,"\n```py\nprint(1)\n```\n","a2"),step(14,"unanswered","u2")])
        self.assertEqual(0,self.ad.sync()["saved_this_sync"])
        before = path.read_bytes()
        self.enable()
        status=self.ad.sync()
        self.assertEqual(4,status["saved_this_sync"],status["sessions"])
        self.assertEqual(0,self.ad.sync()["saved_this_sync"])
        rows=[json.loads(line) for p in self.context.raw_message_store.glob("*.jsonl")
              for line in p.read_text("utf8").splitlines()]
        self.assertEqual(["保留 `<tool>` 和代码","Checking.","\n```py\nprint(1)\n```\n","unanswered"],
                         [r["original_text"] for r in rows])
        self.assertEqual(before,path.read_bytes())
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("update steps set step_payload=? where idx=0",(step(14,"edited","u")[3],))
        db.close()
        self.assertIsNotNone(self.ad.sync()["sessions"][sid]["last_error"])
        self.assertEqual(4,self.ad.status()["history"]["archived_messages"])
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("delete from steps where idx=1")
            db.execute("insert into steps values (?,?,?,?,?,?)",(6,*step(15,"new answer","a3")))
        update=self.ad.sync()
        self.assertEqual(1,update["saved_this_sync"])
        self.assertIsNotNone(update["sessions"][sid]["last_error"])
        self.assertEqual(0,self.ad.sync()["saved_this_sync"])
        self.assertEqual(5,self.ad.status()["history"]["archived_messages"])

    def test_wal_blocks_only_affected_session_and_retries_without_touching_source(self):
        sid,path=self.fixture([step(14,"one","u")])
        self.fixture([step(14,"two","v")])
        self.enable()
        wal=Path(str(path)+"-wal")
        wal.write_bytes(b"uncommitted")
        first=self.ad.sync()
        self.assertEqual(1,first["saved_this_sync"])
        self.assertIsNotNone(first["sessions"][sid]["last_error"])
        self.assertIsNone(first["last_sync_at"])
        self.assertEqual(b"uncommitted",wal.read_bytes())
        wal.unlink()
        self.assertEqual(1,self.ad.sync()["saved_this_sync"])
        self.assertEqual(2,self.ad.status()["history"]["archived_messages"])

    def test_external_media_is_reference_only(self):
        outside=self.root/"other.jpg"
        outside.write_bytes(b"outside")
        a=_media(field(5,str(outside)), self.directory,0)
        self.assertIsNone(a.local_path)
        self.assertIsNone(a.inline_data)
