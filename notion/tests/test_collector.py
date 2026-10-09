import copy
import json
import unittest
from unittest.mock import patch
from types import SimpleNamespace
from notion.collector import NotionClient, fetch_page, sync
from tools.archiveio import CollectorError, load, locked
from .fixtures import Fixture, PID, BID, OTHER, UID, COMMENT, block, page, rt
from . import helpers as h


class Client:
    requests = 0
    def __init__(self):
        self.value = page()
        self.fail = None
        self.directory = True
        self.comment_rows = []
        self.calls = []

    def request(self, path, method="GET", body=None, query=None):
        self.calls.append(path)
        self.requests += 1
        if path == "/v1/users/me":
            return {"id": UID, "object": "user", "name": "Synthetic"}
        if path == f"/v1/pages/{PID}":
            if self.fail == "page":
                raise CollectorError("api_error", 404)
            return copy.deepcopy(self.value["page"])
        raise AssertionError("unexpected endpoint")

    def items(self, path, method="GET", body=None, query=None):
        self.calls.append(path)
        if path == "/v1/users":
            if not self.directory:
                raise CollectorError("api_error", 403)
            yield {"id": UID, "name": "Synthetic"}
        elif path == "/v1/search":
            if not body["filter"]["in_trash"]:
                yield dict(self.value["page"], object="page")
        elif path == f"/v1/blocks/{PID}/children":
            if self.fail == "children":
                raise CollectorError("api_error", 404)
            yield from copy.deepcopy(list(self.value["blocks"].values()))
        elif path == "/v1/comments":
            if self.fail == "comments":
                raise CollectorError("api_error", 403)
            if query["block_id"] == PID:
                yield from copy.deepcopy(self.comment_rows)
        else:
            raise AssertionError("unexpected endpoint")


class CollectorTests(unittest.TestCase):
    def setUp(self):
        self.fx, self.client = Fixture(), Client()
        self.addCleanup(h.force_rmtree, self.fx.base)
        self.path = self.fx.root / f"pages/{PID[:2]}/{PID}.json"

    def test_collection_capture_and_noop(self):
        first = sync(self.fx.root, self.client, full=True)
        self.assertEqual(first["pages_fetched"], 1)
        self.assertEqual(h.capture(self.fx), 0)
        n = len(h.records(self.fx))
        report = sync(self.fx.root, self.client, full=True)
        self.assertEqual(report["pages_written"], 0)
        self.assertEqual(h.capture(self.fx), 0)
        self.assertEqual(len(h.records(self.fx)), n)
        self.assertFalse(h.invariants(self.fx)[0])
        self.assertEqual(h.replay_diff(self.fx), 0)

    def test_tombstones_and_gone_restore(self):
        self.client.comment_rows = [{"id": COMMENT, "rich_text": rt("synthetic comment")}]
        sync(self.fx.root, self.client, full=True)
        self.client.value["blocks"] = {}
        self.client.comment_rows = []
        sync(self.fx.root, self.client, full=True)
        obj = load(self.path)
        self.assertIn("_removed_at", obj["blocks"][BID])
        self.assertIn("_removed_at", obj["comments"][COMMENT])
        self.client.fail = "page"
        self.assertEqual(sync(self.fx.root, self.client, full=True)["pages_gone"], 1)
        self.assertIn("_gone_at", load(self.path))
        self.client.fail = None
        sync(self.fx.root, self.client, full=True)
        self.assertNotIn("_gone_at", load(self.path))

    def test_partial_children_or_comments_preserves_snapshot(self):
        sync(self.fx.root, self.client, full=True)
        previous = self.path.read_bytes()
        for failure in ("children", "comments"):
            with self.subTest(failure=failure):
                self.client.fail = failure
                report = sync(self.fx.root, self.client, full=True)
                self.assertEqual(report["errors"], 1)
                self.assertEqual(self.path.read_bytes(), previous)

    def test_comments_opt_out_preserves_known_comments(self):
        self.client.comment_rows = [{"id": COMMENT, "rich_text": rt("synthetic comment")}]
        sync(self.fx.root, self.client, full=True)
        self.client.fail = "comments"
        report = sync(self.fx.root, self.client, comments=False, full=True)
        self.assertEqual(report["errors"], 0)
        self.assertNotIn("_removed_at", load(self.path)["comments"][COMMENT])

    def test_directory_permission_is_optional(self):
        self.client.directory = False
        report = sync(self.fx.root, self.client, full=True)
        self.assertFalse(report["user_directory_available"])
        self.assertEqual(report["errors"], 0)
        self.assertEqual(report["pages_fetched"], 1)

    def test_skip_unchanged_but_full_sweep_rechecks_blocks(self):
        sync(self.fx.root, self.client, full=True)
        self.client.value["blocks"][BID] = block(text="changed without page timestamp")
        self.assertEqual(sync(self.fx.root, self.client)["pages_fetched"], 0)
        self.assertEqual(sync(self.fx.root, self.client, full=True)["pages_written"], 1)

    def test_identity_change_and_lock(self):
        sync(self.fx.root, self.client, full=True)
        original = self.client.request
        def request(path, **kwargs):
            return {"id": OTHER} if path == "/v1/users/me" else original(path, **kwargs)
        self.client.request = request
        with self.assertRaisesRegex(CollectorError, "account_changed"):
            sync(self.fx.root, self.client)
        with locked(self.fx.root):
            with self.assertRaisesRegex(CollectorError, "collector_busy"):
                sync(self.fx.root, self.client)

    def test_nested_synced_blocks_and_child_page_boundary(self):
        original = self.client.items
        self.client.value["blocks"][BID] = block(has_children=True, type="synced_block", synced_block={"synced_from":{"block_id":OTHER}})
        def items(path, **kwargs):
            if path == f"/v1/blocks/{OTHER}/children":
                yield block(COMMENT, has_children=True, type="child_page", child_page={"title":"child"})
            else:
                yield from original(path, **kwargs)
        self.client.items = items
        obj = fetch_page(self.client, PID)
        self.assertEqual(obj["order"], [[BID,0,None],[COMMENT,1,BID]])

    def test_client_paginated_request_and_retry(self):
        outputs = [SimpleNamespace(returncode=1, stdout=json.dumps({"object":"error","status":429}), stderr=""),
                   SimpleNamespace(returncode=0, stdout=json.dumps({"object":"list","results":[{"id":"a"}],"has_more":True,"next_cursor":"next"}),stderr=""),
                   SimpleNamespace(returncode=0, stdout=json.dumps({"object":"list","results":[{"id":"b"}],"has_more":False}),stderr="")]
        calls = []
        def runner(argv, **kwargs):
            calls.append((argv,kwargs))
            return outputs.pop(0)
        with patch("notion.collector.executable", return_value="ntn"):
            client=NotionClient(runner=runner,sleep=lambda delay:None)
        self.assertEqual(len(list(client.items('/v1/search','POST'))),2)
        self.assertIn('--data',calls[0][0])
        self.assertEqual(json.loads(calls[2][1]['input'])['start_cursor'],'next')
        self.assertEqual(calls[0][1]['env']['NOTION_API_BASE_URL'],'https://api.notion.com')

    def test_client_auth_error_redaction_and_cursor_guard(self):
        secret = "synthetic-private-token"
        runner=lambda *args,**kwargs:SimpleNamespace(returncode=1,stdout='',stderr='not logged in '+secret)
        with patch("notion.collector.executable", return_value="ntn"):
            client=NotionClient(runner=runner,sleep=lambda delay:None)
        with self.assertRaises(CollectorError) as raised:
            client.request('/v1/users/me')
        self.assertEqual(str(raised.exception),'auth_required')
        self.assertNotIn(secret,str(raised.exception))
        client.request=lambda *args,**kwargs:{'results':[],'has_more':True,'next_cursor':'same'}
        with self.assertRaisesRegex(CollectorError,'pagination_stalled'):
            list(client.items('/v1/users'))
