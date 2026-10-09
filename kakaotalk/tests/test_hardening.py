# -*- coding: utf-8 -*-
"""Adoption hardening (comparison report 2026-10-04, section 4) on the kakaotalk app's synthetic inputs: exit codes, the
edb partial run and sidecar, the app's option and SYSTEM precondition, room_gone for static rooms, and the disk
estimate. Recovery, ledger defects, config, provenance, srcmap shape, the lit() contract and real process kills are
core rules, tested on the toy app in tests/core."""
import shutil
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from kakaotalk.readers import edb as src_kakao_edb
from rawlayer.capture import canon, run
from kakaotalk.tests.fixtures import Fixture
from kakaotalk.tests.helpers import KAKAO, _force, capture, invariants, manifests, records
from kakaotalk.tests.test_m4 import EDB, EdbBase, rows


class Base(unittest.TestCase):
    def setUp(self):
        self.fx = Fixture().standard()

    def tearDown(self):
        shutil.rmtree(self.fx.base, onerror=_force)


class TestExit(Base):
    def test_every_origin_on_the_standard_inputs(self):
        """Every origin in one run (no edb room file): exit 0."""
        self.assertEqual(capture(self.fx, KAKAO), 0)
        m = manifests(self.fx)[-1]
        self.assertEqual({o: d["status"] for o, d in m["origins"].items()}, {o: "ok" for o in KAKAO})
        self.assertEqual(invariants(self.fx)[0], {})

    def test_error_origin_exits_75(self):
        """A container that cannot be read is skipped this run: exit 75 (SPEC 13), the container error."""
        (self.fx.backup / "101.sqlite").write_bytes(b"not a database" * 100)
        self.assertEqual(capture(self.fx), 75)
        o = manifests(self.fx)[-1]["origins"]["backup_decrypted"]
        self.assertEqual(o["containers"][canon.source_id("101.sqlite")]["status"], "error")
        self.assertEqual(o["notes"].get("read_error"), 1)

    def test_static_room_gone(self):
        """AB-6: a backup_decrypted room file that disappears -> note room_gone, no records."""
        self.assertEqual(capture(self.fx), 0)
        (self.fx.backup / "102.sqlite").unlink()
        self.assertEqual(capture(self.fx), 0)
        o = manifests(self.fx)[-1]["origins"]["backup_decrypted"]
        self.assertEqual((o["status"], o["notes"].get("room_gone"), o["counts"]), ("ok", 1, {"put": 0, "absent": 0}))
        self.assertEqual(manifests(self.fx)[-1]["segments"], [])


class TestPerOrigin(EdbBase):
    def test_partial_run_leaves_the_origin_out_of_its_manifest(self):
        """Q5: a run that does not capture edb writes no edb entry, so edb's newest manifest stays the suspect one."""
        self.room(91, rows(91, 1, 4))
        self.assertEqual(capture(self.fx, EDB), 0)
        (self.cd / "chatLogs_91.edb").unlink()
        self.assertEqual(capture(self.fx, EDB), 3)
        self.fx.standard()
        self.assertEqual(capture(self.fx, ["backup_decrypted"]), 0)
        mans = manifests(self.fx)
        self.assertEqual(sorted(mans[-1]["origins"]), ["backup_decrypted"])
        newest_edb = [m for m in mans if "edb" in m["origins"]][-1]
        self.assertEqual((newest_edb["seq"], newest_edb["origins"]["edb"]["status"]), (2, "suspect"))

    def test_sidecar_makes_room_unreadable(self):
        self.room(92, rows(92, 1, 4))
        self.room(93, rows(93, 1, 4))
        real = src_kakao_edb.dumper.iter_room_obs

        def wrapped(con, chat, *a, **kw):
            yield from real(con, chat, *a, **kw)
            if chat == "93":                    # the main file's name as SQLite reports it (text)
                Path(con.execute("PRAGMA database_list").fetchone()[2] + "-wal").write_bytes(b"")
        with mock.patch.object(src_kakao_edb.dumper, "iter_room_obs", wrapped):
            self.assertEqual(capture(self.fx, EDB), 0)
        c = self.cont(93)
        self.assertEqual((c["status"], self.edb()["notes"].get("sidecar_left")), ("unreadable", 1))
        self.assertEqual(self.cont(92)["status"], "clean")
        self.assertEqual([r for r in records(self.fx) if r["key"].startswith("kakao:93")], [])
        # the rolled-back room's records are out of the manifest count too (SPEC 9.3 counts.put)
        self.assertEqual(self.edb()["counts"]["put"], sum(1 for r in records(self.fx) if r["origin"] == "edb"))


class TestEstimatePeak(unittest.TestCase):
    def test_estimate_peak_counts_main_and_wal(self):
        d = Path(tempfile.mkdtemp(prefix="llmw-peak-"))
        try:
            for name, n in (("chatLogs_1.edb", 100), ("chatLogs_1.edb-wal", 50), ("chatLogs_2.edb", 120)):
                (d / name).write_bytes(b"\0" * n)
            inputs = types.SimpleNamespace(edb_dir=d)           # InputSet attributes are Paths
            self.assertEqual(run.estimate_peak(inputs, ["edb"]), 2 * 150 + 1024 ** 3)
            self.assertEqual(run.estimate_peak(inputs, ["backup_decrypted"]), 1024 ** 3)
        finally:
            shutil.rmtree(d)


if __name__ == "__main__":
    unittest.main()
