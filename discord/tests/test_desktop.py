import gzip
import json
import unittest
from discord.desktop import payloads, json_values, scan, sync
from .fixtures import Fixture, CID, MID, UID
from . import helpers as h


class DesktopTests(unittest.TestCase):
    def setUp(self):
        self.fx = Fixture()
        self.addCleanup(h.force_rmtree,self.fx.base)
        self.source=self.fx.base/'desktop'
        self.cache=self.source/'Cache/Cache_Data'
        self.cache.mkdir(parents=True)
        self.obj={'id':MID,'channel_id':CID,'guild_id':'300','type':0,'content':'synthetic local deployment',
                  'timestamp':'2024-10-01T00:00:00Z','author':{'id':UID,'username':'Synthetic'},'attachments':[],'embeds':[]}
        self.path=self.cache/'entry_0'
        self.save(self.obj)
    def save(self,obj):
        self.path.write_bytes(b'cache header\x00'+gzip.compress(json.dumps([obj]).encode())+b'cache footer')

    def test_scan_gzip_and_actual_archive_capture(self):
        before=self.path.read_bytes()
        _,messages,stats=scan(self.source)
        self.assertEqual(stats['messages_found'],1)
        self.assertEqual(messages[CID][0]['id'],MID)
        self.assertEqual(sync(self.fx.root,self.source)['messages_written'],1)
        self.assertEqual(h.capture(self.fx),0)
        self.assertFalse(h.invariants(self.fx)[0])
        self.assertEqual(h.replay_diff(self.fx),0)
        self.assertEqual(self.path.read_bytes(),before)
        self.assertEqual(sync(self.fx.root,self.source)['messages_written'],0)

    def test_eviction_does_not_delete_and_stale_edit_is_ignored(self):
        self.obj['edited_timestamp']='2024-10-03T00:00:00Z'
        self.save(self.obj)
        sync(self.fx.root,self.source)
        self.assertEqual(h.capture(self.fx),0)
        count=len(h.records(self.fx))
        self.obj.update(content='old body',edited_timestamp='2024-10-02T00:00:00Z')
        self.save(self.obj)
        self.assertEqual(sync(self.fx.root,self.source)['messages_written'],0)
        self.path.unlink()
        self.assertEqual(sync(self.fx.root,self.source)['messages_written'],0)
        self.assertEqual(h.capture(self.fx),0)
        self.assertEqual(len(h.records(self.fx)),count)

    def test_unknown_guild_remains_unclassified(self):
        self.obj.pop('guild_id')
        self.save(self.obj)
        self.assertEqual(scan(self.source)[2]['unclassified_channels'],1)
        sync(self.fx.root,self.source)
        self.assertEqual(h.capture(self.fx),0)

    def test_non_message_objects_and_credentials_not_imported(self):
        (self.source/'Local Storage').mkdir()
        (self.source/'Local Storage'/'token.json').write_text(json.dumps({'token':'private-secret','content':'not a message'}))
        self.path.write_bytes(b'header {"id":"1","author":{},"content":"not a message"}')
        self.assertEqual(scan(self.source)[2]['messages_found'],0)
        self.assertEqual(list(json_values(b'header {"a":1} tail')),[{'a':1}])
        self.assertEqual(len(list(payloads(b'not compressed'))),1)

    def test_later_guild_classification_preserves_partition_order(self):
        from tools.archiveio import load
        from discord.readers.archive import read_once
        self.obj.pop('guild_id')
        self.save(self.obj)
        sync(self.fx.root,self.source)
        self.obj.update(guild_id='400',content='new cached content')
        self.save(self.obj)
        sync(self.fx.root,self.source)
        ch=load(self.fx.root/'manifest.json')['channels'][CID]
        self.assertEqual(ch['guild_id'],'400')
        self.assertEqual(ch['files'],sorted(ch['files']))
        self.assertEqual(read_once(self.fx.root,[],set(),'dc_archive').notes['order_bad'],0)
