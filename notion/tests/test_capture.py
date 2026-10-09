import unittest
from rawlayer.capture import canon
from notion.readers.project import project
from .fixtures import Fixture, page, block, BID, PID, OTHER, COMMENT, rt
from . import helpers as h


class CaptureTests(unittest.TestCase):
    def setUp(self):
        self.fx = Fixture()
        self.addCleanup(h.force_rmtree, self.fx.base)

    def test_genesis_noop_invariants_replay(self):
        self.assertEqual(h.capture(self.fx), 0)
        n = len(h.records(self.fx))
        self.assertEqual(h.capture(self.fx), 0)
        self.assertEqual(len(h.records(self.fx)), n)
        self.assertFalse(h.invariants(self.fx)[0])
        self.assertEqual(h.replay_diff(self.fx), 0)

    def test_block_edit_only(self):
        self.assertEqual(h.capture(self.fx), 0)
        n = len(h.records(self.fx))
        self.fx.write(page(blocks={BID: block(text="changed deployment")}))
        self.assertEqual(h.capture(self.fx), 0)
        self.assertEqual([r["kind"] for r in h.records(self.fx)[n:]], ["block"])

    def test_insert_and_order_no_existing_block_revisions(self):
        self.assertEqual(h.capture(self.fx), 0)
        self.fx.write(page(blocks={BID: block(), OTHER: block(OTHER, "new")}, order=[[OTHER, 0, None], [BID, 0, None]]))
        self.assertEqual(h.capture(self.fx), 0)
        by = h.by_key(h.records(self.fx))
        self.assertEqual(len(by[f"nt:block:{PID}:{BID}"]), 1)
        self.assertEqual(len(by[f"nt:page:{PID}"]), 2)

    def test_move_and_synced_blocks(self):
        self.assertEqual(h.capture(self.fx), 0)
        self.fx.write(page(blocks={BID: block(_removed_at="2024-10-02T00:00:00Z")}, order=[]))
        self.fx.write(page(pid=OTHER))
        self.assertEqual(h.capture(self.fx), 0)
        by = h.by_key(h.records(self.fx))
        self.assertEqual(len(by[f"nt:block:{PID}:{BID}"]), 2)
        self.assertIn(f"nt:block:{OTHER}:{BID}", by)

    def test_url_rotation_no_rev(self):
        a = block(type="image", image={"type": "file", "file": {"url": "https://cdn/x?sig=1", "expiry_time": "a"}})
        self.fx.write(page(blocks={BID: a}))
        self.assertEqual(h.capture(self.fx), 0)
        n = len(h.records(self.fx))
        a["image"]["file"] = {"url": "https://cdn/x?sig=2", "expiry_time": "b"}
        self.fx.write(page(blocks={BID: a}))
        self.assertEqual(h.capture(self.fx), 0)
        self.assertEqual(len(h.records(self.fx)), n)

    def test_comment_tombstone_trash_gone(self):
        value = page(comments={COMMENT: {"id": COMMENT, "rich_text": rt("comment")}})
        self.fx.write(value)
        self.assertEqual(h.capture(self.fx), 0)
        value["comments"][COMMENT]["_removed_at"] = "2024-10-02T00:00:00Z"
        value["page"]["in_trash"] = True
        value["_gone_at"] = "2024-10-02T00:00:00Z"
        self.fx.write(value)
        self.assertEqual(h.capture(self.fx), 0)
        by = h.by_key(h.records(self.fx))
        self.assertEqual(len(by[f"nt:comment:{PID}:{COMMENT}"]), 2)
        self.assertEqual(len(by[f"nt:page:{PID}"]), 2)

    def test_uuid_normalization_dedupe(self):
        from notion.readers.archive import _page
        obj = page()
        obj["blocks"] = [block(), block(text="last")]
        obj["order"] *= 2
        obj["page"]["id"] = PID.replace("-", "").upper()
        obs = _page(obj, PID)
        self.assertEqual(len(obs), 2)
        self.assertEqual(len(obs[0].derived["order"]), 1)

    def test_projection_vector(self):
        a = {"icon": {"type": "file", "file": {"url": "https://cdn/x?sig=1", "expiry_time": "a"}}}
        b = {"icon": {"type": "file", "file": {"url": "https://cdn/x?sig=2", "expiry_time": "b"}}}
        self.assertEqual(canon.record_h(project(a), {}), canon.record_h(project(b), {}))

    def test_projection_preserves_authored_links_and_schema(self):
        value = {"paragraph": {"rich_text": [{"text": {"link": {"url": "https://example.org/?q=important"}}}]},
                 "properties": {"formula": {"type": "rich_text", "rich_text": rt("authored")},
                                "computed": {"type": "formula", "formula": {"type": "string", "string": "one"}}}}
        result = project(value)
        self.assertIn("?q=important", result["paragraph"]["rich_text"][0]["text"]["link"]["url"])
        self.assertIn("formula", result["properties"])
        self.assertNotIn("formula", result["properties"]["computed"])
        schema = {"type": "formula", "formula": {"expression": "1+2"}}
        self.assertEqual(project(schema), schema)
        image = {"image": {"caption": [{"text": {"link": {"url": "https://example.org/?q=authored"}}}]}}
        self.assertEqual(project(image), image)
