"""DiscordConfig.task_checks: what the SYSTEM task needs named in the config (setup_defaults:
tests/tools/test_setup_defaults.py)."""
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from rawlayer import registry


class TaskChecks(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="dc-setup-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.app = registry.get().apps[0]

    def cfg(self, **kv):
        base = {"dc_sync": True, "dc_desktop_path": ""}
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
