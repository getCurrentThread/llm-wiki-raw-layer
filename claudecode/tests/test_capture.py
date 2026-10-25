import json
import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from rawlayer.capture import canon, fsio
from rawlayer.capture.origins import Unstable
from tools.archiveio import CollectorError
from claudecode.origins import CcArchive
from claudecode.readers.archive import read_once
from .fixtures import Fixture, CID, MID, row
from . import helpers as h


class CaptureTests(unittest.TestCase):
    def setUp(self):
        self.fx = Fixture()
        self.addCleanup(h.force_rmtree, self.fx.base)

    def test_capture_genesis_noop_invariants_replay(self):
        self.fx.write({CID: [[row(), row("message-two", n=1)]]})
        self.assertEqual(h.capture(self.fx), 0)
        self.assertEqual(len(h.records(self.fx)), 2)
        self.assertEqual(h.capture(self.fx), 0)
        self.assertEqual(len(h.records(self.fx)), 2)
        self.assertFalse(h.invariants(self.fx)[0])
        self.assertEqual(h.replay_diff(self.fx), 0)

    def test_capture_added_partition_only_new_rows(self):
        self.assertEqual(h.capture(self.fx), 0)
        self.fx.write({CID: [[row()], [row("message-two", n=1)]]})
        self.assertEqual(h.capture(self.fx), 0)
        recs = h.records(self.fx)
        self.assertEqual(len(recs), 2)
        self.assertEqual([r["rev"] for r in recs], [1, 1])
        self.assertEqual(len(h.by_key(recs)[f"cc:msg:{CID}:{MID}"]), 1)

    def test_capture_damaged_partition_only_its_container(self):
        man = self.fx.write({CID: [[row()], [row("message-two")]], "session-two": [[row("other")]]})
        (self.fx.root / man["containers"][CID]["files"][-1]).write_text("{", encoding="utf-8")
        self.assertEqual(h.capture(self.fx), 75)
        self.assertEqual([r["key"] for r in h.records(self.fx)], ["cc:msg:session-two:other"])

    def test_capture_removed_row_suspect_commits_nothing(self):
        self.assertEqual(h.capture(self.fx), 0)
        self.fx.write({CID: [[]], "session-two": [[row("other")]]})
        self.assertEqual(h.capture(self.fx), 3)
        self.assertEqual(len(h.records(self.fx)), 1)

    def test_capture_manifest_change_unstable_retry(self):
        original = fsio.read_shared
        calls = 0

        def changing(path):
            nonlocal calls
            data = original(path)
            if path.name == "manifest.json":
                calls += 1
                if calls == 2:
                    return data + b" "
            return data

        with patch("claudecode.readers.archive.fsio.read_shared", side_effect=changing), \
                patch("rawlayer.capture.origins.time.sleep"):
            self.assertEqual(h.capture(self.fx), 0)
        self.assertGreaterEqual(calls, 4)

    def test_capture_manifest_change_raises_unstable(self):
        original = fsio.read_shared
        calls = 0

        def changing(path):
            nonlocal calls
            data = original(path)
            if path.name == "manifest.json":
                calls += 1
                return data + b" " * calls
            return data

        with patch("claudecode.readers.archive.fsio.read_shared", side_effect=changing):
            with self.assertRaises(Unstable):
                read_once(self.fx.root, frozenset(), "cc_archive")

    def test_capture_invalid_partitions(self):
        for field, value, note in (("rowCount", 2, "count_mismatch"), ("container", "other", "container_mismatch"),
                                   ("rows", [None], "bad_file")):
            with self.subTest(field=field):
                man = self.fx.write({CID: [[row()]], "session-two": [[row("other")]]})
                rel = man["containers"][CID]["files"][0]
                obj = json.loads((self.fx.root / rel).read_text())
                obj[field] = value
                self.fx.save(rel, obj)
                rd = read_once(self.fx.root, frozenset(), "cc_archive")
                self.assertEqual(rd.notes[note], 1)
                self.assertEqual(set(rd.keys), {"cc:msg:session-two:other"})
                self.assertEqual(rd.containers[canon.source_id(rel)]["status"], "error")

    def test_capture_bad_keys_timestamps_orphans_and_order(self):
        man = self.fx.write({CID: [[row(uuid="bad:key"), row("bad-time", ts="no"), row()]]})
        self.fx.save("exports/orphan/unused.json", {})
        rd = read_once(self.fx.root, frozenset(), "cc_archive")
        self.assertEqual((rd.notes["pk_bad"], rd.notes["ts_bad"], rd.notes["orphan_file"]), (1, 1, 1))
        man["containers"][CID]["files"] *= 2
        self.fx.save("manifest.json", man)
        self.assertEqual(read_once(self.fx.root, frozenset(), "cc_archive").notes["order_bad"], 1)

    def context(self, name="live"):
        return SimpleNamespace(cfg=SimpleNamespace(cc_sync=True),
                               inputs=SimpleNamespace(name=name, cc_archive=self.fx.root), denylist=frozenset())

    def test_capture_frozen_never_collects(self):
        sync = Mock(side_effect=AssertionError("collector called"))
        with patch.dict(sys.modules, {"claudecode.collector": SimpleNamespace(sync_config=sync)}):
            CcArchive().read_once(self.context("frozen"))
        sync.assert_not_called()

    def test_capture_live_collects_and_incomplete_errors(self):
        sync = Mock(return_value={"errors": 0})
        ctx = self.context()
        with patch.dict(sys.modules, {"claudecode.collector": SimpleNamespace(sync_config=sync)}), \
                patch.dict(os.environ, {"RAWLAYER_ARCHIVE_READY": "0"}), patch("rawlayer.capture.precheck.check") as check:
            CcArchive().read_once(ctx)
            sync.assert_called_once_with(ctx.cfg)
            check.assert_called_once_with(self.fx.root)
            sync.return_value = {"errors": 1}
            with self.assertRaisesRegex(CollectorError, "collection_incomplete"):
                CcArchive().read_once(ctx)

    def test_capture_archive_ready_never_collects(self):
        sync = Mock(side_effect=AssertionError("collector called"))
        with patch.dict(sys.modules, {"claudecode.collector": SimpleNamespace(sync_config=sync)}), \
                patch.dict(os.environ, {"RAWLAYER_ARCHIVE_READY": "1"}):
            CcArchive().read_once(self.context())
        sync.assert_not_called()
