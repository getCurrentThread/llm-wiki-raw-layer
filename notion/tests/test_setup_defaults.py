"""NotionConfig.setup_defaults (the archive; the desktop database in the owner's profile) and task_checks (sync only
from the desktop source, with its database named)."""
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from rawlayer import conf, registry
from . import fixtures  # noqa: F401 -- the data parent under the temp folder

EXAMPLE = Path(__file__).resolve().parents[1] / "config.example.json"


class Base(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="nt-setup-"))
        self.home = Path(tempfile.mkdtemp(prefix="nt-home-"))
        for d in (self.root, self.home):
            self.addCleanup(shutil.rmtree, d, True)
        patcher = mock.patch.object(conf, "settings", lambda: SimpleNamespace(HOME_DIR=self.home))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.app = registry.get().apps[0]


class SetupDefaults(Base):
    def test_desktop_database_when_it_is_there(self):
        cfg = json.loads(EXAMPLE.read_text(encoding="utf-8"))
        cfg["data_root"] = str(self.root)
        notes = []
        self.app.setup_defaults(cfg, notes.append)
        self.assertEqual(cfg["sources"]["nt_archive"], str(self.root / "archive"))
        self.assertTrue((self.root / "archive").is_dir())
        self.assertEqual(cfg["nt_desktop_path"], "")
        db = self.home / "AppData" / "Roaming" / "Notion" / "notion.db"
        db.parent.mkdir(parents=True)
        db.write_bytes(b"")
        self.app.setup_defaults(cfg, notes.append)
        self.assertEqual(cfg["nt_desktop_path"], str(db))
        cfg["nt_desktop_path"] = str(self.root / "kept.db")
        self.app.setup_defaults(cfg, notes.append)
        self.assertEqual(cfg["nt_desktop_path"], str(self.root / "kept.db"))
        self.assertFalse(any(str(self.root) in n or str(self.home) in n for n in notes))

    def test_api_source_names_no_database(self):
        cfg = json.loads(EXAMPLE.read_text(encoding="utf-8"))
        cfg.update(data_root=str(self.root), nt_source="api")
        db = self.home / "AppData" / "Roaming" / "Notion" / "notion.db"
        db.parent.mkdir(parents=True)
        db.write_bytes(b"")
        self.app.setup_defaults(cfg, lambda _n: None)
        self.assertEqual(cfg["nt_desktop_path"], "")


class TaskChecks(Base):
    def cfg(self, **kv):
        base = {"nt_sync": True, "nt_source": "desktop", "nt_desktop_path": ""}
        base.update(kv)
        return SimpleNamespace(**base)

    def test_cases(self):
        db = self.root / "notion.db"
        db.write_bytes(b"")
        self.assertEqual(self.app.task_checks(self.cfg(nt_sync=False, nt_source="api")), [])
        self.assertIn("ntn keychain", self.app.task_checks(self.cfg(nt_source="api"))[0])
        for path, ok in (("", False), ("notion.db", False), (str(self.root / "none.db"), False),
                         (str(self.root), False), (str(db), True)):
            with self.subTest(path=path):
                self.assertEqual(self.app.task_checks(self.cfg(nt_desktop_path=path)) == [], ok)
