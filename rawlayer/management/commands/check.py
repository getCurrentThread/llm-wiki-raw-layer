# -*- coding: utf-8 -*-
import argparse
import os
import sys
import traceback
from pathlib import Path

from ... import conf, coreset, registry, schedule
from ..base import BaseCommand, default_config


def load_error(e):
    """A short public account of why an app does not load: the exception type, and the module or line at fault."""
    if isinstance(e, ModuleNotFoundError) and e.name:
        return f"{type(e).__name__} {e.name}"
    if isinstance(e, SyntaxError) and e.filename:
        return f"{type(e).__name__} {Path(e.filename).name}:{e.lineno}"
    return type(e).__name__


def describe(app, show_traceback=False):
    """One line about a discovered app (loaded on its own, never selected), and whether it loads, keeps the app
    contract and has a well-formed schedule."""
    try:
        reg = registry.app_registry(app)
    except registry.RegistryError as e:
        return f"{app}: app contract broken ({e})", False
    except Exception as e:  # noqa: BLE001 -- an app that does not import (yet)
        if show_traceback:
            traceback.print_exc()
        return f"{app}: does not load (error {load_error(e)}; manage.py check {app} --traceback)", False
    a = reg.apps[0]
    try:
        s = schedule.task_settings(app)
        when = f"{s.task} {schedule.describe(s)}" if s else "none"
        ok = True
    except schedule.ScheduleError as e:
        when, ok = f"malformed ({e})", False
    return (f"{app}: origins={','.join(o.name for o in a.origins) or '-'} search={'yes' if a.search else 'no'} "
            f"schedule={when}"), ok


def fire_minutes(s):
    """The minutes of the day (0..1439) at which schedule `s` starts its task."""
    h, m = map(int, s.start.split(":"))
    return {(h * 60 + m + k * s.every_hours * 60) % 1440 for k in range(24 // s.every_hours or 1)}


class Check:
    def __init__(self, show_traceback=False):
        self.errors = 0         # rc 1
        self.refused = 0        # rc 2 (the config)
        self.tb = show_traceback

    def error(self, msg):
        self.errors += 1
        print(f"check: {msg}", file=sys.stderr)

    def note(self, msg):
        print(f"note: {msg}")

    def app(self, app):
        """The app on its own: it loads, keeps the app contract and its schedule is well formed. True when it
        loads."""
        line, ok = describe(app, self.tb)
        print(line)
        if not ok:
            self.errors += 1
        return ok

    def together(self, apps):
        """What two apps of one project must not share: origin names (the ledger guard tells apps apart by them),
        source keys, options and state tables (one Registry of them all keeps them mergeable), and task names."""
        if len(apps) > 1:
            try:
                registry.Registry([registry.app_registry(a).names[0] for a in apps])  # the packages, each checked
                print(f"apps: {len(apps)} apps, no collision")
            except registry.RegistryError as e:
                self.error(f"apps collide: {e}")
        found = []
        for app, s, problem in schedule.scheduled(apps):
            if problem is None and s is not None:
                found.append((app, s))
        tasks = {}
        for app, s in found:
            tasks.setdefault(s.task.lower(), []).append(app)
        for apps_of in tasks.values():
            if len(apps_of) > 1:
                self.error(f"schedule: {' and '.join(apps_of)} register the same task")
        for i, (a1, s1) in enumerate(found):
            for a2, s2 in found[i + 1:]:
                same = sorted(fire_minutes(s1) & fire_minutes(s2))
                if same:
                    self.note(f"schedule: {a1} and {a2} start at the same minute "
                              f"({', '.join(f'{m // 60:02d}:{m % 60:02d}' for m in same)})")

    def settings(self):
        st = conf.settings()
        if not isinstance(getattr(st, "IMPL_NAME", None), str) or not st.IMPL_NAME:
            self.error("settings.IMPL_NAME is not set")
        parent = getattr(st, "DATA_PARENT", None)
        if not isinstance(parent, (str, os.PathLike)) or not Path(parent).is_absolute():
            self.error("settings.DATA_PARENT is not an absolute path")
        base = getattr(st, "BASE_DIR", None)
        # the real paths compared as Windows compares them: normcase of each, not Path equality (str.lower())
        if not isinstance(base, (str, os.PathLike)) or \
                os.path.normcase(Path(base).resolve()) != os.path.normcase(Path(conf.PROJECT_DIR).resolve()):
            self.error("settings.BASE_DIR is not the project directory (the folder that holds manage.py)")
        here = set(registry.discover())
        for name in getattr(st, "EXCLUDE_APPS", ()) or ():
            if name not in here and (Path(conf.PROJECT_DIR) / str(name) / "apps.py").is_file():
                print(f"settings: {name} excluded")

    def core(self):
        bad = coreset.check(conf.PROJECT_DIR)
        if bad:
            kinds = sorted({k for k, _r in bad})
            self.error(f"core: {len(bad)} files differ from {coreset.SUMS} ({', '.join(kinds)}; "
                       f"python -s -B tools/coresums.py lists them, --write records a deliberate change)")
        else:
            print(f"core: ok files={len(coreset.files(conf.PROJECT_DIR))}")

    def config(self, app, path, given, ledger=True):
        """The app's config: it loads, its data root's ledger is the app's, the folded state matches that ledger
        (replay --check), and whether the scheduled task would take it (a note only: a development config need
        not be fit for the task)."""
        from ...capture import ledger  # noqa: PLC0415
        from ...capture.config import Config, ConfigError  # noqa: PLC0415
        if not given and not Path(path).is_file():
            print(f"config: none at <DATA_PARENT>\\{app}\\config.json (not checked)")
            return
        try:
            registry.select(app)
            cfg = Config(path)
        except ConfigError as e:
            self.refused += 1
            print(f"check: config refused ({e})", file=sys.stderr)
            return
        try:
            foreign = ledger.foreign_origins(cfg.raw)
        except Exception as e:  # noqa: BLE001
            self.error(f"config: the ledger could not be read (error {type(e).__name__})")
            return
        if foreign:
            self.refused += 1
            print("check: config refused (the data root's ledger belongs to another app)", file=sys.stderr)
            return
        print("config: ok")
        if ledger:
            self.ledger(path)
        try:
            s = schedule.task_settings(app)
        except schedule.ScheduleError:
            return                      # reported with the app
        if s:
            for p in schedule.task_problems(cfg, registry.apps()):
                self.note(f"schedule install would refuse this config: {p}")

    def ledger(self, path, rebuild=False, export=None):
        """The folded state against the ledger: replay --check folded in (the old replay command), a full replay
        with rebuild=True, and export-state's record dump to `export` (the old export-state command). A busy
        ledger (a capture is running) is a note, not a problem: there is nothing to verify against yet."""
        from ...capture import cli, run  # noqa: PLC0415
        argv = ["replay"] + ([] if rebuild else ["--check"]) + ["--config", str(path)]
        rc = cli.main(argv)
        if rc == run.EXIT_BUSY:
            self.note("ledger busy (a capture holds it); the folded state was not verified")
        elif rc != 0:
            self.errors += 1
        if export:
            rc = cli.main(["export-state", "--out", str(export), "--config", str(path)])
            if rc != 0:
                self.errors += 1

    def rc(self):
        return schedule.worst(1 if self.errors else 0, 2 if self.refused else 0)


class Command(BaseCommand):
    help = ("the apps keep the app contract and keep clear of each other (origins, source keys, options, state "
            "tables, task names), and the core matches CORE_SHA256SUMS; with <app>: that app, its "
            "config, and the ledger against the folded state")
    args = "[<app>] [--config CFG] [--export-state PATH] [--rebuild-state] [--traceback]"

    def run(self, app, argv):
        ap = argparse.ArgumentParser(prog="manage.py check")
        ap.add_argument("app", nargs="?")
        ap.add_argument("--config", help="the app's config (default: <DATA_PARENT>\\<app>\\config.json, if any)")
        ap.add_argument("--export-state", default=None, metavar="PATH",
                        help="also write every record's (origin, key, rev, op, h) to this file")
        ap.add_argument("--rebuild-state", action="store_true",
                        help="rebuild the folded state from the ledger instead of only comparing")
        ap.add_argument("--traceback", action="store_true", help="the traceback of an app that does not load")
        a = ap.parse_args(argv)
        apps = registry.discover()
        if a.app is not None and a.app not in apps:
            print(f"manage.py check: no app {a.app!r} in this project (apps: {', '.join(apps) or 'none'})",
                  file=sys.stderr)
            return 2
        if a.app is None and a.config is not None:
            if len(apps) != 1:
                print("manage.py check: --config belongs to one app: manage.py check <app> --config CFG",
                      file=sys.stderr)
                return 2
            a.app = apps[0]
        if (a.export_state or a.rebuild_state) and a.app is None:
            print("manage.py check: --export-state/--rebuild-state belong to one app: manage.py check <app> ...",
                  file=sys.stderr)
            return 2
        c = Check(a.traceback)
        c.settings()
        if a.app is not None:
            if c.app(a.app):
                c.config(a.app, a.config or default_config(a.app), a.config is not None,
                         ledger=not (a.export_state or a.rebuild_state))
                if a.export_state or a.rebuild_state:
                    c.ledger(a.config or default_config(a.app), rebuild=a.rebuild_state, export=a.export_state)
        else:
            if not apps:
                print("apps: none")
            loaded = [x for x in apps if c.app(x)]
            if len(loaded) == len(apps):
                c.together(apps)
            else:
                print("apps: not checked together while one does not load")
        c.core()
        rc = c.rc()
        print(f"check: {'ok' if rc == 0 else 'problems'} errors={c.errors} refused={c.refused}")
        return rc
