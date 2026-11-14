# -*- coding: utf-8 -*-
"""Config loading and input resolution (SPEC 2, 16). Never derives a path from the environment.

The "sources" entries and top-level options a config must carry are the installed apps' (AppConfig.source_keys,
AppConfig.options); the input paths are resolved by each app's inputs.py."""
import json
import os
from pathlib import Path

from .. import registry
from . import paths


class ConfigError(Exception):
    """Precondition / configuration refusal -> exit 2."""


class Config:
    def __init__(self, path):
        try:
            with open(path, encoding="utf-8") as fh:    # as given: Path() would drop a trailing separator
                d = json.load(fh)
        except (OSError, ValueError) as e:
            raise ConfigError("config unreadable") from e
        if not isinstance(d, dict) or d.get("v") != 1:
            raise ConfigError("config version")
        self.path = Path(os.path.abspath(path))     # abspath: ".." folded without following links
        self.impl = d.get("impl")
        if self.impl != paths.IMPL_NAME:            # recorded in every manifest; never defaulted
            raise ConfigError("impl")
        data_root = d.get("data_root")
        # Keys nobody reads (llm-wiki's old verify settings, inputs_root of the frozen input sets removed on
        # 2026-11-14, another app's options) are accepted and ignored.
        self.sources = d.get("sources")
        reg = registry.get()
        for op in reg.options:                       # the apps' top-level keys
            try:
                value = op.check(d.get(op.key, op.default))
            except ValueError as e:
                raise ConfigError(op.key) from e
            setattr(self, op.attr or op.key, value)
        if not isinstance(self.sources, dict):
            raise ConfigError("sources")
        # SPEC 2: every path is absolute and exists, or the command is refused (exit 2). Tested on the config's own
        # strings (the sources stay strings): a Path would drop a trailing separator the OS refuses after a file name.
        for p in [self.sources.get(k) for k in reg.source_keys] + [data_root]:
            if not isinstance(p, str) or not os.path.isabs(p) or not os.path.exists(p):
                raise ConfigError("path missing or relative")
        self.data_root = Path(data_root)
        if not self.data_root.is_dir():
            raise ConfigError("path kind")
        # SPEC 10.1: private writes (ledger, state, .verify) stay under the ACL'd,
        # Search-excluded parent; checked here so every command refuses before it writes anything.
        # resolve() too: a junction inside the parent must not lead out of it.
        if not paths.inside(self.data_root, paths.DATA_PARENT):
            raise ConfigError("data root outside the data parent")
        parent = Path(paths.DATA_PARENT).resolve()
        raw = self.data_root / "raw"
        # The same folders llm-wiki's capture checked. <data_root>/index belongs to the search side, which guards it
        # itself (rawlayer.search.index.load_data_root): capture's acceptance must not depend on it.
        for p in [self.data_root, raw, self.data_root / "reports"] + \
                [raw / d for d in ("ledger", "manifests", ".state", ".work", ".verify")]:
            if p.exists(follow_symlinks=False) and not paths.inside(p.resolve(), parent):
                raise ConfigError("data root path leads outside the data parent")

    # ---- layout
    @property
    def raw(self):
        return self.data_root / "raw"

    @property
    def reports(self):
        return self.data_root / "reports"


class InputSet:
    """The resolved source locations: each installed app's inputs.resolve(cfg) names its attributes."""

    def __init__(self, cfg: Config):
        for app in registry.apps():
            for attr, path in app.inputs.resolve(cfg).items():
                if hasattr(self, attr):
                    raise registry.RegistryError("input attribute claimed twice")
                setattr(self, attr, path)
