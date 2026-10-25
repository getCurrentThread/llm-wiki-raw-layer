import json
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from rawlayer import conf, registry
from .fixtures import TEST_PARENT

EXAMPLE = Path(__file__).resolve().parents[1] / "config.example.json"


class SetupDefaultsTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="cc-setup-", dir=TEST_PARENT))
        self.home = Path(tempfile.mkdtemp(prefix="cc-home-", dir=TEST_PARENT))
        for p in (self.root, self.home):
            self.addCleanup(shutil.rmtree, p, True)
        patcher = patch.object(conf, "settings", lambda: SimpleNamespace(HOME_DIR=self.home))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.app = registry.get().apps[0]
        self.cfg = json.loads(EXAMPLE.read_text(encoding="utf-8"))
        self.cfg["data_root"] = str(self.root)
        self.notes = []

    def test_setup_defaults_profile_when_present(self):
        self.app.setup_defaults(self.cfg, self.notes.append)
        self.assertEqual(self.cfg["sources"]["cc_archive"], str(self.root / "archive"))
        self.assertTrue((self.root / "archive").is_dir())
        self.assertEqual(self.cfg["cc_claude_dir"], "")
        source = self.home / ".claude"
        source.mkdir()
        self.app.setup_defaults(self.cfg, self.notes.append)
        self.assertEqual(self.cfg["cc_claude_dir"], str(source))
        self.assertFalse(any(str(self.root) in n or str(self.home) in n for n in self.notes))

    def test_setup_defaults_preserve_owner_values(self):
        self.cfg["sources"]["cc_archive"] = str(self.root / "custom")
        self.cfg["cc_claude_dir"] = str(self.home / "custom")
        before = json.loads(json.dumps(self.cfg))
        self.app.setup_defaults(self.cfg, self.notes.append)
        self.assertEqual(self.cfg, before)

    def test_setup_defaults_without_data_root(self):
        self.cfg.pop("data_root")
        before = json.loads(json.dumps(self.cfg))
        self.app.setup_defaults(self.cfg, self.notes.append)
        self.assertEqual(self.cfg, before)
        self.assertEqual(self.notes, [])

    def test_setup_defaults_task_checks(self):
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
