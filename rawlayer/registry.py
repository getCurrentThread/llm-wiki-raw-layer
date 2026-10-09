# -*- coding: utf-8 -*-
"""The project's apps (Django-style): every folder next to manage.py that holds __init__.py and apps.py is an app,
found by discover() without importing anything. The folder name is the app's one name (its label, its package,
its index file, the <app> of every command). Each app has the same modules:

  apps.py     exactly one AppConfig subclass: label (= the folder name), source_keys (config "sources" entries the
              app reads), options (top-level config keys the app reads), optionally schedule (the scheduled task,
              rawlayer/schedule.py) and task_checks(cfg) (what makes a config unfit for the scheduled task)
  origins.py  ORIGINS: the app's Origin subclasses (rawlayer.capture.origins) in processing and commit order
  inputs.py   resolve(cfg, input_set, frozen_root) -> {InputSet attribute: Path} and
              static_roots(inputs) -> {freeze.json static label: (root, closed)}
  vocab.py    WORDS and PATTERNS: the strings the app's readers put in manifests and console lines (its origin
              names, kinds and statuses are registered from origins.py)
  search.py   optional: a rawlayer.search source module (SPEC, HELP, add_arguments, run)

One app owns one ledger, so a process works on ONE app: every command that opens a data root calls select(app)
before anything reads the registry, and the registry then holds that app alone (its origin order is the app's
ORIGINS). get() without a selection picks the only app of a project that has one (a project split out of a
multi-app checkout), and refuses otherwise. A Registry of several apps is built only to validate them together
(manage.py check). Everything here is looked up at call time and never bound at import, so tests can swap the
installed apps with use()."""
import importlib
import inspect
import os
import re
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, NamedTuple

from . import conf

CORE_STATUSES = ("ok", "skipped", "suspect", "unstable", "error", "missing")
CORE_TABLES = ("meta", "heads")
CORE_CONFIG_KEYS = ("v", "impl", "data_root", "inputs_root", "sources")
_LABEL = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
_ORIGIN = re.compile(r"^[a-z_]{1,40}$")      # a ledger path segment (ledger._SEG_PATH) and a public word
_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,39}$")
# folders next to manage.py that are never apps (the core's own, and the words commands use for "every app")
RESERVED = ("rawlayer", "config", "tests", "tools", "docs", "core", "all", "settings", "manage")


class RegistryError(Exception):
    """An installed app breaks the app contract."""


class Option(NamedTuple):
    """A top-level config.json key an app reads. `check(value)` returns the value to keep, or raises ValueError
    (the config is refused, exit 2). The value lands on Config as attribute `attr` (default: the key)."""
    key: str
    default: object
    check: Callable
    attr: str = None


class AppConfig:
    """Subclassed once in <app>/apps.py."""
    label = None
    source_keys = ()
    options = ()
    schedule = None             # {"task", "start", "every_hours", "limit_hours", "description"} (rawlayer/schedule.py)

    def __init__(self, name):
        self.name = name            # the package: the app's folder name (a dotted path for the core tests' toy apps)
        self.origins = ()
        self.inputs = None
        self.vocab = None
        self.search = None

    def __repr__(self):
        return f"<AppConfig {self.label}>"

    def task_checks(self, cfg):
        """Problems (short public strings) that make `cfg` unfit for the project's scheduled task, which runs collect
        as SYSTEM on the live input set. `manage.py schedule install` refuses while any app reports one."""
        return []

    def setup_defaults(self, cfg, note):
        """Fill the machine's own defaults into the config dict setup is about to write (an app may resolve its
        config.example.json placeholders itself: standard locations it knows, values it detects, its own outputs
        under the data root). `note` prints a setup line; only key names are printed, never the values. Whatever
        stays a <...> placeholder is named by setup after the write."""
        return None

    def setup_provision(self, cfg, note, restart_client=False, scan=False, root=None):
        """Provision what only the app can after setup wrote its config.json but before the config is validated
        (the inputs no command can read anywhere else: credentials and their derivations). `note` prints a setup
        line; `restart_client` allows restarting the app's client to rescan it, `scan` adds the app's
        memory-scan fallback and `root` names a key file to install. Returns attention strings (setup exits 3
        while any is there)."""
        return []


def _optional(module):
    try:
        return importlib.import_module(module)
    except ModuleNotFoundError as e:
        if e.name == module:
            return None
        raise


def _load(name):
    mod = importlib.import_module(name + ".apps")
    found = [v for v in vars(mod).values()
             if isinstance(v, type) and issubclass(v, AppConfig) and v is not AppConfig and v.__module__ == mod.__name__]
    if len(found) != 1:
        raise RegistryError(f"{name}.apps must define exactly one AppConfig subclass")
    app = found[0](name)
    app.origins = tuple(cls(app) for cls in importlib.import_module(name + ".origins").ORIGINS)
    app.inputs = importlib.import_module(name + ".inputs")
    app.vocab = importlib.import_module(name + ".vocab")
    app.search = _optional(name + ".search")
    return app


class Registry:
    def __init__(self, names):
        from .capture.origins import Origin    # noqa: PLC0415 -- the origins module imports the engine
        self.names = tuple(names)
        self.apps = tuple(_load(n) for n in self.names)
        self.origins = tuple(o for a in self.apps for o in a.origins)
        for o in self.origins:                  # before anything is derived from them
            if not isinstance(o, Origin) or not isinstance(o.name, str) or not _ORIGIN.fullmatch(o.name):
                raise RegistryError("origin name")
        self.by_name = {o.name: o for o in self.origins}
        self.origin_order = tuple(o.name for o in self.origins)
        extra = sorted({s for o in self.origins for s in o.statuses} - set(CORE_STATUSES))
        self.statuses = CORE_STATUSES + tuple(extra)
        tables = {}
        for o in self.origins:
            for t, ddl in o.state_tables:
                if t in CORE_TABLES or (t in tables and tables[t] != ddl):
                    raise RegistryError(f"state table {t} declared twice")
                tables.setdefault(t, ddl)
        self.state_tables = tuple(tables.items())
        self.source_keys = tuple(k for a in self.apps for k in a.source_keys)
        self.options = tuple(op for a in self.apps for op in a.options)
        self.search = {a.label: a.search for a in self.apps if a.search is not None}
        words = {conf.settings().IMPL_NAME}
        patterns = []
        for a in self.apps:
            for o in a.origins:
                words.add(o.name)
                words.update(o.kinds)
                words.update(o.statuses)
            words.update(a.vocab.WORDS)
            patterns.extend(a.vocab.PATTERNS)
        self.words = frozenset(words)
        self.patterns = tuple(patterns)
        self._check(Origin)

    def _check(self, Origin):
        labels = [a.label for a in self.apps]
        if len(set(labels)) != len(labels) or not all(isinstance(x, str) and _LABEL.fullmatch(x) for x in labels):
            raise RegistryError("app labels must be unique lower-case names")
        if len(set(self.origin_order)) != len(self.origin_order):
            raise RegistryError("origin installed twice")
        for o in self.origins:
            if not set(o.day_kinds) <= set(o.kinds):
                raise RegistryError(f"{o.name}: day_kinds outside kinds")
            if o.complete_status not in o.ok_statuses:
                raise RegistryError(f"{o.name}: complete_status is not an ok status")
            # the read -> diff -> facts pipeline is the core's: an app implements read() or read_once(), never run()
            if getattr(type(o).run, "__module__", None) != Origin.__module__:
                raise RegistryError(f"{o.name}: run() overridden outside the core")
        for k in self.source_keys:
            if not isinstance(k, str) or not _NAME.fullmatch(k):
                raise RegistryError("source key")
        if len(set(self.source_keys)) != len(self.source_keys):
            raise RegistryError("source key claimed by two apps")
        keys = [op.key for op in self.options]
        if len(set(keys)) != len(keys) or set(keys) & set(CORE_CONFIG_KEYS):
            raise RegistryError("option key")
        for a in self.apps:
            if a.search is not None and getattr(getattr(a.search, "SPEC", None), "name", None) != a.label:
                raise RegistryError(f"{a.label}: search.SPEC.name must be the app label")
            try:                                # task_checks(cfg): a method taking the config
                inspect.signature(a.task_checks).bind(None)
            except (TypeError, ValueError) as e:
                raise RegistryError(f"{a.label}: task_checks must be a method taking the config") from e
        for w in self.words:
            if not isinstance(w, str) or not _NAME.fullmatch(w):
                raise RegistryError("vocabulary word shape")

    def app(self, label):
        for a in self.apps:
            if a.label == label:
                return a
        return None


_current = None
_selected = None


def _install(reg):
    global _current
    from .capture import publicout      # noqa: PLC0415 -- publicout loads the registry lazily
    publicout.install(reg.words, reg.patterns)
    _current = reg


def discover():
    """The project's apps, by folder name in name order: every folder next to manage.py holding __init__.py and
    apps.py whose name is an app name, is not reserved and is not in settings.EXCLUDE_APPS. Nothing is imported,
    so an app that does not import (yet) never stops another app's commands."""
    excluded = set(getattr(conf.settings(), "EXCLUDE_APPS", ()) or ())
    project = Path(conf.PROJECT_DIR)
    try:
        names = [p.name for p in project.iterdir()]
    except OSError:
        return []
    out = []
    for d in sorted(names):
        p = project / d
        if _LABEL.fullmatch(d) and d not in RESERVED and d not in excluded \
                and (p / "__init__.py").is_file() and (p / "apps.py").is_file():
            out.append(d)
    return out


def on_path():
    """Put the project folder on sys.path (first) when it is not there: the apps are its top-level packages. manage.py
    has it there already (the script's folder); a test or a `python -c` child may not."""
    project = str(conf.PROJECT_DIR)             # sys.path holds strings
    if project not in sys.path and os.path.realpath(project) not in map(os.path.realpath, sys.path):
        sys.path.insert(0, project)


def app_registry(app):
    """A Registry of one discovered app, loaded but not installed. Refuses an app whose package is not the folder
    next to manage.py (another package of that name earlier on sys.path) or whose label is not its folder name."""
    if app not in discover():
        raise RegistryError(f"no app {app!r} in this project")
    on_path()
    reg = Registry([app])
    where = Path(getattr(sys.modules.get(app), "__file__", None) or "").resolve().parent
    if os.path.normcase(where) != os.path.normcase(Path(conf.PROJECT_DIR, app).resolve()):
        raise RegistryError(f"{app}: the package imported is not the folder next to manage.py")
    if reg.apps[0].label != app:
        raise RegistryError(f"{app}: AppConfig.label must be the folder name")
    return reg


def select(app):
    """Install the registry of `app` for this process (every command that opens a data root does this first).
    Selecting the selected app again is a no-op; another app is refused: one process works on one ledger."""
    global _selected
    if _selected is not None:
        if _selected != app:
            raise RegistryError(f"{_selected} is already selected in this process")
        if _current is not None and _current.names == (app,):
            return _current
    reg = app_registry(app)
    _install(reg)
    _selected = app
    return reg


def selected():
    """The app select() installed in this process, or None."""
    return _selected


def get():
    """The registry of the selected app. With nothing selected, the project's only app (a project split out of a
    multi-app checkout); with several, RegistryError: name the app."""
    if _current is None:
        found = discover()
        if len(found) != 1:
            raise RegistryError("no app in this project" if not found else
                                f"several apps ({', '.join(found)}): name one (manage.py <command> <app> ...)")
        select(found[0])
    return _current


@contextmanager
def use(names):
    """Tests: run a block with another set of installed apps (e.g. tests/core/toyapp)."""
    global _current
    prev = _current
    _install(Registry(names))
    try:
        yield _current
    finally:
        if prev is None:
            from .capture import publicout  # noqa: PLC0415
            publicout.uninstall()
            _current = None
        else:
            _install(prev)


def apps():
    return get().apps


def origins():
    return get().origins


def origin(name):
    return get().by_name.get(name)


def origin_order():
    return get().origin_order


def statuses():
    return get().statuses


def state_tables():
    return get().state_tables


def search_modules():
    return get().search
