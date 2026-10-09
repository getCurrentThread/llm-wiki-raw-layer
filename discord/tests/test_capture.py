import json
import unittest
from unittest.mock import patch
from rawlayer.capture import canon
from rawlayer.capture.origins import Unstable
from discord.readers.archive import msg_native
from discord.keys import snowflake_ms
from .fixtures import Fixture, CID, MID, message
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

    def test_versions_and_return(self):
        from .ledgerfix import rounds
        rounds(self.fx)
        recs = h.by_key(h.records(self.fx))[f"dc:msg:{CID}:{MID}"]
        self.assertEqual([r["rev"] for r in recs], [1, 2, 3])

    def test_two_versions_same_pass(self):
        self.fx.write({CID: [[message()], [message(text="changed")]]})
        self.assertEqual(h.capture(self.fx), 0)
        self.assertEqual(len(h.by_key(h.records(self.fx))[f"dc:msg:{CID}:{MID}"]), 2)

    def test_volatile_fields_no_revision(self):
        self.assertEqual(h.capture(self.fx), 0)
        n = len(h.records(self.fx))
        self.fx.write({CID: [[message(author={"id": "200", "name": "rotated", "avatarUrl": "https://x/?sig=2"},
                                     mentions=[{"id": "201"}], reactions=[{"count": 12}])]]})
        self.assertEqual(h.capture(self.fx), 0)
        self.assertEqual(len(h.records(self.fx)), n)

    def test_refresh_duplicate_same_signature(self):
        self.assertEqual(h.capture(self.fx), 0)
        self.fx.write({CID: [[message()], [message()]]})
        self.assertEqual(h.capture(self.fx), 0)
        self.assertEqual(len(h.by_key(h.records(self.fx))[f"dc:msg:{CID}:{MID}"]), 1)
        self.assertFalse(h.invariants(self.fx)[0])

    def test_thread_same_id_two_keys(self):
        self.fx.write({CID: [[message()]], MID: [[message()]]}, parents={MID: CID})
        self.assertEqual(h.capture(self.fx), 0)
        self.assertEqual(sum(r["kind"] == "msg" for r in h.records(self.fx)), 2)

    def test_vanished(self):
        self.assertEqual(h.capture(self.fx), 0)
        n = len(h.records(self.fx))
        self.fx.write({CID: [[]]})
        self.assertEqual(h.capture(self.fx), 3)
        self.assertEqual(len(h.records(self.fx)), n)

    def test_later_exclusion_room_gone(self):
        self.assertEqual(h.capture(self.fx), 0)
        n = len(h.records(self.fx))
        self.fx.write_config(["CHANNEL"])
        self.assertEqual(h.capture(self.fx), 0)
        self.assertEqual(len(h.records(self.fx)), n)
        self.assertGreater(h.manifests(self.fx)[-1]["origins"]["dc_archive"]["notes"]["room_gone"], 0)

    def test_channel_all_or_nothing(self):
        self.fx.write({CID: [[message()], [message(text="new")]]})
        self.assertEqual(h.capture(self.fx), 0)
        n = len(h.records(self.fx))
        man = json.loads((self.fx.root / "manifest.json").read_text())
        (self.fx.root / man["channels"][CID]["files"][-1]).write_text("{", encoding="utf-8")
        self.assertEqual(h.capture(self.fx), 75)
        self.assertEqual(len(h.records(self.fx)), n)

    def test_unstable(self):
        with patch("discord.origins.archive.read_once", side_effect=Unstable()), patch("rawlayer.capture.origins.time.sleep"):
            self.assertEqual(h.capture(self.fx), 75)

    def test_dm_and_snowflake_day(self):
        self.fx.write({CID: [[message()]]}, guild="0")
        self.assertEqual(h.capture(self.fx), 0)
        msg = next(r for r in h.records(self.fx) if r["kind"] == "msg")
        self.assertEqual(msg["ts_ms"], snowflake_ms(MID))
        self.assertTrue(canon.kst_day(msg["ts_ms"]).startswith("2024"))

    def test_projection_vector(self):
        a = message(attachments=[{"id": "a", "fileName": "a.txt", "fileSizeBytes": 4, "url": "https://x/?hm=1"}],
                    embeds=[{"title": "title", "url": "https://example.org/x?hm=1"}])
        b = json.loads(json.dumps(a))
        b["attachments"][0]["url"] = "https://x/?hm=2"
        b["embeds"][0]["url"] = "https://example.org/x?hm=2"
        self.assertEqual(canon.record_h(msg_native(a), {}), canon.record_h(msg_native(b), {}))
        b["content"] = "authored change"
        self.assertNotEqual(canon.record_h(msg_native(a), {}), canon.record_h(msg_native(b), {}))
        link = "https://example.org/?q=authored"
        self.assertEqual(msg_native(message(forwardedMessage={"content": link}))["forwardedMessage"]["content"], link)
