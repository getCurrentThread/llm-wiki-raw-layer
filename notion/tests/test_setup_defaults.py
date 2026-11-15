"""NotionConfig.task_checks: sync needs the desktop database named (setup_defaults:
tests/tools/test_setup_defaults.py)."""
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from rawlayer import registry


class TaskChecks(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="nt-setup-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.app = registry.get().apps[0]

    def cfg(self, **kv):
        base = {"nt_sync": True, "nt_desktop_path": ""}
        base.update(kv)
        return SimpleNamespace(**base)

    def test_cases(self):
        db = self.root / "notion.db"
        db.write_bytes(b"")
        self.assertEqual(self.app.task_checks(self.cfg(nt_sync=False)), [])
        for path, ok in (("", False), ("notion.db", False), (str(self.root / "none.db"), False),
                         (str(self.root), False), (str(db), True)):
            with self.subTest(path=path):
                self.assertEqual(self.app.task_checks(self.cfg(nt_desktop_path=path)) == [], ok)
