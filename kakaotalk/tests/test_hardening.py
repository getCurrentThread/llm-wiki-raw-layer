# -*- coding: utf-8 -*-
"""Adoption hardening on the kakaotalk app's synthetic inputs: exit codes for unreadable containers, the edb partial
run and sidecar, room_gone for static rooms, and defective producer inputs (a keystore of the wrong
shape, a tableless producer database, a missing chat_data). Recovery, ledger defects, config, provenance, srcmap shape,
the lit() contract and real process kills are core rules, tested on the toy app in tests/core."""
import shutil
import unittest
from pathlib import Path
from unittest import mock

from kakaotalk.readers import edb as src_kakao_edb
from rawlayer.capture import canon
from kakaotalk.tests import edbfix as E
from kakaotalk.tests.fixtures import Fixture
from kakaotalk.tests.helpers import _force, capture, manifests, records
from kakaotalk.tests.test_edb import EDB, EdbBase, rows


class Base(unittest.TestCase):
    def setUp(self):
        self.fx = Fixture().standard()

    def tearDown(self):
        shutil.rmtree(self.fx.base, onerror=_force)


class TestExit(Base):
    def test_unreadable_room_file_is_skipped_not_a_crash(self):
        """A room file that is not a database: that container is error, the other rooms commit, and the run says
        'skipped' (75, SPEC 13)."""
        (self.fx.backup / "101.sqlite").write_bytes(b"not a database" * 100)
        self.assertEqual(capture(self.fx), 75)
        m = manifests(self.fx)[-1]
        o = m["origins"]["backup_decrypted"]
        self.assertEqual(o["containers"][canon.source_id("101.sqlite")]["status"], "error")
        self.assertEqual(o["notes"].get("read_error"), 1)
        self.assertTrue(any(s["origin"] == "backup_decrypted" for s in m["segments"]))

    def test_static_room_gone(self):
        """AB-6: a backup_decrypted room file that disappears -> note room_gone, no records."""
        self.assertEqual(capture(self.fx), 0)
        (self.fx.backup / "102.sqlite").unlink()
        self.assertEqual(capture(self.fx), 0)
        o = manifests(self.fx)[-1]["origins"]["backup_decrypted"]
        self.assertEqual((o["status"], o["notes"].get("room_gone"), o["counts"]), ("ok", 1, {"put": 0, "absent": 0}))
        self.assertEqual(manifests(self.fx)[-1]["segments"], [])

    def test_bad_manifest_and_user_db_degrade_quietly(self):
        """An unreadable _manifest.json and a corrupt TalkUserDB.sqlite cost their labels only: rooms still
        capture, untitled and unnamed, exit 0."""
        (self.fx.backup / "_manifest.json").write_text("{not json", encoding="utf-8")
        (self.fx.backup / "TalkUserDB.sqlite").write_bytes(b"junk" * 64)
        self.assertEqual(capture(self.fx), 0)
        recs = [r for r in records(self.fx) if r["key"] == "kakao:101"]
        self.assertEqual(recs[0]["derived"], {})            # no manifest entry, no title

    def test_keystore_of_the_wrong_shape(self):
        """A keystore that is JSON but not an object: capture marks edb error (exit 75)."""
        self.assertEqual(capture(self.fx), 0)
        self.fx.keystore.write_text("[]", encoding="utf-8")
        self.assertEqual(capture(self.fx, ["edb"]), 75)
        self.assertEqual(manifests(self.fx)[-1]["origins"]["edb"]["status"], "error")
        self.assertEqual(manifests(self.fx)[-1]["segments"], [])


class TestPerOrigin(EdbBase):
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

    def test_one_bad_keystore_entry_is_one_room(self):
        """guard-tests#1: a non-string key_hex makes only that room no_key; the rest of edb is read."""
        self.room(61, rows(61, 1, 3))
        E.place_room(self.fx, 62, rows(62, 1, 2), E.key_for(62))
        self.extra["chat_data\\chatLogs_62.edb"] = {"chatId": 62, "key_hex": 5, "header_ok": True}
        self.keystore()
        self.assertEqual(capture(self.fx, EDB), 0)
        self.assertEqual((self.edb()["status"], self.cont(61)["status"], self.cont(62)["status"]),
                         ("ok", "clean", "no_key"))

    def test_missing_chat_data_is_an_origin_error(self):
        self.keystore()
        shutil.rmtree(self.cd)
        self.assertEqual(capture(self.fx, EDB), 75)
        self.assertEqual(self.edb()["status"], "error")


if __name__ == "__main__":
    unittest.main()
