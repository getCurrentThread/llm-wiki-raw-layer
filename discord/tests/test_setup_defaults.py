"""DiscordConfig.task_checks: what the SYSTEM task needs named in the config (setup_defaults:
tests/tools/test_setup_defaults.py)."""
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from rawlayer import registry
from .fixtures import TEST_PARENT


class TaskChecks(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="dc-setup-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.app = registry.get().apps[0]

    def cfg(self, **kv):
        base = {"dc_sync": True, "dc_source": "desktop", "dc_desktop_path": "", "dc_token_file": ""}
        base.update(kv)
        return SimpleNamespace(**base)

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
                mock.patch("discord.fetch.machine_token_vars", return_value=[]):
            for path, want in cases:
                with self.subTest(path=path):
                    found = self.app.task_checks(self.cfg(dc_source="api", dc_token_file=path))
                    self.assertEqual(len(found), 1)
                    self.assertIn(want, found[0])
            token.write_text("x", encoding="utf-8")
            self.assertEqual(self.app.task_checks(self.cfg(dc_source="api", dc_token_file=str(token))), [])
            with mock.patch("discord.fetch.machine_token_vars", return_value=["DISCORD_BOT_TOKEN"]):
                found = self.app.task_checks(self.cfg(dc_source="api", dc_token_file=str(token)))
            self.assertEqual(len(found), 1)
            self.assertIn("machine-wide DISCORD_BOT_TOKEN", found[0])
