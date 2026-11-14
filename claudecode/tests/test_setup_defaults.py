"""ClaudeCodeConfig.task_checks (setup_defaults: tests/tools/test_setup_defaults.py)."""
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from rawlayer import registry
from .fixtures import TEST_PARENT


class TaskChecks(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="cc-setup-", dir=TEST_PARENT))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.app = registry.get().apps[0]

    def test_task_checks(self):
        f = self.root / "file"
        f.write_text("synthetic", encoding="utf-8")
        for value, ok in (("", False), ("relative/.claude", False), (str(self.root / "missing"), False),
                          (str(f), False), (str(self.root), True)):
            with self.subTest(value=value):
                errors = self.app.task_checks(SimpleNamespace(cc_sync=True, cc_claude_dir=value))
                self.assertEqual(errors == [], ok)
                if not ok:
                    self.assertEqual(errors, ["cc_sync requires an absolute cc_claude_dir to an existing folder "
                                              "(SYSTEM has another home)"])
        self.assertEqual(self.app.task_checks(SimpleNamespace(cc_sync=False, cc_claude_dir="")), [])
