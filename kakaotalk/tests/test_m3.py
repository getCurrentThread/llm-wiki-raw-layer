# -*- coding: utf-8 -*-
"""M3 (live_out): V8 source-unchanged with a collector-like writer holding the WAL database open
(SPEC-CLARIFICATIONS Q6), captured_at -> src_asof_s, updated_at dropped, live next to the static origins, and a
producer-side change (cursor count, one message) captured as one put each."""
import hashlib
import shutil
import sqlite3
import time
import unittest
from pathlib import Path

from kakaotalk.tests.fixtures import Fixture, T0
from kakaotalk.tests.helpers import _force, capture, invariants, manifests, records, replay_diff

LIVE = ["live_out"]
LIVE_DDL = """
CREATE TABLE messages(chatId TEXT, logId TEXT, sendAt INTEGER, authorId TEXT, type INTEGER, deleted INTEGER,
  message TEXT, sender TEXT, type_name TEXT, date TEXT, source TEXT, captured_at INTEGER,
  PRIMARY KEY(chatId, logId));
CREATE TABLE cursors(chatId TEXT PRIMARY KEY, last_logId TEXT, title TEXT, count INTEGER, first_ts INTEGER,
  last_ts INTEGER, updated_at INTEGER);
"""


def live_db(path, rooms):
    """rooms: {chatId(int): [(logId, message)...]}. Returns the open writer connection (WAL mode)."""
    path = Path(path)
    if path.exists():
        path.unlink()
    w = sqlite3.connect(path)
    w.execute("PRAGMA journal_mode=WAL")
    w.executescript(LIVE_DDL)
    for chat, rows in rooms.items():
        for lid, msg in rows:
            w.execute("INSERT INTO messages VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                      (str(chat), str(lid), T0 + lid * 3600, "7", 1, 0, msg, "s", "t", "d", "live", T0 + 5))
        w.execute("INSERT INTO cursors VALUES (?,?,?,?,?,?,?)",
                  (str(chat), str(max(r[0] for r in rows)), "r", len(rows), T0, T0 + 9, int(time.time())))
    w.commit()
    return w


def snap(path):
    path = Path(path)
    out = {}
    for suf in ("", "-wal", "-shm"):
        p = path.with_name(path.name + suf)
        if p.exists():
            st = p.stat()
            out[suf or "main"] = (st.st_size, st.st_mtime_ns, hashlib.sha256(p.read_bytes()).hexdigest())
    return out


class TestLive(unittest.TestCase):
    def setUp(self):
        self.fx = Fixture().standard()
        # live overlaps merged on chat 101 (same rows) and adds chat 301
        self.w = live_db(self.fx.live, {101: [(1, "m101-1"), (2, "m101-2"), (3, "m101-3")],
                                        301: [(10, "x"), (11, "y")]})

    def tearDown(self):
        self.w.close()
        shutil.rmtree(self.fx.base, onerror=_force)

    def test_source_unchanged_and_fields(self):
        self.w.execute("INSERT INTO messages VALUES ('301','12',1600043200,'7',1,0,'z','s','t','d','live',1)")
        self.w.commit()                                   # leave frames in the WAL, writer still open
        before = snap(self.fx.live)
        self.assertIn("-wal", before)
        time.sleep(1.1)
        self.assertEqual(capture(self.fx, LIVE), 0)
        after = snap(self.fx.live)
        for part in ("main", "-wal"):
            self.assertEqual(before[part], after[part], part)
        self.assertEqual(before["-shm"][:2], after["-shm"][:2])           # Q6: size and mtime only
        recs = {r["key"]: r for r in records(self.fx)}
        m = recs["kakao:301:msg:12"]
        self.assertEqual(m["src_asof_s"], 1)
        self.assertNotIn("captured_at", m["native"])
        self.assertNotIn("captured_at", m["derived"])
        self.assertEqual(recs["kakao:301"]["native"], {"chatId": "301"})
        self.assertNotIn("updated_at", recs["kakao:301"]["derived"])
        self.assertEqual(recs["kakao:301"]["derived"]["count"], 2)

    def test_progress_fields_make_no_records(self):
        self.assertEqual(capture(self.fx, LIVE), 0)
        self.w.execute("UPDATE cursors SET updated_at = updated_at + 100")
        self.w.execute("UPDATE messages SET captured_at = captured_at + 100")
        self.w.commit()
        self.assertEqual(capture(self.fx, LIVE), 0)
        self.assertEqual(manifests(self.fx)[1]["segments"], [])
        self.assertEqual(replay_diff(self.fx), 0)

    def test_live_with_static_and_producer_change(self):
        self.assertEqual(capture(self.fx), 0)
        self.assertEqual(capture(self.fx, LIVE), 0)
        self.assertEqual(invariants(self.fx)[0], {})
        live = {r["key"]: r for r in records(self.fx) if r["origin"] == "live_out"}
        self.assertEqual(sorted(k for k, r in live.items() if r["kind"] == "room"), ["kakao:101", "kakao:301"])
        self.assertEqual(sum(1 for r in live.values() if r["kind"] == "msg"), 5)
        # a producer inconsistency (cursor count) and an edited message: one put each, nothing else
        self.w.execute("UPDATE cursors SET count = 5 WHERE chatId = '301'")
        self.w.execute("UPDATE messages SET message = 'other' WHERE chatId = '101' AND logId = '2'")
        self.w.commit()
        self.assertEqual(capture(self.fx, LIVE), 0)
        new = [(r["key"], r["rev"]) for r in records(self.fx) if r["seq"] == 3]
        self.assertEqual(new, [("kakao:101:msg:2", 2), ("kakao:301", 2)])
        self.assertEqual(invariants(self.fx)[0], {})
        self.assertEqual(replay_diff(self.fx), 0)


if __name__ == "__main__":
    unittest.main()
