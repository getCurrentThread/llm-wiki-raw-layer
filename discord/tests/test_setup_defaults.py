"""DiscordConfig.setup_defaults (the archive; the desktop cache in the owner's profile, or the api token file) and
task_checks (what the SYSTEM task needs named in the config)."""
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from rawlayer import conf, registry
from .fixtures import TEST_PARENT

EXAMPLE = Path(__file__).resolve().parents[1] / "config.example.json"


class Base(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="dc-setup-"))
        self.home = Path(tempfile.mkdtemp(prefix="dc-home-"))
        for d in (self.root, self.home):
            self.addCleanup(shutil.rmtree, d, True)
        patcher = mock.patch.object(conf, "settings", lambda: SimpleNamespace(HOME_DIR=self.home))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.app = registry.get().apps[0]


class SetupDefaults(Base):
    def setUp(self):
        super().setUp()
        self.cfg = json.loads(EXAMPLE.read_text(encoding="utf-8"))
        self.cfg["data_root"] = str(self.root)
        self.notes = []

    def test_desktop_cache_when_it_is_there(self):
        self.app.setup_defaults(self.cfg, self.notes.append)
        self.assertEqual(self.cfg["sources"]["dc_archive"], str(self.root / "archive"))
        self.assertEqual(self.cfg["dc_desktop_path"], "")           # no Discord desktop in this profile
        self.assertEqual(self.cfg["dc_token_file"], "")
        cache = self.home / "AppData" / "Roaming" / "discord"
        cache.mkdir(parents=True)
        self.app.setup_defaults(self.cfg, self.notes.append)
        self.assertEqual(self.cfg["dc_desktop_path"], str(cache))
        self.assertFalse(any(str(self.root) in n or str(self.home) in n for n in self.notes))

    def test_api_token_file(self):
        self.cfg["dc_source"] = "api"
        self.app.setup_defaults(self.cfg, self.notes.append)
        self.assertEqual(self.cfg["dc_token_file"], str(self.root / "token.txt"))
        self.assertEqual(self.cfg["dc_desktop_path"], "")
        self.assertFalse((self.root / "token.txt").exists())        # the owner writes it (6.2 handshake)


class TaskChecks(Base):
    def cfg(self, **kv):
        base = {"dc_sync": True, "dc_source": "desktop", "dc_desktop_path": "", "dc_token_file": ""}
        base.update(kv)
        return SimpleNamespace(**base)

    def test_sync_off(self):
        self.assertEqual(self.app.task_checks(self.cfg(dc_sync=False)), [])

    def test_desktop(self):
        f = self.root / "file"
        f.write_text("x", encoding="utf-8")
        for path, ok in (("", False), ("relative\\discord", False), (str(self.root / "none"), False),
                         (str(f), False), (str(self.root), True)):
            with self.subTest(path=path):
                found = self.app.task_checks(self.cfg(dc_desktop_path=path))
                self.assertEqual(found == [], ok, found)

    def test_api_token_file(self):
        inside = Path(tempfile.mkdtemp(prefix="dc-tok-", dir=TEST_PARENT))
        self.addCleanup(shutil.rmtree, inside, True)
        outside = Path(TEST_PARENT).parent / "dc-not-under-the-parent" / "token.txt"
        token = inside / "token.txt"
        cases = (("", "protected"), ("token.txt", "protected"), (str(outside), "protected"),
                 (str(token), "requires a token file"))
        with mock.patch("rawlayer.capture.precheck.check"), \
                mock.patch("discord.collector.machine_token_vars", return_value=[]):
            for path, want in cases:
                with self.subTest(path=path):
                    found = self.app.task_checks(self.cfg(dc_source="api", dc_token_file=path))
                    self.assertEqual(len(found), 1)
                    self.assertIn(want, found[0])
            token.write_text("x", encoding="utf-8")
            self.assertEqual(self.app.task_checks(self.cfg(dc_source="api", dc_token_file=str(token))), [])
            with mock.patch("discord.collector.machine_token_vars", return_value=["DISCORD_BOT_TOKEN"]):
                found = self.app.task_checks(self.cfg(dc_source="api", dc_token_file=str(token)))
            self.assertEqual(len(found), 1)
            self.assertIn("machine-wide DISCORD_BOT_TOKEN", found[0])

    def test_api_token_folder_failing_precheck(self):
        # the collector's own precheck of the token's folder (ACL, Windows Search): the same refusal as a bad path
        from rawlayer.capture.config import ConfigError
        token = Path(tempfile.mkdtemp(prefix="dc-tok-", dir=TEST_PARENT)) / "token.txt"
        self.addCleanup(shutil.rmtree, token.parent, True)
        token.write_text("x", encoding="utf-8")
        with mock.patch("rawlayer.capture.precheck.check", side_effect=ConfigError("data root ACL unreadable")):
            found = self.app.task_checks(self.cfg(dc_source="api", dc_token_file=str(token)))
        self.assertEqual(len(found), 1)
        self.assertIn("protected token path", found[0])
