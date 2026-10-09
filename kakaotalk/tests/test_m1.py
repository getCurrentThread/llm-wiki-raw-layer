# -*- coding: utf-8 -*-
"""M1 (V8 basics) on the kakaotalk app's synthetic inputs: types, 2^53, pk_bad, schema drift, vanished, determinism,
and the ledger invariants (tests/core/ledgercheck.py: seal, policy, facts). The core rules M1 also
covered (lock, crash points, environment, spill/part boundaries, canon, publicout) are tested on the toy app in
tests/core. Run from the project root (the folder that holds manage.py):

    python -s -B -m unittest discover -s kakaotalk/tests -t .
"""
import gzip
import shutil
import sqlite3
import unittest

from rawlayer.capture import canon, fsio
from rawlayer.capture.config import Config
from rawlayer.capture.ledger import Ledger
from kakaotalk.tests.fixtures import BIG, Fixture, make_room
from kakaotalk.tests.helpers import _force, capture, export, invariants, manifests, records, replay_diff


class Base(unittest.TestCase):
    def setUp(self):
        self.fx = Fixture().standard()

    def tearDown(self):
        shutil.rmtree(self.fx.base, onerror=_force)


class TestCapture(Base):
    def test_genesis_types_and_rerun(self):
        self.assertEqual(capture(self.fx), 0)
        recs = {r["key"]: r for r in records(self.fx)}
        big = recs[f"kakao:102:msg:{BIG}"]
        self.assertEqual(big["native"]["logId"], BIG)
        self.assertEqual(big["native"]["message"], {"$b64": "/2Jpbg=="})
        self.assertIsNone(recs["kakao:101:msg:1"]["native"]["attachment"])
        self.assertEqual(recs["kakao:101:attachment:3"]["native"]["attach_fullData"], {"$b64": "AAE="})
        self.assertIn("kakao:101:hist:1:1", recs)
        self.assertIn("kakao:101:meta:2:3", recs)
        self.assertEqual(recs["kakao:101"]["native"], {"chatId": "101"})            # Q1
        self.assertEqual(recs["kakao:101"]["derived"], {"from": "a", "rows": 3, "to": "b"})
        for r in recs.values():
            self.assertEqual(r["h"], canon.record_h(r["native"], r["derived"]))
        blob = b"".join(gzip.open(Config(self.fx.config_path).raw.joinpath(*s["path"].split("/"))).read()
                        for m in manifests(self.fx) for s in m["segments"])
        self.assertNotIn(b"secret draft", blob)
        m1 = manifests(self.fx)[0]
        f101 = m1["origins"]["backup_decrypted"]["containers"][canon.source_id("101.sqlite")]
        self.assertEqual(f101["tables"]["chatLogDrafts"], 1)
        self.assertEqual(m1["origins"]["backup_decrypted"]["notes"]["not_room"], 1)
        # V3a: nothing changes -> no records, manifest without segments
        self.assertEqual(capture(self.fx), 0)
        m2 = manifests(self.fx)[1]
        self.assertEqual(m2["segments"], [])
        self.assertEqual(m2["prev_sha256"], fsio.sha256_file(Ledger(Config(self.fx.config_path).raw).manifests()[0][2]))
        self.assertEqual(replay_diff(self.fx), 0)

    def test_ledger_invariants(self):
        """The former verify C2, C10, V4, V5 on a genesis of the standard inputs, now from tests/core/ledgercheck.py."""
        self.assertEqual(capture(self.fx), 0)
        bad, nums = invariants(self.fx)
        self.assertEqual(bad, {})
        self.assertEqual(nums["containers_skipped"], 0)
        self.assertEqual(nums["containers_checked"], 4)        # merged, 101, 102, 201
        self.assertEqual(replay_diff(self.fx), 0)
        # the merged rows of the standard fixture: 10 messages from three rooms (2^53 row included)
        self.assertEqual(sum(1 for r in records(self.fx) if r["origin"] == "merged_out" and r["kind"] == "msg"), 10)

    def test_pk_bad(self):
        p = self.fx.backup / "103.sqlite"
        make_room(p, 103, 2)
        con = sqlite3.connect(p)
        con.executescript("ALTER TABLE chatLogMeta RENAME TO m0;"
                          "CREATE TABLE chatLogMeta(logId BLOB, revision INTEGER, type TEXT, content TEXT, extra TEXT,"
                          " PRIMARY KEY(logId, type));")
        con.execute("INSERT INTO chatLogMeta VALUES (?,?,?,?,?)", (b"\x01", 0, "t", "c", None))
        con.execute("INSERT INTO chatLogMeta VALUES (?,?,?,?,?)", (5, 0, "a:b", "c", None))
        con.execute("INSERT INTO chatLogMeta VALUES (?,?,?,?,?)", (6, 0, "ok", "c", None))
        con.commit()
        con.close()
        self.assertEqual(capture(self.fx, ["backup_decrypted"]), 0)
        keys = {r["key"] for r in records(self.fx)}
        self.assertIn("kakao:103:meta:6:ok", keys)
        self.assertEqual(manifests(self.fx)[0]["origins"]["backup_decrypted"]["notes"]["pk_bad"], 2)

    def test_schema_drift(self):
        self.assertEqual(capture(self.fx), 0)
        p = self.fx.backup / "101.sqlite"
        con = sqlite3.connect(p)
        con.execute("ALTER TABLE chatLogs ADD COLUMN extra2 INTEGER DEFAULT 0")
        con.commit()
        con.close()
        self.assertEqual(capture(self.fx), 0)
        new = [r for r in records(self.fx) if r["seq"] == 2]
        self.assertEqual([(r["key"], r["rev"]) for r in new], [("kakao:101@chatLogs", 2)])
        con = sqlite3.connect(p)
        con.execute("UPDATE chatLogs SET extra2=5 WHERE logId=2")
        con.commit()
        con.close()
        self.assertEqual(capture(self.fx), 0)
        new = [r for r in records(self.fx) if r["seq"] == 3]
        self.assertEqual([(r["key"], r["rev"], r["native"].get("extra2")) for r in new], [("kakao:101:msg:2", 2, 5)])
        self.assertEqual(replay_diff(self.fx), 0)

    def test_vanished_is_suspect_and_not_committed(self):
        self.assertEqual(capture(self.fx), 0)
        con = sqlite3.connect(self.fx.backup / "102.sqlite")
        con.execute("DELETE FROM chatLogs WHERE logId=1")
        con.execute("UPDATE chatLogs SET message='changed' WHERE logId=2")
        con.commit()
        con.close()
        con = sqlite3.connect(self.fx.dlive / "201.sqlite")
        con.execute("UPDATE chatLogs SET message='changed' WHERE logId=1")
        con.commit()
        con.close()
        self.assertEqual(capture(self.fx), 3)
        m = manifests(self.fx)[1]
        self.assertEqual(m["origins"]["backup_decrypted"]["status"], "suspect")
        self.assertEqual(m["origins"]["backup_decrypted"]["notes"]["vanished"], 1)
        self.assertEqual(m["origins"]["backup_decrypted"]["counts"]["put"], 0)
        self.assertEqual({s["origin"] for s in m["segments"]}, {"decrypted_live"})
        bad, nums = invariants(self.fx)
        self.assertEqual(bad, {})                                   # Q2: facts skip the suspect origin
        self.assertEqual((nums["containers_checked"], nums["containers_skipped"]), (2, 2))   # backup: 101, 102

    def test_determinism_two_roots(self):
        self.assertEqual(capture(self.fx), 0)
        a = export(self.fx)
        self.fx.write_config(self.fx.base / "data2")
        self.assertEqual(capture(self.fx), 0)
        self.assertEqual(export(self.fx), a)
        self.assertEqual(len(a), len({(o, k) for o, k, *_ in a}))


if __name__ == "__main__":
    unittest.main()
