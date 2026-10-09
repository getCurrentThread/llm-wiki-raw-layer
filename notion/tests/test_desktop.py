import json
import sqlite3
import unittest
from notion.desktop import sync, rich_text
from tools.archiveio import load
from .fixtures import Fixture, PID, BID, UID, OTHER, COMMENT
from . import helpers as h


class DesktopTests(unittest.TestCase):
    def setUp(self):
        self.fx=Fixture()
        self.addCleanup(h.force_rmtree,self.fx.base)
        self.source=self.fx.base/'notion.db'
        self.con=sqlite3.connect(self.source)
        self.addCleanup(self.con.close)
        self.con.execute('PRAGMA journal_mode=WAL')
        self.con.executescript('''CREATE TABLE block(id TEXT,type TEXT,properties TEXT,content TEXT,parent_id TEXT,parent_table TEXT,
          alive INTEGER,created_time INTEGER,last_edited_time INTEGER,created_by_id TEXT,last_edited_by_id TEXT,crdt_data BLOB);
          CREATE TABLE notion_user(id TEXT,name TEXT);
          CREATE TABLE discussion(id TEXT,parent_id TEXT,resolved INTEGER);
          CREATE TABLE comment(id TEXT,parent_id TEXT,text TEXT,alive INTEGER,created_time INTEGER,last_edited_time INTEGER,created_by_id TEXT);''')
        self.insert(PID,'page','Synthetic page',[BID,OTHER])
        self.insert(BID,'text','synthetic desktop deployment',[])
        self.con.execute('INSERT INTO notion_user VALUES(?,?)',(UID,'Synthetic'))
        self.con.commit()
        self.path=self.fx.root/f'pages/{PID[:2]}/{PID}.json'
    def insert(self,ident,typ,title,content,alive=1,crdt=None):
        props=json.dumps({'title':[[title]]}) if title is not None else None
        self.con.execute('INSERT INTO block VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                         (ident,typ,props,json.dumps(content),PID,'block',alive,1727740800000,1727740800000,UID,UID,crdt))

    def test_wal_snapshot_readonly_capture_noop(self):
        before=self.source.read_bytes()
        report=sync(self.fx.root,self.source)
        self.assertEqual(report['missing_children'],1)
        self.assertEqual(report['pages_written'],1)
        self.assertEqual(load(self.path)['blocks'][BID]['paragraph']['rich_text'][0]['plain_text'],'synthetic desktop deployment')
        self.assertEqual(self.source.read_bytes(),before)
        self.assertEqual(sync(self.fx.root,self.source)['pages_written'],0)
        self.assertEqual(h.capture(self.fx),0)
        self.assertFalse(h.invariants(self.fx)[0])
        self.assertEqual(h.replay_diff(self.fx),0)

    def test_eviction_preserves_block_and_order(self):
        sync(self.fx.root,self.source)
        self.con.execute('DELETE FROM block WHERE id=?',(BID,))
        self.con.commit()
        self.assertEqual(sync(self.fx.root,self.source)['pages_written'],0)
        self.assertIn(BID,load(self.path)['blocks'])
        self.assertEqual(load(self.path)['order'],[[BID,0,None]])

    def test_explicit_trash_and_resolved_comments(self):
        self.con.execute('INSERT INTO discussion VALUES(?,?,?)',(OTHER,PID,0))
        self.con.execute('INSERT INTO comment VALUES(?,?,?,?,?,?,?)',(COMMENT,OTHER,json.dumps([['comment']]),1,1727740800000,1727740800000,UID))
        self.con.commit()
        sync(self.fx.root,self.source)
        self.con.execute('UPDATE discussion SET resolved=1')
        self.con.execute('UPDATE block SET alive=0 WHERE id=?',(PID,))
        self.con.commit()
        sync(self.fx.root,self.source)
        obj=load(self.path)
        self.assertTrue(obj['page']['in_trash'])
        self.assertIn('_removed_at',obj['comments'][COMMENT])

    def test_crdt_only_body_preserves_previous_snapshot(self):
        sync(self.fx.root,self.source)
        before=self.path.read_bytes()
        self.con.execute('UPDATE block SET properties=NULL,crdt_data=? WHERE id=?',(b'unsupported',BID))
        self.con.commit()
        report=sync(self.fx.root,self.source)
        self.assertEqual(report['crdt_only_blocks'],1)
        self.assertEqual(report['unsupported_pages'],1)
        self.assertEqual(self.path.read_bytes(),before)

    def test_authored_links_and_annotations(self):
        rich=rich_text([['text',[['b'],['a','https://example.org/?q=authored']]]])
        self.assertTrue(rich[0]['annotations']['bold'])
        self.assertEqual(rich[0]['text']['link']['url'],'https://example.org/?q=authored')
