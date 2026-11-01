# -*- coding: utf-8 -*-
"""The mattermost archive origin on synthetic archives: pinned genesis records and notes, an unchanged re-run, the ledger
invariants (tests/core/ledgercheck.py), posts with odd timestamps, files.json pointer validation, channel exclusion,
a channel path that tries to leave posts/, and the option's validation. Vanished, move, version order, unstable reads
and recovery are core rules, tested on the toy app in tests/core."""
import json
import shutil
import unittest

from rawlayer.capture import canon
from rawlayer.capture.config import Config, ConfigError
from mattermost.readers import archive as src_matter
from mattermost.tests.fixtures import Fixture, mm_file, post
from tests.core.appfix import _force, by_key, capture, invariants, manifests, records, replay_diff

LS = " "


def std_channels():
    f1 = mm_file("f1", "p1", "c1")
    return {
        "c1": {"path": "o/c1.jsonl", "posts": [
            post("p1", "c1", files=[f1]), post("p2", "c1", msg="a" + LS + "b"), post("p3", "c1", root="p1"),
            post("p4", "c1", root="px"), post("p5", "c1", file_ids=["fz"])]},
        "c2": {"path": "d/dm.jsonl", "posts": [post("p6", "c2", user="u9")]},
        "c3": {"path": "o/empty.jsonl", "posts": None, "post_count": 0},
    }


def set_cfg(fx, **kv):
    with fx.config_path.open(encoding="utf-8") as fh:
        cfg = json.load(fh)
    for k, v in kv.items():
        if v is None:
            cfg.pop(k, None)
        else:
            cfg[k] = v
    with fx.config_path.open("w", encoding="utf-8") as fh:
        json.dump(cfg, fh)


class Base(unittest.TestCase):
    def setUp(self):
        self.fx = Fixture()

    def tearDown(self):
        shutil.rmtree(self.fx.base, onerror=_force)


class TestMM(Base):
    def test_genesis_lines_and_rerun(self):
        self.fx.mm_write(std_channels(), partial_tail="c2", orphan=True)
        self.assertEqual(capture(self.fx), 0)
        recs = by_key(records(self.fx))
        kinds = sorted(r[0]["kind"] for r in recs.values())
        self.assertEqual(kinds.count("post"), 6)
        self.assertEqual(kinds.count("file"), 1)
        self.assertEqual(kinds.count("ch"), 3)
        self.assertEqual(kinds.count("user"), 1)
        self.assertEqual(recs["mm:post:p2"][0]["native"]["message"], "a" + LS + "b")
        self.assertEqual(recs["mm:post:p1"][0]["ts_ms"], 1_600_000_000_000)
        self.assertIsNone(recs["mm:file:f1"][0]["ts_ms"])
        self.assertEqual(recs["mm:user:u1"][0]["native"]["id"], "u1")
        ch = recs["mm:ch:c1"][0]["native"]
        self.assertEqual(sorted(ch), sorted(src_matter.CH_WHITELIST))
        m = manifests(self.fx)[0]["origins"]["mm_archive"]
        self.assertEqual(m["notes"]["partial_tail"], 1)
        self.assertEqual(m["notes"]["orphan_file"], 1)
        empty = m["containers"][canon.source_id("posts/o/empty.jsonl")]
        self.assertEqual(empty, {"status": "ok"})
        c1 = m["containers"][canon.source_id("posts/o/c1.jsonl")]
        self.assertEqual(c1["kinds"]["post"]["n"], 5)
        self.assertEqual(c1["versions"], 6)
        self.assertEqual(capture(self.fx), 0)                 # V3b
        self.assertEqual(manifests(self.fx)[1]["segments"], [])
        self.assertEqual(replay_diff(self.fx), 0)

    def test_ledger_invariants(self):
        self.fx.mm_write(std_channels())
        self.assertEqual(capture(self.fx), 0)
        bad, nums = invariants(self.fx)
        self.assertEqual(bad, {})
        self.assertEqual(nums["containers_checked"], 5)        # manifest.json, 3 channel files, users.json
        self.assertEqual(replay_diff(self.fx), 0)

    def test_post_without_create_at(self):
        """A post whose create_at is null or not an int is captured with ts_ms null, counted under day 'none'."""
        self.fx.mm_write({"c1": {"path": "o/c1.jsonl", "posts": [
            post("p1", "c1"), post("p5", "c1", create_at=None), post("p6", "c1", create_at="x")]}})
        self.assertEqual(capture(self.fx), 0)
        recs = by_key(records(self.fx))
        self.assertEqual([recs[k][0]["ts_ms"] for k in ("mm:post:p1", "mm:post:p5", "mm:post:p6")],
                         [1_600_000_000_000, None, None])
        c1 = manifests(self.fx)[0]["origins"]["mm_archive"]["containers"][canon.source_id("posts/o/c1.jsonl")]
        self.assertEqual(c1["days"], {canon.kst_day(1_600_000_000_000): 1, "none": 2})
        self.assertEqual(invariants(self.fx)[0], {})

    def test_files_json_pointers(self):
        sha = "ab" * 32
        good = {"post_id": "p1", "sha256": sha, "size": 3, "path": "files/f1/f1"}
        stored = {"f1": dict(good, extra="dropped"), "f2": dict(good, path="files/../f2"),
                  "f3": dict(good, path="files/f3/f3", sha256="x"), "f4": dict(good, path="files/f4/f4", size="3")}
        self.fx.mm_write(std_channels())
        self.fx.mm.joinpath("files.json").write_text(json.dumps(stored), encoding="utf-8")
        self.assertEqual(capture(self.fx), 0)
        recs = by_key(records(self.fx))
        self.assertEqual([(r["kind"], r["native"]) for r in recs["mm:blob:f1"]], [("blob", good)])
        self.assertFalse({"mm:blob:f2", "mm:blob:f3", "mm:blob:f4"} & set(recs))
        m = manifests(self.fx)[0]["origins"]["mm_archive"]
        self.assertEqual(m["notes"]["bad_line"], 3)
        self.assertEqual(m["containers"][canon.source_id("files.json")]["kinds"]["blob"]["n"], 1)
        self.assertEqual(invariants(self.fx)[1]["containers_checked"], 6)
        # files.json never shrinks: a pointer that vanishes makes the origin suspect
        self.fx.mm.joinpath("files.json").write_text("{}", encoding="utf-8")
        self.assertEqual(capture(self.fx), 3)
        self.assertEqual(replay_diff(self.fx), 0)

    def test_exclusion_and_slim(self):
        ch = std_channels()
        ch["c2"]["display"] = "My FEEDback room"
        ch["c1"]["posts"].append(post("p7", "c1", files=[mm_file("f7", "p7", "c1")], _files=[]))
        self.fx.mm_write(ch)
        set_cfg(self.fx, mm_exclude_patterns=["feedBack"])
        self.assertEqual(capture(self.fx), 0)
        keys = set(by_key(records(self.fx)))
        self.assertNotIn("mm:ch:c2", keys)
        self.assertNotIn("mm:post:p6", keys)
        self.assertIn("mm:post:p7", keys)
        self.assertNotIn("mm:file:f7", keys)
        notes = manifests(self.fx)[0]["origins"]["mm_archive"]["notes"]
        self.assertEqual((notes["excluded"], notes["slim_post"], notes.get("orphan_file")), (1, 1, None))
    def test_channel_path_cannot_leave_posts(self):
        ch = std_channels()
        ch["c9"] = {"path": "../evil.jsonl", "posts": [post("p99", "c9")]}
        self.fx.mm_write(ch)
        self.assertEqual(capture(self.fx), 0)
        self.assertNotIn("mm:post:p99", {r["key"] for r in records(self.fx)})
        self.assertGreaterEqual(manifests(self.fx)[0]["origins"]["mm_archive"]["notes"].get("bad_line"), 1)

    def test_exclude_option_types(self):
        for v in ("feedback", [""], [3]):
            with self.subTest(value=v):
                self.fx.write_config()
                set_cfg(self.fx, mm_exclude_patterns=v)
                with self.assertRaises(ConfigError):
                    Config(self.fx.config_path)
        self.fx.write_config()
        set_cfg(self.fx, mm_exclude_patterns=None)              # left out: the default (none excluded)
        self.assertEqual(Config(self.fx.config_path).mm_exclude, [])


if __name__ == "__main__":
    unittest.main()
