# -*- coding: utf-8 -*-
"""The apps' AppConfig.setup_defaults (appkit/setup.py) on the config `manage.py setup` writes from each
config.example.json, one table row per app: the archive and data-root files are filled, a profile path only when it
is there, a value the owner set is kept, and the notes name keys, never paths. The task_checks the apps share
(appkit.setup.existing_path) are tested in discord's suite; kakaotalk's defaults are its provisioning (kakaotalk/tests/test_provision.py)."""
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from appkit.setup import get, unset
from rawlayer import conf, registry
from rawlayer.capture import paths
from rawlayer.management.commands.setup import placeholders

PROJECT = Path(__file__).resolve().parents[2]
# app: (archive key, {data-root key: name}, profile key and its parts or None, a file (not a folder), placeholders left)
ROWS = {
    "claudecode": ("sources.cc_archive", {}, ("cc_claude_dir", (".claude",)), False, []),
    "discord": ("sources.dc_archive", {}, ("dc_desktop_path", ("AppData", "Roaming", "discord")), False, []),
    "gmail": ("sources.gm_archive", {"gm_imap_password_file": "app_password.txt"}, None, False, []),
    "mattermost": ("sources.mm_archive", {"mm_token_file": "token.txt"}, None, False,
                   ["mm_exclude_patterns", "mm_url"]),
    "notion": ("sources.nt_archive", {}, ("nt_desktop_path", ("AppData", "Roaming", "Notion", "notion.db")), True, []),
}


class SetupDefaults(unittest.TestCase):
    def setUp(self):
        parent = mock.patch.object(paths, "DATA_PARENT", Path(tempfile.gettempdir()))
        parent.start()
        self.addCleanup(parent.stop)

    def test_every_app(self):
        for name, (archive, data, profile, is_file, left) in ROWS.items():
            with self.subTest(app=name):
                root, home = (Path(tempfile.mkdtemp(prefix=f"{name}-{x}-")) for x in ("setup", "home"))
                for d in (root, home):
                    self.addCleanup(shutil.rmtree, d, True)
                example = json.loads((PROJECT / name / "config.example.json").read_text(encoding="utf-8"))
                cfg = dict(example, data_root=str(root))
                app, notes = registry.app_registry(name).apps[0], []
                with mock.patch.object(conf, "settings", lambda home=home: SimpleNamespace(HOME_DIR=home)):
                    app.setup_defaults(cfg, notes.append)
                    self.assertEqual(get(cfg, archive), str(root / "archive"))
                    self.assertTrue((root / "archive").is_dir())
                    self.assertIn(f"{archive}: folder created", notes)
                    for key, file in data.items():
                        self.assertEqual(cfg[key], str(root / file))
                    self.assertEqual(sorted(placeholders(cfg)), left)
                    if profile:
                        key, parts = profile
                        self.assertTrue(unset(cfg[key]))                # nothing in this profile yet
                        p = home.joinpath(*parts)
                        if is_file:
                            p.parent.mkdir(parents=True)
                            p.write_bytes(b"")
                        else:
                            p.mkdir(parents=True)
                        app.setup_defaults(cfg, notes.append)
                        self.assertEqual(cfg[key], str(p))
                    owners = dict(cfg, sources=dict(cfg["sources"], **{archive.split(".")[1]: str(root / "kept")}),
                                  **{k: str(root / "elsewhere") for k in [*data, *([profile[0]] if profile else [])]})
                    before = json.loads(json.dumps(owners))
                    app.setup_defaults(owners, notes.append)
                    self.assertEqual(owners, before)                     # the owner's values are kept
                self.assertFalse(any(str(root) in n or str(home) in n for n in notes))
                if app.wiki:                                             # `collect <app>`'s keys are in the example
                    self.assertTrue({op.key for op in app.options if "_wiki_" in op.key} <= set(example))
