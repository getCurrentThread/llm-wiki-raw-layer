# -*- coding: utf-8 -*-
"""M4 (edb) on synthetic SQLCipher-style files: room statuses, WAL handling (torn frame, previous-
generation salts, big-endian checksums, bad header -> partial, zero-filled pages), wrong key,
rekeyed, checkpoint race (unstable), two-phase absent, shrink guard, room_gone, source unchanged, a mixed
edb / backup / live layout, and the kakatok oracle differential (tests only)."""
import hashlib
import importlib
import os
import shutil
import sys
import unittest
from pathlib import Path
from unittest import mock

from rawlayer import conf
from rawlayer.capture import canon, fsio
from kakaotalk.readers import edb as src_kakao_edb
from rawlayer.capture.config import Config
from kakaotalk.tests import edbfix as E
from kakaotalk.tests.fixtures import Fixture
from kakaotalk.tests.helpers import _force, capture, invariants, manifests, records, replay_diff
from kakaotalk.tests.helpers import by_key

EDB = ["edb"]


def file_snaps(d):
    out = {}
    for p in sorted(Path(d).iterdir(), key=lambda q: q.name):
        st = p.stat()
        out[p.name] = (st.st_size, st.st_mtime_ns, hashlib.sha256(p.read_bytes()).hexdigest())
    return out


def sid(chat):
    return canon.source_id(f"chat_data/chatLogs_{chat}.edb")


def rows(chat, a, b, **kw):
    return [E.msg(chat, i, **kw) for i in range(a, b + 1)]


class EdbBase(unittest.TestCase):
    def setUp(self):
        self.fx = Fixture()
        self.cd = self.fx.user_dir / "chat_data"
        self.plain = self.fx.base / "plain"
        self.plain.mkdir(parents=True, exist_ok=True)
        self.keys = {}
        self.extra = {}

    def tearDown(self):
        shutil.rmtree(self.fx.base, onerror=_force)

    def keystore(self):
        E.write_keystore(self.fx.keystore, self.keys, self.extra)

    def room(self, chat, rs, wal_rows=None, key=None, salt=None, hist=()):
        key = key or E.key_for(chat)
        self.keys.setdefault(chat, key)
        E.place_room(self.fx, chat, rs, key, salt=salt, hist=hist, wal_rows=wal_rows)
        self.keystore()

    def plain_room(self, chat, rs, *txs):
        """(main, plain wal) of a room whose later transactions stay in the WAL."""
        pr = E.PlainRoom(self.plain / f"{chat}.sqlite", rs)
        for t in txs:
            pr.add(t)
        main, wal = pr.snapshot()
        pr.close()
        return main, wal

    def edb(self, i=-1):
        return manifests(self.fx)[i]["origins"]["edb"]

    def cont(self, chat, i=-1):
        return self.edb(i)["containers"][sid(chat)]

    def msg_keys(self, chat):
        pre = f"kakao:{chat}:msg:"
        return sorted(int(r["key"][len(pre):]) for r in records(self.fx) if r["key"].startswith(pre) and r["op"] == "put")


class TestEdbRead(EdbBase):
    def test_genesis_statuses_rerun_and_source_unchanged(self):
        self.room(11, rows(11, 1, 5), hist=[(2, 1, "old")])
        self.room(12, rows(12, 1, 3), wal_rows=rows(12, 4, 5))
        E.place_room(self.fx, 13, rows(13, 1, 1), E.key_for(13))               # key exists but header_ok false
        self.extra["chat_data\\chatLogs_13.edb"] = {"chatId": 13, "key_hex": E.key_for(13).hex(), "header_ok": False}
        E.place_room(self.fx, 14, rows(14, 1, 1), E.key_for(14))               # no keystore entry at all
        self.keystore()
        (self.cd / "chatLogs_x.edb").write_bytes(b"")
        before = file_snaps(self.cd)
        self.assertEqual(capture(self.fx, EDB), 0)
        self.assertEqual(file_snaps(self.cd), before)
        o = self.edb()
        self.assertEqual(o["status"], "ok")
        self.assertEqual(o["notes"].get("not_room"), 1)
        self.assertEqual((self.cont(11)["status"], self.cont(11)["wal"]["state"]), ("clean", "none"))
        self.assertEqual(self.cont(12)["wal"], {"state": "ok", "frames_total": self.cont(12)["wal"]["frames_total"],
                                                "frames_valid": self.cont(12)["wal"]["frames_total"],
                                                "frames_applied": self.cont(12)["wal"]["frames_total"],
                                                "commits": 1, "zero_filled": 0})
        self.assertEqual(self.cont(13)["status"], "no_key")
        self.assertEqual(self.cont(14)["status"], "no_key")
        self.assertEqual(self.cont(11)["tables"]["chatLogDrafts"], 1)
        recs = by_key(records(self.fx))
        salt = hashlib.sha256(b"salt-11").digest()[:16]
        self.assertEqual(recs["kakao:11"][0]["native"], {"chatId": "11", "salt_fp": hashlib.sha256(salt).hexdigest()[:16]})
        self.assertIn("kakao:11:hist:2:1", recs)
        self.assertIn("kakao:11@chatLogs", recs)
        self.assertFalse(any(k.startswith("kakao:11@chatLogDrafts") for k in recs))
        self.assertEqual(self.msg_keys(12), [1, 2, 3, 4, 5])
        self.assertFalse(any(k.startswith(("kakao:13", "kakao:14")) for k in recs))
        m = recs["kakao:11:msg:3"][0]
        self.assertEqual(m["ts_ms"], (E.T0 + 180) * 1000)
        # V3: nothing changed -> nothing written
        self.assertEqual(capture(self.fx, EDB), 0)
        self.assertEqual(manifests(self.fx)[-1]["segments"], [])
        self.assertEqual(replay_diff(self.fx), 0)
        # os.walk, not Path.walk: Path.walk enters no junction (as tests/core/ledgercheck.py)
        left = [n for _dp, _d, fs in os.walk(self.fx.data_root) for n in fs if n.endswith(".sqlite") and n != "state.sqlite"]
        self.assertEqual(left, [])

    def test_wal_torn_frame_old_generation_and_big_endian(self):
        main, wal = self.plain_room(21, rows(21, 1, 3), rows(21, 4, 4), rows(21, 5, 5, text="y" * 3000))
        hdr, frames = E.wal_frames(wal)
        commits = [i for i, f in enumerate(frames) if f[1]]
        self.assertEqual(len(commits), 2)
        n, first = len(frames), commits[0] + 1
        for chat in (21, 22, 23, 24):
            key, salt = E.key_for(chat), bytes([chat]) * 16
            self.keys[chat] = key
            enc = [(p, c, E.enc_page(key, salt, p, pg)) for p, c, pg in frames]
            if chat == 21:      # last frame's page damaged -> checksum chain breaks there
                w = bytearray(E.build_wal(enc, magic=hdr[0], salts=(hdr[4], hdr[5])))
                w[-100] ^= 0xFF
                w = bytes(w)
            elif chat == 22:    # file cut inside the last frame
                w = E.build_wal(enc, magic=hdr[0], salts=(hdr[4], hdr[5]))[:-1000]
            elif chat == 23:    # frames after the first commit belong to a previous WAL generation
                fs = [None] * first + [(7, 8)] * (n - first)
                w = E.build_wal(enc, magic=hdr[0], salts=(hdr[4], hdr[5]), frame_salts=fs)
            else:               # big-endian checksums, everything valid
                w = E.build_wal(enc, magic=0x377F0683, salts=(hdr[4], hdr[5]))
            E.write_edb(self.cd, chat, E.enc_main(key, salt, main), w)
        self.keystore()
        self.assertEqual(capture(self.fx, EDB), 0)
        for chat, valid, total in ((21, n - 1, n), (22, n - 1, n - 1), (23, first, n)):
            with self.subTest(chat=chat):
                c = self.cont(chat)
                self.assertEqual(c["status"], "clean")
                self.assertEqual(c["wal"], {"state": "ok", "frames_total": total, "frames_valid": valid,
                                            "frames_applied": first, "commits": 1, "zero_filled": 0})
                self.assertEqual(self.msg_keys(chat), [1, 2, 3, 4])
        self.assertEqual(self.cont(24)["wal"]["commits"], 2)
        self.assertEqual(self.msg_keys(24), [1, 2, 3, 4, 5])

    def test_wrong_key_zero_filled_and_short_main_are_unreadable(self):
        self.room(31, rows(31, 1, 3), key=E.key_for(31))
        self.keys[31] = E.key_for(99)
        # 32: a commit frame claims two more pages than main has but carries only the last one
        main, _w = self.plain_room(32, rows(32, 1, 3))
        key, salt = E.key_for(32), b"\x02" * 16
        self.keys[32] = key
        npg = len(main) // E.PAGE
        page = E.enc_page(key, salt, npg + 2, main[E.PAGE:2 * E.PAGE])
        E.write_edb(self.cd, 32, E.enc_main(key, salt, main), E.build_wal([(npg + 2, npg + 2, page)]))
        # 33: main is not whole pages
        main, _w = self.plain_room(33, rows(33, 1, 3))
        self.keys[33] = E.key_for(33)
        E.write_edb(self.cd, 33, E.enc_main(E.key_for(33), b"\x03" * 16, main) + b"x")
        self.keystore()
        # no room reads clean: N_obs == 0 trips the guard even at genesis (Q12), so the origin is suspect
        self.assertEqual(capture(self.fx, EDB), 3)
        self.assertEqual(self.edb()["status"], "suspect")
        self.assertEqual(self.cont(31)["status"], "unreadable")
        self.assertEqual((self.cont(32)["status"], self.cont(32)["wal"]["zero_filled"]), ("unreadable", 1))
        self.assertEqual(self.cont(33)["status"], "unreadable")
        notes = self.edb()["notes"]
        self.assertEqual((notes.get("gate_header"), notes.get("zero_filled"), notes.get("main_not_page_multiple")),
                         (1, 1, 1))
        self.assertEqual([r for r in records(self.fx) if r["key"].startswith(("kakao:31", "kakao:32", "kakao:33"))], [])

    def test_bad_wal_header_is_partial_and_adds_no_pending(self):
        self.room(49, rows(49, 1, 3))                        # a clean room: N_obs > 0, the guard stays quiet
        main, wal = self.plain_room(41, rows(41, 1, 40), rows(41, 41, 42))
        key, salt = E.key_for(41), b"\x04" * 16
        self.keys[41] = key
        good = E.enc_wal(key, salt, wal)
        E.write_edb(self.cd, 41, E.enc_main(key, salt, main), good)
        self.keystore()
        self.assertEqual(capture(self.fx, EDB), 0)
        self.assertEqual(len(self.msg_keys(41)), 42)
        bad = bytearray(good)
        bad[28] ^= 0x01                                      # header checksum
        E.write_edb(self.cd, 41, E.enc_main(key, salt, main), bytes(bad))
        self.assertEqual(capture(self.fx, EDB), 0)
        c = self.cont(41)
        self.assertEqual((c["status"], c["wal"]["state"], c["wal"]["frames_applied"]), ("partial", "bad_header", 0))
        self.assertEqual(self.edb()["pending"], {"add": [], "drop": []})
        self.assertEqual(manifests(self.fx)[-1]["segments"], [])
        bad, nums = invariants(self.fx)
        self.assertEqual((bad, nums["containers_skipped"]), ({}, 1))      # the partial room is not compared


class TestEdbPolicy(EdbBase):
    def test_absent_is_two_phase_and_pending_clears(self):
        self.room(51, rows(51, 1, 40))
        self.room(52, rows(52, 1, 40))
        self.assertEqual(capture(self.fx, EDB), 0)
        self.room(51, rows(51, 1, 39))
        self.room(52, rows(52, 1, 39))
        self.assertEqual(capture(self.fx, EDB), 0)
        self.assertEqual(self.edb()["pending"]["add"], ["kakao:51:msg:40", "kakao:52:msg:40"])
        self.assertEqual(manifests(self.fx)[-1]["segments"], [])
        self.room(52, rows(52, 1, 40))                        # back with the same content
        asof = 1_790_000_123
        os.utime(self.cd / "chatLogs_51.edb", (asof, asof))            # pathlib has no utime
        self.assertEqual(capture(self.fx, EDB), 0)
        recs = by_key(records(self.fx))
        self.assertEqual([(r["rev"], r["op"]) for r in recs["kakao:51:msg:40"]], [(1, "put"), (2, "absent")])
        ab = recs["kakao:51:msg:40"][1]
        self.assertEqual(ab["src"], sid(51))
        # Q9: ts_ms null; src_asof_s = as-of of the read that found the key missing (not the last put's)
        self.assertEqual((ab["ts_ms"], ab["src_asof_s"]), (None, asof))
        self.assertNotEqual(recs["kakao:51:msg:40"][0]["src_asof_s"], asof)
        self.assertEqual(len(recs["kakao:52:msg:40"]), 1)
        self.assertEqual(self.edb()["pending"], {"add": [], "drop": ["kakao:52:msg:40"]})
        self.assertEqual(capture(self.fx, EDB), 0)
        self.assertEqual(manifests(self.fx)[-1]["segments"], [])
        self.assertEqual(invariants(self.fx)[0], {})
        self.assertEqual(replay_diff(self.fx), 0)

    def test_shrink_guard_marks_suspect(self):
        self.room(61, rows(61, 1, 60))
        self.assertEqual(capture(self.fx, EDB), 0)
        self.room(61, rows(61, 1, 5))
        self.assertEqual(capture(self.fx, EDB), 3)
        o = self.edb()
        self.assertEqual((o["status"], o["notes"].get("shrink_guard"), o["pending"]["add"]), ("suspect", 1, []))
        self.assertEqual(o["counts"]["absent"], 0)
        self.room(61, rows(61, 1, 60))
        self.assertEqual(capture(self.fx, EDB), 0)

    def test_no_clean_room_after_puts_is_suspect(self):
        """SPEC 8.3 guard 'N_obs == 0': once the ledger holds edb puts, a run that reads no room clean (every
        file gone here) is suspect with no pending add and no absent; genesis with no edb file is not."""
        self.assertEqual(capture(self.fx, EDB), 0)           # genesis, no edb file: nothing to protect
        self.room(81, rows(81, 1, 4))
        self.assertEqual(capture(self.fx, EDB), 0)
        (self.cd / "chatLogs_81.edb").unlink()
        self.assertEqual(capture(self.fx, EDB), 3)
        o = self.edb()
        self.assertEqual((o["status"], o["notes"].get("shrink_guard"), o["notes"].get("room_gone")),
                         ("suspect", 1, 1))
        self.assertEqual((o["pending"]["add"], o["counts"]["absent"]), ([], 0))

    def test_rekeyed_then_new_key(self):
        self.room(79, rows(79, 1, 3))                        # a clean room: N_obs > 0, the guard stays quiet
        self.room(71, rows(71, 1, 3), salt=b"\x07" * 16)
        self.assertEqual(capture(self.fx, EDB), 0)
        E.place_room(self.fx, 71, rows(71, 1, 3), E.key_for(171), salt=b"\x08" * 16)   # keystore still has the old key
        self.assertEqual(capture(self.fx, EDB), 0)
        self.assertEqual(self.cont(71)["status"], "rekeyed")
        self.assertEqual(manifests(self.fx)[-1]["segments"], [])
        self.keys[71] = E.key_for(171)
        self.keystore()
        self.assertEqual(capture(self.fx, EDB), 0)
        self.assertEqual(self.cont(71)["status"], "clean")
        recs = [r for r in records(self.fx) if r["key"] == "kakao:71"]
        self.assertEqual([r["rev"] for r in recs], [1, 2])
        self.assertNotEqual(recs[0]["native"]["salt_fp"], recs[1]["native"]["salt_fp"])
        self.assertEqual(len([r for r in records(self.fx) if r["key"].startswith("kakao:71:msg:")]), 3)

    def test_checkpoint_race(self):
        self.room(81, rows(81, 1, 3), wal_rows=rows(81, 4, 4))
        self.room(82, rows(82, 1, 3), wal_rows=rows(82, 4, 4))
        orig = fsio.read_shared
        races = {"81": 99, "82": 1}

        def racing(path, limit=None, retries=6):
            data = orig(path, limit, retries)
            name = Path(path).name
            if limit is None and name.endswith(".edb"):
                chat = name[len("chatLogs_"):-len(".edb")]
                if races.get(chat, 0) > 0:
                    races[chat] -= 1
                    with E.sidecar(Path(path), "-wal").open("ab") as fh:      # the writer appends a frame meanwhile
                        fh.write(b"\x00" * (24 + E.PAGE))
            return data

        with mock.patch.object(fsio, "read_shared", racing), mock.patch.object(src_kakao_edb, "RETRY_SLEEP", 0):
            self.assertEqual(capture(self.fx, EDB), 0)
        self.assertEqual(self.cont(81)["status"], "unstable")
        self.assertEqual(self.cont(82)["status"], "clean")
        w = self.cont(82)["wal"]
        self.assertEqual((w["frames_total"] - w["frames_valid"], w["commits"]), (1, 1))
        self.assertEqual(self.msg_keys(81), [])
        self.assertEqual(self.msg_keys(82), [1, 2, 3, 4])

    def test_room_gone(self):
        self.room(93, rows(93, 1, 40))
        self.room(94, rows(94, 1, 40))
        self.assertEqual(capture(self.fx, EDB), 0)
        (self.cd / "chatLogs_94.edb").unlink()
        self.assertEqual(capture(self.fx, EDB), 0)
        o = self.edb()
        self.assertEqual((o["notes"].get("room_gone"), o["pending"]["add"], o["counts"]), (1, [], {"put": 0, "absent": 0}))


class TestEdbMixedLayout(EdbBase):
    """One hand-made layout across edb, backup_decrypted and live_out (deleted and 0x4000 rows, a revised row with
    history, a file without a key, a key without a file, a header_ok false entry), then a round in which an edb file
    disappears and live gains a chat without a cursor. Formerly the C1/C5/C6/C7 fixture; what is asserted here is
    capture's side: exit codes, room statuses, what each origin recorded, and the ledger invariants."""

    def live(self, rooms, cursors):
        """rooms: {chat: [(lid, type, deleted, message)]}, cursors: {chat: last_logId}."""
        from kakaotalk.tests.test_m3 import LIVE_DDL  # noqa: PLC0415
        import sqlite3  # noqa: PLC0415
        if self.fx.live.exists():
            self.fx.live.unlink()
        con = sqlite3.connect(self.fx.live)
        con.executescript(LIVE_DDL)
        for chat, rs in rooms.items():
            for lid, typ, deleted, text in rs:
                con.execute("INSERT INTO messages VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                            (str(chat), str(lid), E.T0 + lid * 60, "7", typ, deleted, text, "s", "t", "d", "live", 5))
        for chat, last in cursors.items():
            n = len(rooms.get(chat, []))
            con.execute("INSERT INTO cursors VALUES (?,?,?,?,?,?,?)", (str(chat), str(last), "r", n, 0, 0, 0))
        con.commit()
        con.close()

    def test_mixed_layout(self):
        m = E.msg
        self.room(301, [m(301, 1), m(301, 2, typ=1 | 0x4000, text="QUJD"), m(301, 3, text="edited", rev=1),
                        m(301, 4, text="same4", deleted=1), m(301, 5, text="x"), m(301, 6), m(301, 8, text="new8"),
                        m(301, 11, text="", deleted=1, prev=8)],
                  hist=[(8, 1, "old")])
        self.room(303, rows(303, 1, 3))
        self.room(305, rows(305, 1, 2))
        self.room(306, [m(306, 10, prev=5), m(306, 12, prev=11, pm=1, nm=1), m(306, 14, prev=13),
                        m(306, 16, prev=15), m(306, 17, prev=16)])
        E.place_room(self.fx, 309, rows(309, 1, 1), E.key_for(309))           # file without a key
        self.extra["chat_data\\chatLogs_307.edb"] = {"chatId": 307, "key_hex": "00" * 32, "header_ok": False}
        self.keys[308] = E.key_for(308)                                         # key without a file
        self.keystore()
        E.plain_backup_room(self.fx.backup / "301.sqlite",
                            [m(301, 1), m(301, 2, text="abc"), m(301, 3, text="orig"), m(301, 4, text="same4"),
                             m(301, 5, text="y"), m(301, 7), m(301, 8, text="old8")])
        E.plain_backup_room(self.fx.backup / "302.sqlite", rows(302, 1, 2))
        self.live({301: [(2, 1, 0, "abc"), (3, 1, 0, "orig"), (4, 1, 0, "same4"), (5, 1, 0, "y"), (8, 1, 0, "new8"),
                         (9, 1, 0, "n9"), (11, 1, 0, "hi")],
                   304: [(1, 1, 0, "a")], 305: [(1, 1, 0, "e305-1")]},
                  {301: 11, 304: 1, 305: 0})
        self.assertEqual(capture(self.fx, ["backup_decrypted", "live_out", "edb"]), 0)
        self.assertEqual(invariants(self.fx)[0], {})
        self.assertEqual({c: self.cont(c)["status"] for c in (301, 303, 305, 306, 309)},
                         {301: "clean", 303: "clean", 305: "clean", 306: "clean", 309: "no_key"})
        rooms = {o: sorted(r["key"] for r in records(self.fx) if r["origin"] == o and r["kind"] == "room")
                 for o in ("edb", "backup_decrypted", "live_out")}
        self.assertEqual(rooms, {"edb": ["kakao:301", "kakao:303", "kakao:305", "kakao:306"],
                                 "backup_decrypted": ["kakao:301", "kakao:302"],
                                 "live_out": ["kakao:301", "kakao:304", "kakao:305"]})
        msgs = {o: sorted(int(r["key"].split(":")[3]) for r in records(self.fx)
                          if r["origin"] == o and r["key"].startswith("kakao:301:msg:"))
                for o in ("edb", "backup_decrypted", "live_out")}
        self.assertEqual(msgs, {"edb": [1, 2, 3, 4, 5, 6, 8, 11], "backup_decrypted": [1, 2, 3, 4, 5, 7, 8],
                                "live_out": [2, 3, 4, 5, 8, 9, 11]})
        edb = {r["key"]: r for r in records(self.fx) if r["origin"] == "edb"}
        self.assertEqual(edb["kakao:301:msg:2"]["native"]["type"], 1 | 0x4000)
        self.assertIn("kakao:301:hist:8:1", edb)
        # next round: an edb file disappears, and live gains a chat with messages but no cursor
        (self.cd / "chatLogs_303.edb").unlink()
        self.live({301: [(2, 1, 0, "abc"), (3, 1, 0, "orig"), (4, 1, 0, "same4"), (5, 1, 0, "y"), (8, 1, 0, "new8"),
                         (9, 1, 0, "n9"), (11, 1, 0, "hi")],
                   304: [(1, 1, 0, "a")], 305: [(1, 1, 0, "e305-1")], 310: [(1, 1, 0, "b")]},
                  {301: 11, 304: 1, 305: 0})
        self.assertEqual(capture(self.fx, ["live_out", "edb"]), 0)
        o = self.edb()
        self.assertEqual((o["status"], o["notes"].get("room_gone"), o["pending"]["add"]), ("ok", 1, []))
        new = sorted(r["key"] for r in records(self.fx) if r["seq"] == 2)
        self.assertEqual(new, ["kakao:310:msg:1"])
        self.assertEqual(invariants(self.fx)[0], {})
        self.assertEqual(replay_diff(self.fx), 0)


def _oracle_dir():
    """The kakatok folder that holds edb_reader.py, as the text sys.path takes, or None. LLMWIKI_ORACLE_DIR is
    used as given; the default is Documents/kakatok in the home of the user this project belongs to
    (settings.HOME_DIR)."""
    d = os.environ.get("LLMWIKI_ORACLE_DIR") or str(conf.settings().HOME_DIR / "Documents" / "kakatok")
    return d if (Path(d) / "edb_reader.py").exists() else None


def _oracle():
    d = _oracle_dir()
    if d is None:
        raise unittest.SkipTest("kakatok oracle not available")
    sys.path.insert(0, d)
    dont = sys.dont_write_bytecode
    sys.dont_write_bytecode = True          # never leave a __pycache__ in the kakatok checkout
    try:
        return importlib.import_module("edb_reader")
    finally:
        sys.dont_write_bytecode = dont
        sys.path.remove(d)


def usable(img):
    return [img[i:i + E.CT_END] for i in range(0, len(img), E.PAGE)]


class TestOracle(EdbBase):
    def test_synthetic_matches_oracle(self):
        er = _oracle()
        self.room(11, rows(11, 1, 300))
        self.room(12, rows(12, 1, 100), wal_rows=rows(12, 101, 400, text="z" * 200))
        for chat in (11, 12):
            p = self.cd / f"chatLogs_{chat}.edb"
            main, wal = src_kakao_edb.stable_read(p, E.sidecar(p, "-wal"))[:2]
            mine, _f = src_kakao_edb.build_image(E.key_for(chat), main, wal)
            theirs = er.decrypt_to_bytes(str(p), E.key_for(chat))          # kakatok's reader takes a str
            self.assertEqual(usable(mine), usable(theirs), chat)


@unittest.skipUnless(os.environ.get("LLMWIKI_REAL_EDB") == "1", "real-data oracle differential (set LLMWIKI_REAL_EDB=1)")
class TestOracleReal(unittest.TestCase):
    """Aggregates only. LLMWIKI_CONFIG = config.json, LLMWIKI_INPUT_SET = T1|T2|live."""

    def test_real_matches_oracle(self):
        from rawlayer.capture.config import InputSet  # noqa: PLC0415
        er = _oracle()
        cfg = Config(os.environ["LLMWIKI_CONFIG"])
        inputs = InputSet(cfg, os.environ.get("LLMWIKI_INPUT_SET", "T1"), verify_static=False)
        keys, _all, _ok = src_kakao_edb.load_keystore(inputs.keystore)
        n = {"rooms": 0, "with_wal": 0, "equal": 0, "differ_main_only": 0, "differ_wal": 0, "mine_unreadable": 0}
        for p in sorted(inputs.edb_dir.iterdir(), key=lambda q: q.name):
            m = src_kakao_edb._FILE.match(p.name)
            if not m or m.group(1) not in keys:
                continue
            key = keys[m.group(1)]
            n["rooms"] += 1
            main, wal, _st = src_kakao_edb.stable_read(p, E.sidecar(p, "-wal"))
            if wal:
                n["with_wal"] += 1
            try:
                mine, _f = src_kakao_edb.build_image(key, main, wal)
            except src_kakao_edb.ContainerError:
                mine = None
            if mine is None:
                n["mine_unreadable"] += 1
                continue
            if usable(mine) == usable(er.decrypt_to_bytes(str(p), key)):
                n["equal"] += 1
            else:
                n["differ_wal" if wal else "differ_main_only"] += 1
        print("oracle differential", " ".join(f"{k}={v}" for k, v in n.items()))
        self.assertEqual(n["differ_main_only"], 0)


if __name__ == "__main__":
    unittest.main()
