# -*- coding: utf-8 -*-
"""Config loading and input-set resolution (SPEC 2, 16). Never derives a path from the environment.

The "sources" entries and top-level options a config must carry are the installed apps' (AppConfig.source_keys,
AppConfig.options); the paths of an input set are resolved by each app's inputs.py."""
import json
import os
from pathlib import Path

from .. import registry
from . import fsio, paths

INPUT_SETS = ("live", "T1", "T2")


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
        # inputs_root is read only by the frozen input sets T1/T2 (checked there). Keys nobody reads (llm-wiki's
        # old verify settings, another app's options) are accepted and ignored.
        inputs_root = d.get("inputs_root")
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
        if inputs_root is not None and not (isinstance(inputs_root, str) and os.path.isabs(inputs_root)):
            raise ConfigError("inputs_root relative")
        self.inputs_root = None if inputs_root is None else Path(inputs_root)
        # SPEC 10.1: private writes (ledger, state, export-state output under .verify) stay under the ACL'd,
        # Search-excluded parent; checked here so every command refuses before it writes anything.
        # resolve() too: a junction inside the parent must not lead out of it.
        if not paths.inside(self.data_root, paths.DATA_PARENT):
            raise ConfigError("data root outside the data parent")
        parent = Path(paths.DATA_PARENT).resolve()
        raw = self.data_root / "raw"
        # The same folders llm-wiki's capture checked. <data_root>/index belongs to the search side, which guards it
        # itself (rawlayer.search.common.load_data_root): capture's acceptance must not depend on it.
        for p in [self.data_root, raw, self.data_root / "reports"] + \
                [raw / d for d in ("ledger", "manifests", ".state", ".work", ".verify")]:
            if p.exists(follow_symlinks=False) and not paths.inside(p.resolve(), parent):
                raise ConfigError("data root path leads outside the data parent")

    def check_input_set(self, name):
        """Refuse an input set this config cannot serve, before any command writes (SPEC 10.1; llm-wiki refused
        a missing inputs_root at Config load). A frozen set (T1/T2) needs inputs_root to be an existing
        directory; live never reads it."""
        if name not in INPUT_SETS:
            raise ConfigError("input set")
        if name != "live" and (self.inputs_root is None or not Path(self.inputs_root).is_dir()):
            raise ConfigError("inputs_root missing")        # the frozen sets live under inputs_root

    # ---- layout
    @property
    def raw(self):
        return self.data_root / "raw"

    @property
    def reports(self):
        return self.data_root / "reports"


class InputSet:
    """Resolved source locations for one input set: `live` reads the sources where config.json points; a frozen
    set (T1/T2) reads the copies under <inputs_root>/<name> that freeze.json describes. Each installed app's
    inputs.resolve() names its attributes."""

    def __init__(self, cfg: Config, name: str, verify_static=True):
        cfg.check_input_set(name)              # a backstop: cli.main and run.capture check before they write
        self.name = name
        self.freeze = None
        self.snapshot_s = None
        frozen_root = None
        if name == "live":
            self.live_frozen = False
        else:
            frozen_root = Path(cfg.inputs_root, name)
            fz = frozen_root / "freeze.json"
            if not fz.exists():
                raise ConfigError("input set not frozen")
            with fz.open(encoding="utf-8") as fh:
                self.freeze = json.load(fh)
            self.live_frozen = True
            self.snapshot_s = _iso_to_s(self.freeze.get("finished_at"))
        for app in registry.apps():
            for attr, path in app.inputs.resolve(cfg, name, frozen_root).items():
                if hasattr(self, attr):
                    raise registry.RegistryError("input attribute claimed twice")
                setattr(self, attr, path)
        if frozen_root is not None and verify_static:
            self._verify_static(cfg)

    def _verify_static(self, cfg):
        """freeze.json "static": {label: {relpath: {sha256}}} -- every file still there with its hash; a closed
        root (a directory of room files) has no file the freeze did not list."""
        st = self.freeze.get("static") or {}
        roots = {}
        for app in registry.apps():
            roots.update(app.inputs.static_roots(self))
        for label, files in st.items():
            entry = roots.get(label)
            if entry is None:
                raise ConfigError("freeze static label")
            root, closed = entry
            for rel, meta in files.items():
                p = Path(root, *rel.split("/"))
                if not p.exists() or fsio.sha256_file(p) != meta["sha256"]:
                    raise ConfigError("static input drift")
            if closed:
                now = {c.name for c in Path(root).iterdir() if c.is_file()}
                if now - set(files):
                    raise ConfigError("static input drift")


def _iso_to_s(s):
    if not s:
        return None
    import datetime as _dt
    return int(_dt.datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=_dt.timezone.utc).timestamp())
