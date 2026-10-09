import json
import shutil
import tempfile
import unittest
from pathlib import Path

from rawlayer import registry
from . import fixtures  # noqa: F401 -- the data parent under the temp folder

EXAMPLE = Path(__file__).resolve().parents[1] / "config.example.json"


class SetupDefaults(unittest.TestCase):
    """GmailConfig.setup_defaults on the config setup writes from config.example.json (data_root set by setup)."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="gm-setup-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.cfg = json.loads(EXAMPLE.read_text(encoding="utf-8"))
        self.cfg["data_root"] = str(self.root)
        self.notes = []

    def fill(self):
        registry.get().apps[0].setup_defaults(self.cfg, self.notes.append)

    def test_fills_the_data_root_paths(self):
        self.fill()
        self.assertEqual(self.cfg["sources"]["gm_archive"], str(self.root / "archive"))
        self.assertEqual(self.cfg["gm_client_file"], str(self.root / "client.json"))
        self.assertEqual(self.cfg["gm_token_file"], str(self.root / "token.json"))
        self.assertTrue((self.root / "archive").is_dir())
        self.assertFalse(any(str(self.root) in n for n in self.notes))
        self.assertIn("sources.gm_archive: folder created", self.notes)

    def test_keeps_what_the_owner_set(self):
        self.cfg["gm_client_file"] = str(self.root / "given.json")
        self.fill()
        self.assertEqual(self.cfg["gm_client_file"], str(self.root / "given.json"))
        self.assertNotIn("gm_client_file: set to its default", self.notes)
        before = json.dumps(self.cfg, sort_keys=True)
        self.notes.clear()
        self.fill()
        self.assertEqual(json.dumps(self.cfg, sort_keys=True), before)
        self.assertEqual(self.notes, [])

    def test_no_folder_outside_the_data_parent(self):
        # a data root outside the protected data parent (a hand-made config): the paths are filled, no folder made
        from unittest import mock
        from rawlayer.capture import paths
        elsewhere = Path(tempfile.mkdtemp(prefix="gm-parent-"))
        self.addCleanup(shutil.rmtree, elsewhere, True)
        with mock.patch.object(paths, "DATA_PARENT", elsewhere):
            self.fill()
        self.assertEqual(self.cfg["sources"]["gm_archive"], str(self.root / "archive"))
        self.assertFalse((self.root / "archive").exists())
        self.assertNotIn("sources.gm_archive: folder created", self.notes)

    def test_no_data_root(self):
        del self.cfg["data_root"]
        self.fill()
        self.assertEqual(self.notes, [])
