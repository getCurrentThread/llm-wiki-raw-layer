# -*- coding: utf-8 -*-
"""M2 (mm_archive) on synthetic archives: line handling, multi-version order (Q3), moves, vanished,
missing and empty channels, unstable manifest, exclusion, slim posts, os.replace concurrency, and the ledger
invariants (tests/core/ledgercheck.py)."""
import json
import shutil
import threading
import unittest
from pathlib import Path

from rawlayer.capture import canon, fsio
from mattermost.readers import archive as src_matter
from mattermost.tests.fixtures import Fixture, mm_file, post
from mattermost.tests.helpers import MM, _force, by_key, capture, invariants, manifests, records, replay_diff

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


class Base(unittest.TestCase):
    def setUp(self):
        self.fx = Fixture()

    def tearDown(self):
        shutil.rmtree(self.fx.base, onerror=_force)


class TestMM(Base):
    def test_genesis_lines_and_rerun(self):
        self.fx.mm_write(std_channels(), partial_tail="c2", orphan=True)
        self.assertEqual(capture(self.fx, MM), 0)
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
        self.assertEqual(capture(self.fx, MM), 0)                 # V3b
        self.assertEqual(manifests(self.fx)[1]["segments"], [])
        self.assertEqual(replay_diff(self.fx), 0)

    def test_ledger_invariants(self):
        self.fx.mm_write(std_channels())
        self.assertEqual(capture(self.fx, MM), 0)
        bad, nums = invariants(self.fx)
        self.assertEqual(bad, {})
        self.assertEqual(nums["containers_checked"], 5)        # manifest.json, 3 channel files, users.json
        self.assertEqual(replay_diff(self.fx), 0)

    def test_post_without_create_at(self):
        """A post whose create_at is null or not an int is captured with ts_ms null, counted under day 'none'."""
        self.fx.mm_write({"c1": {"path": "o/c1.jsonl", "posts": [
            post("p1", "c1"), post("p5", "c1", create_at=None), post("p6", "c1", create_at="x")]}})
        self.assertEqual(capture(self.fx, MM), 0)
        recs = by_key(records(self.fx))
        self.assertEqual([recs[k][0]["ts_ms"] for k in ("mm:post:p1", "mm:post:p5", "mm:post:p6")],
                         [1_600_000_000_000, None, None])
        c1 = manifests(self.fx)[0]["origins"]["mm_archive"]["containers"][canon.source_id("posts/o/c1.jsonl")]
        self.assertEqual(c1["days"], {canon.kst_day(1_600_000_000_000): 1, "none": 2})
        self.assertEqual(invariants(self.fx)[0], {})

    def test_versions_order_q3(self):
        ch = {"c1": {"path": "c1.jsonl", "posts": [post("p1", "c1", msg=m) for m in ("1", "3", "1", "2")]}}
        self.fx.mm_write(ch)
        self.assertEqual(capture(self.fx, MM), 0)
        revs = [(r["rev"], r["native"]["message"]) for r in by_key(records(self.fx))["mm:post:p1"]]
        self.assertEqual(revs, [(1, "1"), (2, "3"), (3, "2")])
        self.assertEqual(manifests(self.fx)[0]["origins"]["mm_archive"]["containers"][
            canon.source_id("posts/c1.jsonl")]["kinds"]["post"]["n"], 1)
        # same versions, different last one: only the representative is re-put (a reverted edit)
        ch["c1"]["posts"] = [post("p1", "c1", msg=m) for m in ("2", "3", "1")]
        self.fx.mm_write(ch)
        self.assertEqual(capture(self.fx, MM), 0)
        revs = [(r["rev"], r["native"]["message"]) for r in by_key(records(self.fx))["mm:post:p1"]]
        self.assertEqual(revs[3:], [(4, "1")])
        self.assertEqual(invariants(self.fx)[0], {})

    def test_move_is_one_put_not_vanished(self):
        ch = std_channels()
        self.fx.mm_write(ch)
        self.assertEqual(capture(self.fx, MM), 0)
        moved = ch["c1"]["posts"].pop(1)
        ch["c2"]["posts"].append(moved)
        ch["c1"]["post_count"], ch["c2"]["post_count"] = 4, 2
        self.fx.mm_write(ch)
        self.assertEqual(capture(self.fx, MM), 0)
        new = [r for r in records(self.fx) if r["seq"] == 2]
        self.assertEqual([(r["key"], r["rev"], r["src"]) for r in new],
                         [("mm:post:p2", 2, canon.source_id("posts/d/dm.jsonl"))])
        p2 = by_key(records(self.fx))["mm:post:p2"]
        self.assertEqual(p2[0]["h"], p2[1]["h"])
        self.assertEqual(invariants(self.fx)[0], {})             # same h, new src: not put_same_h (Q3)

    def test_vanished_is_suspect(self):
        ch = std_channels()
        self.fx.mm_write(ch)
        self.assertEqual(capture(self.fx, MM), 0)
        ch["c1"]["posts"].pop(1)
        ch["c1"]["posts"][0]["message"] = "edited"
        self.fx.mm_write(ch)
        self.assertEqual(capture(self.fx, MM), 3)
        m = manifests(self.fx)[1]
        self.assertEqual(m["origins"]["mm_archive"]["status"], "suspect")
        self.assertEqual(m["origins"]["mm_archive"]["notes"]["vanished"], 1)
        self.assertEqual(m["segments"], [])

    def test_missing_file_is_not_vanished(self):
        ch = std_channels()
        self.fx.mm_write(ch)
        self.assertEqual(capture(self.fx, MM), 0)
        ch["c2"]["posts"], ch["c2"]["post_count"] = None, 1
        self.fx.mm_write(ch)
        self.assertEqual(capture(self.fx, MM), 0)
        m = manifests(self.fx)[1]["origins"]["mm_archive"]
        self.assertEqual(m["containers"][canon.source_id("posts/d/dm.jsonl")]["status"], "missing")
        self.assertEqual(m["status"], "ok")
        self.assertEqual(manifests(self.fx)[1]["segments"], [])
        bad, nums = invariants(self.fx)
        self.assertEqual((bad, nums["containers_skipped"]), ({}, 1))   # the missing file is not compared

    def test_unstable_manifest(self):
        self.fx.mm_write(std_channels())
        real = fsio.read_shared
        calls = {"n": 0}

        def flaky(path, *a, **kw):
            data = real(path, *a, **kw)
            if Path(path).name == "manifest.json":
                calls["n"] += 1
                if calls["n"] % 2 == 0:
                    data += b" "
            return data
        src_matter.fsio.read_shared = flaky
        try:
            self.assertEqual(capture(self.fx, MM), 75)       # unstable = skipped (SPEC 13)
        finally:
            src_matter.fsio.read_shared = real
        m = manifests(self.fx)[0]
        self.assertEqual(m["origins"]["mm_archive"]["status"], "unstable")
        self.assertEqual(m["segments"], [])
        self.assertEqual(calls["n"], 6)

    def test_exclusion_and_slim(self):
        ch = std_channels()
        ch["c2"]["display"] = "My FEEDback room"
        ch["c1"]["posts"].append(post("p7", "c1", files=[mm_file("f7", "p7", "c1")], _files=[]))
        self.fx.mm_write(ch)
        with self.fx.config_path.open(encoding="utf-8") as fh:
            cfg = json.load(fh)
        cfg["mm_exclude_patterns"] = ["feedBack"]
        with self.fx.config_path.open("w", encoding="utf-8") as fh:
            json.dump(cfg, fh)
        self.assertEqual(capture(self.fx, MM), 0)
        keys = set(by_key(records(self.fx)))
        self.assertNotIn("mm:ch:c2", keys)
        self.assertNotIn("mm:post:p6", keys)
        self.assertIn("mm:post:p7", keys)
        self.assertNotIn("mm:file:f7", keys)
        notes = manifests(self.fx)[0]["origins"]["mm_archive"]["notes"]
        self.assertEqual((notes["excluded"], notes["slim_post"]), (1, 1))

    def test_read_shared_under_concurrent_os_replace(self):
        """Q4: the reader never fails and never sees a torn file. The writer's os.replace CAN fail while
        any handle is open (MoveFileExW ignores FILE_SHARE_DELETE), so that is measured, not asserted."""
        target = self.fx.base / "posts.jsonl"
        versions = [b"a" * 50000 + b"\r\n", b"b" * 70000 + b"\r\n"]
        target.write_bytes(versions[0])
        errors = {"replace": 0, "read": 0, "torn": 0, "replaced": 0}
        stop = threading.Event()

        def writer():
            i = 0
            tmp = target.with_name(target.name + ".tmp")
            while not stop.is_set():
                tmp.write_bytes(versions[i % 2])
                try:
                    tmp.replace(target)               # os.replace
                    errors["replaced"] += 1
                except PermissionError:
                    errors["replace"] += 1
                i += 1
        t = threading.Thread(target=writer)
        t.start()
        try:
            for _ in range(400):
                try:
                    data = fsio.read_shared(target)
                except OSError:
                    errors["read"] += 1
                    continue
                if data not in versions:
                    errors["torn"] += 1
        finally:
            stop.set()
            t.join()
        self.assertEqual((errors["read"], errors["torn"]), (0, 0))
        self.assertGreater(errors["replaced"], 0)


if __name__ == "__main__":
    unittest.main()
