# -*- coding: utf-8 -*-
"""SPEC-CLARIFICATIONS Q10 (edb pending/absent over every kind) on synthetic rooms: a capture table that
disappears (empty and non-empty), a no_pk transition, re-observation under the shrink guard (drop) and in a
partial room (no drop), same-h regeneration (flap) and key_unprintable. The pending fold of the ledger invariants
(tests/core/ledgercheck.py, formerly C10) is tested on its own in tests/core/test_ledgercheck.py."""
import unittest

from kakaotalk.tests import edbfix as E
from kakaotalk.tests.helpers import capture, invariants, manifests, records, replay_diff
from kakaotalk.tests.helpers import by_key
from kakaotalk.tests.test_m4 import EDB, EdbBase, rows

HIST_NO_PK = "CREATE TABLE chatLogHistory(logId INTEGER, revision INTEGER, message TEXT)"
HIST_TEXT_PK = "CREATE TABLE chatLogHistory(logId TEXT, revision TEXT, message TEXT, PRIMARY KEY(logId, revision))"


class TestQ10(EdbBase):
    def room_sql(self, chat, rs, *stmts, hist=()):
        """A clean room (no WAL) built from EDB_DDL + rows/hist, then the given SQL statements."""
        key = self.keys.setdefault(chat, E.key_for(chat))
        pr = E.PlainRoom(self.plain / f"{chat}.sqlite", rs, hist)
        for q in stmts:
            pr.con.execute(q)
        pr.checkpoint()
        main, _wal = pr.snapshot()
        pr.close()
        for suf in ("", "-wal", "-shm"):
            p = E.sidecar(pr.path, suf)
            if p.exists():
                p.unlink()
        E.write_edb(self.cd, chat, E.enc_main(key, b"\x07" * 16, main))
        self.keystore()

    def ops(self, key):
        return [r["op"] for r in by_key(records(self.fx)).get(key, [])]

    def test_empty_table_drop_schema_absent_then_same_h_is_flap(self):
        self.room_sql(71, rows(71, 1, 40))
        self.room_sql(72, rows(72, 1, 40))
        self.assertEqual(capture(self.fx, EDB), 0)
        sk = "kakao:71@chatLogHistory"
        self.assertEqual(self.ops(sk), ["put"])
        self.room_sql(71, rows(71, 1, 40), "DROP TABLE chatLogHistory")
        self.assertEqual(capture(self.fx, EDB), 0)                  # schema key printable in pending.add
        self.assertEqual(self.edb()["pending"], {"add": [sk], "drop": []})
        self.assertEqual(capture(self.fx, EDB), 0)
        recs = by_key(records(self.fx))[sk]
        self.assertEqual([(r["rev"], r["op"]) for r in recs], [(1, "put"), (2, "absent")])
        self.assertEqual((recs[1]["ts_ms"], type(recs[1]["src_asof_s"])), (None, int))
        self.assertEqual(self.edb()["pending"], {"add": [], "drop": []})
        self.assertEqual(invariants(self.fx)[0], {})
        self.assertEqual(replay_diff(self.fx), 0)
        self.room_sql(71, rows(71, 1, 40))                          # the same table again: same h as rev 1
        self.assertEqual(capture(self.fx, EDB), 0)
        self.assertEqual(self.ops(sk), ["put", "absent", "put"])
        self.assertEqual(invariants(self.fx)[0], {"flap": 1})       # the ledger policy sees the regeneration

    def test_nonempty_table_drop_and_no_pk(self):
        self.room_sql(73, rows(73, 1, 40), hist=[(5, 1, "h"), (6, 1, "h")])
        self.room_sql(74, rows(74, 1, 40), hist=[(7, 1, "h")])
        self.assertEqual(capture(self.fx, EDB), 0)
        # 73: chatLogHistory loses its PK -> schema put (rev 2), hist rows unobserved; 74: table dropped
        self.room_sql(73, rows(73, 1, 40), "DROP TABLE chatLogHistory", HIST_NO_PK,
                      "INSERT INTO chatLogHistory VALUES (5, 1, 'h')")
        self.room_sql(74, rows(74, 1, 40), "DROP TABLE chatLogHistory")
        self.assertEqual(capture(self.fx, EDB), 0)
        o = self.edb()
        self.assertEqual(o["pending"]["add"], ["kakao:73:hist:5:1", "kakao:73:hist:6:1",
                                               "kakao:74:hist:7:1", "kakao:74@chatLogHistory"])
        self.assertEqual(o["notes"].get("no_pk"), 1)
        self.assertEqual(self.ops("kakao:73@chatLogHistory"), ["put", "put"])
        self.assertEqual(capture(self.fx, EDB), 0)
        for k in ("kakao:73:hist:5:1", "kakao:73:hist:6:1", "kakao:74:hist:7:1", "kakao:74@chatLogHistory"):
            self.assertEqual(self.ops(k), ["put", "absent"], k)
        self.assertEqual(invariants(self.fx)[0], {})
        self.assertEqual(replay_diff(self.fx), 0)

    def test_guard_run_still_drops(self):
        self.room_sql(75, rows(75, 1, 60))
        self.room_sql(76, rows(76, 1, 60))
        self.assertEqual(capture(self.fx, EDB), 0)
        self.room_sql(75, rows(75, 1, 59))
        self.assertEqual(capture(self.fx, EDB), 0)
        self.assertEqual(self.edb()["pending"]["add"], ["kakao:75:msg:60"])
        self.room_sql(75, rows(75, 1, 60))                          # back ...
        self.room_sql(76, rows(76, 1, 2))                           # ... while 76 trips the guard
        self.assertEqual(capture(self.fx, EDB), 3)
        o = self.edb()
        self.assertEqual((o["status"], o["notes"].get("shrink_guard"), o["counts"]["absent"]), ("suspect", 1, 0))
        self.assertEqual(o["pending"], {"add": [], "drop": ["kakao:75:msg:60"]})
        bad, _nums = invariants(self.fx)
        self.assertEqual(bad, {})
        self.assertEqual(replay_diff(self.fx), 0)

    def test_partial_room_keeps_pending(self):
        main40, wal = self.plain_room(77, rows(77, 1, 40), rows(77, 41, 42))
        key, salt = E.key_for(77), b"\x05" * 16
        self.keys[77] = key
        good = E.enc_wal(key, salt, wal)
        E.write_edb(self.cd, 77, E.enc_main(key, salt, main40), good)
        self.keystore()
        self.room_sql(78, rows(78, 1, 40))
        self.assertEqual(capture(self.fx, EDB), 0)
        self.assertEqual(len(self.msg_keys(77)), 42)
        main39, _ = self.plain_room(77, rows(77, 1, 39))
        E.write_edb(self.cd, 77, E.enc_main(key, salt, main39))
        self.assertEqual(capture(self.fx, EDB), 0)
        miss = ["kakao:77:msg:40", "kakao:77:msg:41", "kakao:77:msg:42"]
        self.assertEqual(self.edb()["pending"]["add"], miss)
        bad = bytearray(good)
        bad[28] ^= 0x01                                             # WAL header checksum -> partial
        E.write_edb(self.cd, 77, E.enc_main(key, salt, main40), bytes(bad))
        self.assertEqual(capture(self.fx, EDB), 0)
        o = self.edb()
        self.assertEqual((o["containers"][self.sid77()]["status"], o["pending"]), ("partial", {"add": [], "drop": []}))
        self.assertEqual(o["counts"]["absent"], 0)
        main42, _ = self.plain_room(77, rows(77, 1, 42))
        E.write_edb(self.cd, 77, E.enc_main(key, salt, main42))
        self.assertEqual(capture(self.fx, EDB), 0)
        self.assertEqual(self.edb()["pending"], {"add": [], "drop": miss})
        self.assertEqual(invariants(self.fx)[0], {})
        self.assertEqual(replay_diff(self.fx), 0)

    def sid77(self):
        from kakaotalk.tests.test_m4 import sid
        return sid(77)

    def test_key_unprintable(self):
        self.room_sql(79, rows(79, 1, 40), "DROP TABLE chatLogHistory", HIST_TEXT_PK,
                      "INSERT INTO chatLogHistory VALUES ('a1', 'r1', 'h')")
        self.assertEqual(capture(self.fx, EDB), 0)
        self.assertEqual(self.ops("kakao:79:hist:a1:r1"), ["put"])
        self.room_sql(79, rows(79, 1, 40), "DROP TABLE chatLogHistory")
        self.assertEqual(capture(self.fx, EDB), 3)                  # no exception: manifest written, suspect
        o = self.edb()
        self.assertEqual((o["status"], o["notes"].get("key_unprintable"), o["pending"]["add"], o["counts"]["absent"]),
                         ("suspect", 1, [], 0))
        self.assertEqual(len(manifests(self.fx)), 2)


if __name__ == "__main__":
    unittest.main()
