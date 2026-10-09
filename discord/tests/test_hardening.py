import json
import unittest
from discord.readers.archive import read_once, _safe_rel
from .fixtures import Fixture, CID, MID, UID, message
from . import helpers as h


class HardeningTests(unittest.TestCase):
    def setUp(self):
        self.fx = Fixture()
        self.addCleanup(h.force_rmtree, self.fx.base)

    def test_paths(self):
        for rel in ("../x.json", "exports/../x.json", "exports//x.json", "C:/x.json", "exports\\x.json"):
            self.assertFalse(_safe_rel(rel))
        man = json.loads((self.fx.root / "manifest.json").read_text())
        man["channels"][CID]["files"] = ["exports/../x.json"]
        self.fx.save("manifest.json", man)
        self.assertEqual(h.capture(self.fx), 75)

    def test_count_channel_order(self):
        for field, value, note in (("messageCount", 5, "count_mismatch"), ("channel", {"id": "wrong"}, "channel_mismatch")):
            with self.subTest(field=field):
                man = self.fx.write({CID: [[]]})
                rel = man["channels"][CID]["files"][0]
                obj = json.loads((self.fx.root / rel).read_text())
                obj[field] = value
                self.fx.save(rel, obj)
                rd = read_once(self.fx.root, [], set(), "dc_archive")
                self.assertEqual(rd.notes[note], 1)
                self.assertFalse(any(k.startswith("dc:msg") for k in rd.keys))
        man = self.fx.write({CID: [[], []]})
        man["channels"][CID]["files"].reverse()
        self.fx.save("manifest.json", man)
        self.assertEqual(read_once(self.fx.root, [], set(), "dc_archive").notes["order_bad"], 1)

    def test_orphan_tmp_bad_pk(self):
        self.fx.save("exports/orphan.json", {})
        self.fx.save("exports/ignored.tmp", {})
        rd = read_once(self.fx.root, [], set(), "dc_archive")
        self.assertEqual(rd.notes["orphan_file"], 1)
        self.fx.write({CID: [[{"id": "bad:id"}]]})
        self.assertEqual(read_once(self.fx.root, [], set(), "dc_archive").notes["pk_bad"], 1)

    def test_manifest_race(self):
        from unittest.mock import patch
        from rawlayer.capture import fsio
        from rawlayer.capture.origins import Unstable
        original = fsio.read_shared
        count = 0
        def read(path):
            nonlocal count
            data = original(path)
            if path.name == "manifest.json":
                count += 1
                if count == 2:
                    return data + b" "
            return data
        with patch("discord.readers.archive.fsio.read_shared", side_effect=read):
            with self.assertRaises(Unstable):
                read_once(self.fx.root, [], set(), "dc_archive")

    def test_nonfinite_partition_channel_rejected(self):
        man = json.loads((self.fx.root / "manifest.json").read_text())
        path = self.fx.root / man["channels"][CID]["files"][0]
        obj = json.loads(path.read_text())
        obj["messages"][0]["content"] = float("nan")
        path.write_text(json.dumps(obj), encoding="utf-8")
        rd = read_once(self.fx.root, [], set(), "dc_archive")
        self.assertEqual(rd.notes["bad_file"], 1)
        self.assertNotIn("dc:ch:" + CID, rd.keys)

    def test_message_count_requires_integer(self):
        for count in (1.0, True, "1"):
            with self.subTest(count=count):
                man = self.fx.write({CID: [[message()]]})
                rel = man["channels"][CID]["files"][0]
                obj = json.loads((self.fx.root / rel).read_text())
                obj["messageCount"] = count
                self.fx.save(rel, obj)
                rd = read_once(self.fx.root, [], set(), "dc_archive")
                self.assertEqual(rd.notes["count_mismatch"], 1)
                self.assertNotIn("dc:ch:" + CID, rd.keys)

    def test_projection_failure_rejects_only_affected_channel(self):
        self.fx.write({CID: [[message()]], MID: [[message()]]}, parents={MID: CID})
        self.assertEqual(h.capture(self.fx), 0)
        before = len(h.records(self.fx))
        self.fx.write({CID: [[message(text="healthy changed")]],
                       MID: [[message(), message(text="bad", embeds=[{"url": "http://["}])]]},
                      parents={MID: CID})
        rd = read_once(self.fx.root, [], set(), "dc_archive")
        self.assertEqual(rd.notes["bad_file"], 1)
        self.assertNotIn("dc:ch:" + MID, rd.keys)
        self.assertNotIn(f"dc:msg:{MID}:{MID}", rd.keys)
        self.assertEqual(h.capture(self.fx), 75)
        added = h.records(self.fx)[before:]
        self.assertEqual([r["key"] for r in added], [f"dc:msg:{CID}:{MID}"])
        self.assertFalse(h.invariants(self.fx)[0])
        self.assertEqual(h.replay_diff(self.fx), 0)

    def test_invalid_snowflakes_and_mismatched_user(self):
        self.fx.write({CID: [[message(mid) for mid in ("word", "0", str(2 ** 63), "9" * 5000)]]})
        self.fx.save("users.json", {UID: {"id": "different", "name": "Synthetic"}})
        rd = read_once(self.fx.root, [], set(), "dc_archive")
        self.assertEqual(rd.notes["pk_bad"], 5)
        self.assertFalse(any(k.startswith(("dc:msg:", "dc:user:")) for k in rd.keys))
