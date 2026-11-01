# -*- coding: utf-8 -*-
"""setup's kakaotalk side (kakaotalk/provision.py): placeholder defaults fill only placeholders, the setup command end
to end on a throwaway data parent (the example config's placeholders resolve, a verified root copy is adopted, the
keystore is written and re-derives), and the ActionLogDB userId lookup decrypting in memory. Nothing here touches the
real client or the real process: every input is synthetic under a temp directory, and the client lookups are patched
out."""
import json
import shutil
import unittest
from pathlib import Path
from unittest import mock

from kakaotalk import wincrypt as kd
from kakaotalk import keychain, keystore, provision
from kakaotalk.tests import edbfix as E
from kakaotalk.tests.fixtures import Fixture
from kakaotalk.tests.test_selfderive import ROOT, quiet, wrap_dek, write_appstate

REPO = Path(__file__).resolve().parents[2]


class Notes(list):
    """The setup line collector the hooks print through."""

    def __call__(self, msg):
        self.append(msg)


def fake_home(base: Path, with_research=True) -> Path:
    """A home holding a root copy where the adoption step looks for one."""
    home = base / "home"
    if with_research:
        p = home / "Documents" / "kakatok" / "root_v2.hex"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(ROOT.hex())
    return home


class TestDefaults(unittest.TestCase):
    def setUp(self):
        self.fx = Fixture()

    def tearDown(self):
        shutil.rmtree(self.fx.base, onerror=lambda *a: None)

    def test_fill_defaults_fills_placeholders_only(self):
        cfg = {"data_root": str(self.fx.data_root), "sources": {
            "backup_decrypted": "<abs dir>\\backup_decrypted",   # the placeholder is replaced
            "keystore": "C:\\elsewhere\\keystore.json",          # the user's own value stays
        }}
        notes = Notes()
        filled = provision.fill_defaults(cfg, home=fake_home(self.fx.base),
                                         users=str(self.fx.base / "no-users"), note=notes)
        self.assertEqual(sorted(filled), ["backup_decrypted"])
        self.assertEqual(cfg["sources"]["keystore"], "C:\\elsewhere\\keystore.json")
        self.assertEqual(cfg["sources"]["backup_decrypted"],
                         str(self.fx.data_root / "backup_decrypted"))
        self.assertEqual(notes, ["default sources set: backup_decrypted"])


class TestSetupCommand(unittest.TestCase):
    """manage.py setup kakaotalk end to end on a throwaway data parent: the example's placeholders all resolve,
    the root is adopted and the keystore written, and the config the file holds is the one capture loads."""

    def setUp(self):
        self.fx = Fixture()
        for chat in (31, 32):
            E.place_room(self.fx, chat, [E.msg(chat, i) for i in (1, 2, 3)], E.key_for(chat))
        wrapped = {f"chat_data\\chatLogs_{c}.edb": wrap_dek(f"chat_data\\chatLogs_{c}.edb", E.key_for(c))
                   for c in (31, 32)}
        write_appstate(self.fx.user_dir, wrapped)
        self.home = fake_home(self.fx.base)
        self.parent = self.fx.base / "parent"                     # the throwaway data parent
        from rawlayer import registry
        registry.select("kakaotalk")

    def tearDown(self):
        shutil.rmtree(self.fx.base, onerror=lambda *a: None)

    def test_setup_fills_defaults_and_provisions_the_root(self):
        example = json.loads((REPO / "kakaotalk" / "config.example.json").read_text(encoding="utf-8"))
        base = self.fx.base / "base-config.json"
        base.write_text(json.dumps(example), encoding="utf-8")
        from rawlayer.capture import paths
        with mock.patch.object(paths, "DATA_PARENT", self.parent), \
                mock.patch.object(provision, "_home", lambda: self.home), \
                mock.patch.object(kd, "_find_pid", lambda name="KakaoTalk.exe": None), \
                mock.patch("rawlayer.management.commands.setup.search_problem", lambda folder: None), \
                mock.patch.dict("os.environ", {"KATOK_USERS_DIR": str(self.fx.inputs)}), \
                quiet():
            from rawlayer.management.commands.setup import Command
            import contextlib, io
            with contextlib.redirect_stdout(io.StringIO()):
                rc = Command("setup").run("kakaotalk", ["--from", str(base)])
        self.assertEqual(rc, 0)
        cfg = json.loads((self.parent / "kakaotalk" / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(cfg["sources"]["kakao_user_dir"], str(self.fx.user_dir))
        self.assertEqual(cfg["sources"]["backup_decrypted"],
                         str(self.parent / "kakaotalk" / "backup_decrypted"))
        self.assertEqual(cfg["sources"]["keystore"], str(self.parent / "kakaotalk" / "keys" / "keystore.json"))
        root = keychain.read_root_hex(str(self.parent / "kakaotalk" / "keys" / "root_v2.hex"))
        self.assertEqual(root, ROOT)
        ks = json.loads((self.parent / "kakaotalk" / "keys" / "keystore.json").read_text(encoding="utf-8"))
        self.assertEqual(len(ks), 2)                                # the keystore shape capture loads
        # the keystore the setup wrote re-derives cleanly (the --provision rerun's core step)
        cfgdict = json.loads((self.parent / "kakaotalk" / "config.json").read_text(encoding="utf-8"))
        with quiet():
            self.assertEqual(provision.write_keystore_for(
                cfgdict, lambda m: None, ROOT,
                keystore.load_store(str(self.fx.user_dir / "appstate.dat")), str(self.fx.user_dir)), [])


class TestUseridInMemory(unittest.TestCase):
    """The ActionLogDB userId lookup decrypts in memory: no plaintext file beside the source, ever."""

    def setUp(self):
        import sqlite3
        import tempfile
        self.base = Path(tempfile.mkdtemp(prefix="llmw-"))
        self.src_dir, work = self.base / "client", self.base / "work"
        self.src_dir.mkdir()
        work.mkdir()
        self.images = {}
        for name, ddl in (("good", "CREATE TABLE Common(userid INTEGER); INSERT INTO Common VALUES (4242);"),
                          ("bare", "CREATE TABLE Other(x INTEGER);")):
            p = work / (name + ".sqlite")
            con = sqlite3.connect(p)
            con.execute("PRAGMA journal_mode=wal")                    # the WAL header must still open
            con.executescript(ddl)
            con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            con.close()
            self.images[name] = p.read_bytes()
        self.src = self.src_dir / "ActionLogDB.edb_1.backup"
        self.src.write_bytes(b"\0" * 16)
        self.connected = []
        real = sqlite3.connect
        self.connect = lambda target, *a, **kw: (self.connected.append(target), real(target, *a, **kw))[1]

    def tearDown(self):
        shutil.rmtree(self.base, onerror=lambda *a: None)

    def lookup(self, image, live):
        with mock.patch.object(kd, "read_shared", lambda p: b"\0" * 16), \
                mock.patch.object(kd, "eva_decrypt", lambda data, key, iv: image), \
                mock.patch.object(kd, "sqlcipher_decrypt", lambda data, key: image), \
                mock.patch.object(kd.sqlite3, "connect", self.connect):
            return kd.get_userid_live(str(self.src)) if live else kd.get_userid_backup(str(self.src), "pragma")

    def test_success_and_missing_table_write_nothing(self):
        import sqlite3
        for live in (False, True):
            self.assertEqual(self.lookup(self.images["good"], live), "4242")
            with self.assertRaises(sqlite3.Error):
                self.lookup(self.images["bare"], live)
        self.assertEqual(sorted(p.name for p in self.src_dir.iterdir()), [self.src.name])
        self.assertEqual(set(self.connected), {":memory:"})


if __name__ == "__main__":
    unittest.main()
