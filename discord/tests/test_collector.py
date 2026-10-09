import copy
import io
import json
import unittest
from urllib.error import HTTPError
from discord.collector import DiscordClient, dce_message, sync
from discord.readers.archive import read_once
from tools.archiveio import CollectorError, load, locked
from .fixtures import Fixture, CID, MID, UID
from . import helpers as h


def message(mid=MID, content="synthetic deployment"):
    return {"id":mid,"type":0,"content":content,"author":{"id":UID,"username":"Synthetic"},
            "timestamp":"2024-10-01T00:00:00Z","attachments":[],"embeds":[]}


class Client:
    is_bot = True
    requests = 0
    def __init__(self):
        self.rows = [message()]
        self.fail = False
    def authenticate(self):
        return {"id":UID,"bot":True}
    def request(self,path,query=None):
        if path == f"/channels/{CID}":
            return {"id":CID,"type":0,"guild_id":"300","name":"Synthetic"}
        if path == '/guilds/300':
            return {"id":"300","name":"Synthetic guild"}
        raise AssertionError("unexpected endpoint")
    def messages(self,cid,lower=0):
        if self.fail:
            raise CollectorError("api_error",403)
        yield from copy.deepcopy([m for m in self.rows if int(m['id'])>lower])


class Response:
    def __init__(self,value,headers=None):
        self.data=json.dumps(value).encode()
        self.headers=headers or {}
    def __enter__(self):return self
    def __exit__(self,*args):pass
    def read(self):return self.data


class CollectorTests(unittest.TestCase):
    def setUp(self):
        self.fx, self.client = Fixture(), Client()
        self.addCleanup(h.force_rmtree,self.fx.base)
    def collect(self):
        return sync(self.fx.root,self.client,[CID],refresh_days=10000)

    def test_immutable_delta_noop_and_capture(self):
        self.assertEqual(h.capture(self.fx),0)
        first=self.collect()
        self.assertEqual(first['messages_written'],1) # First API shape has explicit null edit time.
        self.assertEqual(h.capture(self.fx),0)
        man=load(self.fx.root/'manifest.json')
        before={p:(self.fx.root/p).read_bytes() for p in man['channels'][CID]['files']}
        report=self.collect()
        self.assertEqual(report['partitions_written'],0)
        self.assertEqual(h.capture(self.fx),0)
        self.assertEqual(before,{p:(self.fx.root/p).read_bytes() for p in before})
        self.client.rows[0]['content']='edited deployment'
        self.assertEqual(self.collect()['messages_written'],1)
        self.assertEqual(h.capture(self.fx),0)
        self.assertFalse(h.invariants(self.fx)[0])
        self.assertEqual(h.replay_diff(self.fx),0)

    def test_partial_deletion_tombstone_and_users_nonshrinking(self):
        other=str(int(MID)+1)
        self.client.rows.append(message(other))
        self.collect()
        self.client.rows=[message(other)]
        report=self.collect()
        self.assertEqual(report['tombstones'],1)
        rd=read_once(self.fx.root,[],set(),'dc_archive')
        key=f'dc:msg:{CID}:{MID}'
        self.assertIn('_removedAt',rd.keys[key][rd.last_h[key]].ob.native)
        self.assertIn(UID,load(self.fx.root/'users.json'))

    def test_permission_failure_or_empty_refresh_keeps_cursor_and_files(self):
        self.collect()
        old=load(self.fx.root/'manifest.json')['channels'][CID]
        for fail in (True,False):
            self.client.fail=fail
            self.client.rows=[]
            report=self.collect()
            self.assertEqual(report['errors'],1)
            ch=load(self.fx.root/'manifest.json')['channels'][CID]
            self.assertEqual(ch['files'],old['files'])
            self.assertEqual(ch['last_message_id'],old['last_message_id'])

    def test_rotation_ignored_content_restriction_guard(self):
        self.client.rows[0]['attachments']=[{'id':'400','filename':'synthetic.txt','size':4,'url':'https://cdn/file?sig=1'}]
        self.collect()
        self.client.rows[0]['attachments'][0]['url']='https://cdn/file?sig=2'
        self.assertEqual(self.collect()['messages_written'],0)
        self.client.rows=[message(content='')]
        self.assertEqual(self.collect()['errors'],1)

    def test_key_projection_reply_forward_sticker(self):
        obj=message()
        obj.update(message_reference={'message_id':MID,'channel_id':CID},
                   sticker_items=[{'id':'400','name':'sticker'}],
                   message_snapshots=[{'message':{'content':'forwarded'}}])
        result=dce_message(obj)
        self.assertEqual(result['reference']['messageId'],MID)
        self.assertEqual(result['forwardedMessage'][0]['content'],'forwarded')
        self.assertEqual(result['stickers'][0]['name'],'sticker')

    def test_collector_lock_and_bad_archive(self):
        with locked(self.fx.root):
            with self.assertRaisesRegex(CollectorError,'collector_busy'):
                self.collect()
        man=load(self.fx.root/'manifest.json')
        man['channels'][CID]['files']=['exports/../escape.json']
        self.fx.save('manifest.json',man)
        self.assertEqual(self.collect()['errors'],1)

    def test_backward_pagination_covers_burst_over_100(self):
        all_rows=[message(str(int(MID)+i)) for i in range(250)]
        queries=[]
        client=DiscordClient('synthetic-token',sleep=lambda delay:None)
        def request(path,query=None):
            queries.append(query)
            eligible=[r for r in all_rows if not query.get('before') or int(r['id'])<int(query['before'])]
            return list(reversed(eligible))[:100]
        client.request=request
        rows=list(client.messages(CID,int(MID)))
        self.assertEqual(len(rows),249)
        self.assertEqual(len({m['id'] for m in rows}),249)
        self.assertEqual(len(queries),3)

    def test_rate_limit_retry_and_bot_user_autodetection(self):
        calls=[]
        responses=[HTTPError('https://discord.com',429,'rate limited',{'Retry-After':'0'},io.BytesIO(b'{"retry_after":0}')),
                   HTTPError('https://discord.com',401,'unauthorized',{},io.BytesIO(b'{}')),
                   Response({'id':UID})]
        def opener(req,timeout=None):
            calls.append(req.get_header('Authorization'))
            value=responses.pop(0)
            if isinstance(value,Exception):raise value
            return value
        client=DiscordClient('synthetic-token',opener=opener,sleep=lambda delay:None)
        self.assertEqual(client.authenticate()['id'],UID)
        self.assertEqual(calls,['Bot synthetic-token','Bot synthetic-token','synthetic-token'])
        self.assertFalse(client.is_bot)

    def test_client_auth_redaction(self):
        def opener(*args,**kwargs):
            raise HTTPError('https://discord.com',401,'private-secret',{},io.BytesIO(b'private-secret'))
        client=DiscordClient('private-secret',token_type='bot',opener=opener,sleep=lambda delay:None)
        with self.assertRaises(CollectorError) as raised:
            client.authenticate()
        self.assertEqual(str(raised.exception),'auth_failed')
        self.assertNotIn('private-secret',str(raised.exception))
