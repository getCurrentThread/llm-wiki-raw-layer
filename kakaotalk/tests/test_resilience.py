# -*- coding: utf-8 -*-
"""Second review round after adoption (2026-10-04), the apps' part: a keystore of the wrong shape, a tableless
producer database, and the edb guard at genesis. The core part (a manifest that lost a field, state damage, the
work-tree check and provenance under SYSTEM, junctions, interrupted recovery, the precheck exit codes) is tested on
the toy app in tests/core."""
import shutil
import unittest

from kakaotalk.tests import edbfix as E
from kakaotalk.tests.test_hardening import Base
from kakaotalk.tests.helpers import capture, manifests
from kakaotalk.tests.test_m4 import EDB, EdbBase, rows


class TestDefectiveInputs(Base):
    def test_keystore_of_the_wrong_shape(self):
        """A keystore that is JSON but not an object: capture marks edb error (exit 75)."""
        self.assertEqual(capture(self.fx), 0)
        self.fx.keystore.write_text("[]", encoding="utf-8")
        self.assertEqual(capture(self.fx, ["edb"]), 75)
        self.assertEqual(manifests(self.fx)[-1]["origins"]["edb"]["status"], "error")
        self.assertEqual(manifests(self.fx)[-1]["segments"], [])


class TestProducerShape(Base):
    def test_tableless_producer_db_is_skipped_not_a_crash(self):
        """config-git#0: merged.sqlite without its rooms table: that container is error (note missing_table),
        the other origins commit, and the run says 'skipped' (75)."""
        import sqlite3  # noqa: PLC0415
        con = sqlite3.connect(self.fx.merged)
        con.execute("DROP TABLE rooms")
        con.commit()
        con.close()
        self.assertEqual(capture(self.fx), 75)
        m = manifests(self.fx)[-1]
        self.assertEqual(m["origins"]["merged_out"]["notes"].get("missing_table"), 1)
        self.assertTrue(any(s["origin"] == "backup_decrypted" for s in m["segments"]))


class TestGuardAtGenesis(EdbBase):
    def test_stale_keystore_is_suspect(self):
        """readers#0 (a): an edb file whose key the keystore lacks never reads clean: suspect from genesis."""
        E.place_room(self.fx, 51, rows(51, 1, 3), E.key_for(51))
        self.keystore()
        self.assertEqual(capture(self.fx, EDB), 3)
        self.assertEqual((self.edb()["status"], self.cont(51)["status"]), ("suspect", "no_key"))

    def test_no_room_file_is_ok(self):
        self.keystore()
        self.assertEqual(capture(self.fx, EDB), 0)
        self.assertEqual(self.edb()["status"], "ok")

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
