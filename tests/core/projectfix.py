# -*- coding: utf-8 -*-
"""A throwaway project for the tests of discovery, selection and the commands: a folder under the temp folder with
app folders written from code, which conf.PROJECT_DIR points at while the project is entered. The registry starts and
ends clean (nothing selected), and the app packages it imported are forgotten on the way out."""
import importlib
import sys
import tempfile
import types
from pathlib import Path
from unittest import mock

from rawlayer import conf, registry
from rawlayer.capture import paths, publicout
from tests.core import toyfix as T


def app_files(label, origins="()", extra=None):
    """The files of a minimal app (no input, no reader): its label, and ORIGINS as Python source."""
    files = {
        "__init__.py": "",
        "apps.py": ("from rawlayer.registry import AppConfig\n\n\n"
                    f"class Config(AppConfig):\n    label = {label!r}\n"),
        "origins.py": ("from tests.core.toyapp.origins import ToyA, ToyB, ToyM  # noqa: F401\n\n"
                       f"ORIGINS = {origins}\n"),
        "inputs.py": ("def resolve(cfg, input_set, frozen_root):\n    return {}\n\n\n"
                      "def static_roots(inputs):\n    return {}\n"),
        "vocab.py": "WORDS = ()\nPATTERNS = ()\n",
    }
    files.update(extra or {})
    return files


class Project:
    """with Project({"name": app_files(...), ...}) as proj: proj.dir is the project folder."""

    def __init__(self, apps, exclude=()):
        self.apps, self.exclude = apps, tuple(exclude)

    def write(self, rel, text):
        p = self.dir.joinpath(*rel.split("/"))
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8", newline="\n")

    def __enter__(self):
        # resolved, as conf.PROJECT_DIR is
        self.dir = Path(tempfile.mkdtemp(prefix="llmw-proj-", dir=T.TEST_PARENT)).resolve()
        for app, files in self.apps.items():
            for rel, text in files.items():
                self.write(f"{app}/{rel}", text)
        self.settings = types.SimpleNamespace(BASE_DIR=self.dir, HOME_DIR=conf.user_home(self.dir),
                                              IMPL_NAME=paths.IMPL_NAME, DATA_PARENT=T.TEST_PARENT,
                                              EXCLUDE_APPS=self.exclude)
        self._patches = [mock.patch.object(conf, "PROJECT_DIR", self.dir),
                         mock.patch.object(conf, "settings", lambda: self.settings)]
        for p in self._patches:
            p.start()
        self._state = (registry._current, registry._selected, list(sys.path), set(sys.modules))
        registry._current, registry._selected = None, None
        importlib.invalidate_caches()
        return self

    def __exit__(self, *exc):
        prev, selected, path, modules = self._state
        for p in reversed(self._patches):
            p.stop()
        sys.path[:] = path
        for name in set(sys.modules) - modules:
            if name.split(".")[0] in self.apps:
                del sys.modules[name]
        registry._selected = selected
        if prev is not None:
            registry._install(prev)
        else:
            registry._current = None
            publicout.uninstall()
        T.force_rmtree(self.dir)
        return False
