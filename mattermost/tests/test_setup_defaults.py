# -*- coding: utf-8 -*-
"""MatterConfig.setup_defaults on the config setup writes from config.example.json (data_root set by setup): the
archive and the token file in the data root; mm_url and mm_exclude_patterns stay the owner's to give."""
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from rawlayer import registry
from rawlayer.management.commands.setup import placeholders
from . import fixtures  # noqa: F401 -- the data parent under the temp folder

EXAMPLE = Path(__file__).resolve().parents[1] / "config.example.json"


class SetupDefaults(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="mm-setup-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.cfg = json.loads(EXAMPLE.read_text(encoding="utf-8"))
        self.cfg.update(data_root=str(self.root), inputs_root=str(self.root.parent / "inputs"))   # setup's own
        self.notes = []

    def test_fills_paths_not_the_owners_values(self):
        registry.get().apps[0].setup_defaults(self.cfg, self.notes.append)
        self.assertEqual(self.cfg["sources"]["mm_archive"], str(self.root / "archive"))
        self.assertEqual(self.cfg["mm_token_file"], str(self.root / "token.txt"))
        self.assertTrue((self.root / "archive").is_dir())
        self.assertEqual(sorted(placeholders(self.cfg)), ["mm_exclude_patterns", "mm_url"])
        self.assertFalse(any(str(self.root) in n for n in self.notes))

    def test_keeps_what_the_owner_set(self):
        self.cfg["mm_token_file"] = str(self.root / "elsewhere.txt")
        self.cfg["sources"]["mm_archive"] = str(self.root / "kept")
        registry.get().apps[0].setup_defaults(self.cfg, self.notes.append)
        self.assertEqual(self.cfg["mm_token_file"], str(self.root / "elsewhere.txt"))
        self.assertEqual(self.cfg["sources"]["mm_archive"], str(self.root / "kept"))
        self.assertEqual(self.notes, ["sources.mm_archive: folder created"])
