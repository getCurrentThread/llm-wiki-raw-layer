# -*- coding: utf-8 -*-
"""setup's kakaotalk side (kakaotalk/provision.py): the default sources this machine's client installation and
kakatok research tree provide, the root-provisioning chain (an installed root stays while it unwraps the store,
a research copy is adopted only after it verifies, the memory scan's unwrap oracle), and the setup command end
to end on a throwaway data parent. Nothing here touches the real client, the real research tree or the real
process: every input is synthetic under a temp directory, and the client lookups are patched out.
Run from the project root, like every app suite:

    python -s -B -m unittest discover -s kakaotalk/tests -t .
"""
import json
import shutil
import unittest
from pathlib import Path
from unittest import mock

from kakaotalk import katok_decrypt as kd
from kakaotalk import keychain, keystore_v2, provision
from kakaotalk.tests import edbfix as E
from kakaotalk.tests.fixtures import Fixture
from kakaotalk.tests.test_selfderive import ROOT, SALT, quiet, wrap_dek, write_appstate

REPO = Path(__file__).resolve().parents[2]


class Notes(list):
    """The setup line collector the hooks print through."""

    def __call__(self, msg):
        self.append(msg)


def fake_home(base: Path, with_research=True) -> Path:
    """A home with the kakatok research tree's shape: the collector outputs, and a root copy."""
    home = base / "home"
    for rel in (("Documents", "kakatok", "out", "live.sqlite"),
                ("Documents", "kakatok", "out", "keystore_v2_full.json"),
                ("Downloads", "kakatok", "merged", "merged.sqlite")):
        p = home.joinpath(*rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"")
    for rel in (("Downloads", "kakatok", "backup_decrypted"), ("Downloads", "kakatok", "decrypted_live")):
        (home / Path(*rel)).mkdir(parents=True, exist_ok=True)
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

    def test_research_tree_and_client_detected(self):
        home = fake_home(self.fx.base)
        users = self.fx.base / "users"
        udir = users / "3f"
        udir.mkdir(parents=True)
        (udir / "appstate.dat").write_bytes(b"")
        cfg = {"data_root": str(self.fx.data_root)}
        d = provision.default_sources(cfg, home=home, users=str(users))
        self.assertEqual(d, {
            "merged_out": str(home / "Downloads" / "kakatok" / "merged" / "merged.sqlite"),
            "live_out": str(home / "Documents" / "kakatok" / "out" / "live.sqlite"),
            "backup_decrypted": str(home / "Downloads" / "kakatok" / "backup_decrypted"),
            "decrypted_live": str(home / "Downloads" / "kakatok" / "decrypted_live"),
            "keystore": str(home / "Documents" / "kakatok" / "out" / "keystore_v2_full.json"),
            "kakao_user_dir": str(udir),
        })

    def test_outputs_fall_back_to_the_data_root(self):
        home = self.fx.base / "empty-home"
        home.mkdir()
        cfg = {"data_root": str(self.fx.data_root)}
        d = provision.default_sources(cfg, home=home, users=str(self.fx.base / "no-users"))
        self.assertEqual(d, {"backup_decrypted": str(self.fx.data_root / "backup_decrypted"),
                             "keystore": str(self.fx.data_root / "keys" / "keystore.json")})

    def test_fill_defaults_fills_placeholders_only(self):
        home = fake_home(self.fx.base)
        cfg = {"data_root": str(self.fx.data_root), "sources": {
            "merged_out": "<abs path>\\merged.sqlite",
            "live_out": "C:\\elsewhere\\live.sqlite",          # the user's own value stays
            "keystore": "<abs path>\\keystore_v2_full.json",
        }}
        notes = Notes()
        filled = provision.fill_defaults(cfg, home=home, users=str(self.fx.base / "no-users"), note=notes)
        self.assertEqual(sorted(filled), ["backup_decrypted", "decrypted_live", "keystore", "merged_out"])
        self.assertEqual(cfg["sources"]["live_out"], "C:\\elsewhere\\live.sqlite")
        self.assertEqual(cfg["sources"]["merged_out"],
                         str(home / "Downloads" / "kakatok" / "merged" / "merged.sqlite"))
        self.assertEqual(cfg["sources"]["keystore"],
                         str(home / "Documents" / "kakatok" / "out" / "keystore_v2_full.json"))
        self.assertEqual(notes, ["default sources set: backup_decrypted, decrypted_live, keystore, merged_out"])


class TestRootOracle(unittest.TestCase):
    def setUp(self):
        self.rels = [r"chat_data\chatLogs_31.edb", r"chat_data\chatLogs_32.edb"]
        self.store = {"version": "v2:", "salt": SALT,
                      "map": {r: wrap_dek(r, bytes([0x60 + i]) * 32) for i, r in enumerate(self.rels)}}

    def test_finds_the_candidate_that_unwraps(self):
        cands = [bytes(range(1, 33)), ROOT, bytes(range(2, 34))]
        self.assertEqual(provision.root_in(cands, self.store), ROOT)

    def test_no_hit_and_no_entries(self):
        self.assertIsNone(provision.root_in([bytes(range(1, 33))], self.store))
        self.assertIsNone(provision.root_in([ROOT], {"version": "v2:", "salt": SALT, "map": {}}))


class TestProvision(unittest.TestCase):
    """The provisioning chain against a synthetic appstate.dat and research tree (no client, no scan)."""

    def setUp(self):
        self.fx = Fixture()
        for chat in (31, 32):
            E.place_room(self.fx, chat, [E.msg(chat, i) for i in (1, 2, 3)], E.key_for(chat))
        wrapped = {f"chat_data\\chatLogs_{c}.edb": wrap_dek(f"chat_data\\chatLogs_{c}.edb", E.key_for(c))
                   for c in (31, 32)}
        wrapped["chat_data\\ocii_v2.edb"] = wrap_dek("chat_data\\ocii_v2.edb", b"\x01" * 32)   # non-room entry
        write_appstate(self.fx.user_dir, wrapped)
        self.home = fake_home(self.fx.base)
        self.data_root = self.fx.data_root
        self.cfg = {"data_root": str(self.data_root), "sources": {
            "kakao_user_dir": str(self.fx.user_dir),
            "keystore": str(self.data_root / "keys" / "keystore.json"),
            "backup_decrypted": str(self.data_root / "backup_decrypted"),
        }}

    def tearDown(self):
        shutil.rmtree(self.fx.base, onerror=lambda *a: None)

    def run_provision(self, cfg=None, restart=False):
        notes = Notes()
        with mock.patch.object(kd, "_find_pid", lambda name="KakaoTalk.exe": None):
            problems = provision.provision(cfg or self.cfg, notes, restart, home=self.home)
        return problems, notes

    def installed_root(self):
        p = self.data_root / "keys" / "root_v2.hex"
        return keychain.read_root_hex(str(p)) if p.exists() else None

    def keystore(self):
        p = self.data_root / "keys" / "keystore.json"
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None

    def test_adopts_a_verified_copy_and_writes_the_keystore(self):
        problems, notes = self.run_provision(self.cfg)
        self.assertEqual(problems, [])
        self.assertEqual(self.installed_root(), ROOT)                 # verified against the store, then installed
        self.assertTrue((self.data_root / "backup_decrypted").is_dir())
        ks = self.keystore()
        self.assertEqual(len(ks), 3)                                  # two rooms and the non-room entry
        self.assertEqual(sum(1 for e in ks.values() if e["header_ok"] is True), 2)
        self.assertTrue(any("root adopted" in n for n in notes))

    def test_an_installed_live_root_stays(self):
        keys_dir = self.data_root / "keys"
        keys_dir.mkdir(parents=True)
        (keys_dir / "root_v2.hex").write_text(ROOT.hex())
        notes = Notes()
        problems = provision.provision(self.cfg, notes, home=self.home)   # scan patch not needed: alive root first
        self.assertEqual(problems, [])
        self.assertTrue(any("entries unwrapped (alive)" in n for n in notes))
        self.assertFalse(any("adopted" in n for n in notes))

    def test_a_dead_installed_root_is_replaced(self):
        keys_dir = self.data_root / "keys"
        keys_dir.mkdir(parents=True)
        (keys_dir / "root_v2.hex").write_text((b"\x22" * 32).hex())      # a rotated root unwraps nothing
        problems, _notes = self.run_provision()
        self.assertEqual(problems, [])
        self.assertEqual(self.installed_root(), ROOT)

    def test_a_partial_root_is_kept_when_nothing_better_exists(self):
        alt = b"\x77" * 32
        wrapped = {}
        for r, dek in ((r"chat_data\chatLogs_31.edb", E.key_for(31)),
                       (r"chat_data\chatLogs_32.edb", E.key_for(32))):
            root = ROOT if r.endswith("31.edb") else alt        # 32 is wrapped under another root
            wrapped[r] = keychain.aes_kw_wrap(
                keychain.hkdf_sha256(root, SALT, ("v2:" + r).encode("latin1"), 32), dek)
        write_appstate(self.fx.user_dir, wrapped)
        keys_dir = self.data_root / "keys"
        keys_dir.mkdir(parents=True)
        (keys_dir / "root_v2.hex").write_text(ROOT.hex())              # unwraps 1 of 2: partial
        (self.home / "Documents" / "kakatok" / "root_v2.hex").unlink()  # no copy to adopt
        problems, _notes = self.run_provision()
        self.assertEqual(problems, [])                                  # partial still keys most rooms
        self.assertEqual(self.installed_root(), ROOT)
        self.assertEqual(len(self.keystore()), 1)

    def test_no_root_anywhere_is_an_attention(self):
        for rel in provision.ROOT_FILES:
            p = self.home.joinpath(*rel)
            if p.exists():
                p.unlink()
        problems, _notes = self.run_provision()
        self.assertEqual(len(problems), 1)
        self.assertIn("no live root", problems[0])
        self.assertIsNone(self.installed_root())
        self.assertIsNone(self.keystore())

    def test_a_missing_client_directory_is_named(self):
        cfg = {"data_root": str(self.data_root),
               "sources": {"kakao_user_dir": "<abs dir>\\users\\<live user dir>"}}
        problems, _notes = self.run_provision(cfg)
        self.assertEqual(len(problems), 1)
        self.assertIn("kakao_user_dir is not set", problems[0])


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
        self.assertEqual(cfg["sources"]["live_out"], str(self.home / "Documents" / "kakatok" / "out" / "live.sqlite"))
        self.assertEqual(cfg["sources"]["backup_decrypted"],
                         str(self.home / "Downloads" / "kakatok" / "backup_decrypted"))
        self.assertEqual(cfg["sources"]["keystore"], str(self.home / "Documents" / "kakatok" / "out"
                                                         / "keystore_v2_full.json"))
        root = keychain.read_root_hex(str(self.parent / "kakaotalk" / "keys" / "root_v2.hex"))
        self.assertEqual(root, ROOT)
        ks = json.loads((self.home / "Documents" / "kakatok" / "out" / "keystore_v2_full.json")
                        .read_text(encoding="utf-8"))
        self.assertEqual(len(ks), 2)                                # the keystore shape capture loads
        # the keystore the setup wrote re-derives cleanly (the --provision rerun's core step)
        cfgdict = json.loads((self.parent / "kakaotalk" / "config.json").read_text(encoding="utf-8"))
        with quiet():
            self.assertEqual(provision.write_keystore_for(
                cfgdict, lambda m: None, ROOT,
                keystore_v2.load_store(str(self.fx.user_dir / "appstate.dat")), str(self.fx.user_dir)), [])


if __name__ == "__main__":
    unittest.main()
