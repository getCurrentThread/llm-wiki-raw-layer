# -*- coding: utf-8 -*-
import argparse
import subprocess
import sys
from pathlib import Path

from ... import conf, registry
from ..base import BaseCommand


def suites(names):
    """[(name, start folder)] for `core` and the named apps (all of them when none is named); each folder a Path
    relative to the project."""
    apps = registry.discover()
    names = names or ["core"] + apps
    out = []
    for n in names:
        if n == "core":
            out.append(("core", Path("tests")))
        elif n in apps:
            out.append((n, Path(n) / "tests"))
        else:
            raise ValueError(n)
    return out


class Command(BaseCommand):
    help = "run the core's tests and each app's, one process per suite (an app's tests select that app)"
    args = "[core|<app> ...] [-k PATTERN] [-v] [-f]"

    def run(self, app, argv):
        ap = argparse.ArgumentParser(prog="manage.py test")
        ap.add_argument("suites", nargs="*")
        ap.add_argument("-k", dest="patterns", action="append", default=[])
        ap.add_argument("-v", "--verbose", action="store_true")
        ap.add_argument("-f", "--failfast", action="store_true")
        a = ap.parse_args(argv)
        try:
            todo = suites(a.suites)
        except ValueError as e:
            print(f"manage.py test: no suite {e} (core, or one of: {', '.join(registry.discover())})",
                  file=sys.stderr)
            return 2
        extra = [x for p in a.patterns for x in ("-k", p)] + (["-v"] if a.verbose else []) + \
            (["-f"] if a.failfast else [])
        results = []
        project = Path(conf.PROJECT_DIR)
        for name, start in todo:
            if not (project / start).is_dir():
                results.append((name, "no tests"))
                continue
            print(f"test: == {name}", flush=True)
            p = subprocess.run([sys.executable, "-s", "-B", "-m", "unittest", "discover", "-s", str(start), "-t", ".",
                                *extra], cwd=project)
            results.append((name, "ok" if p.returncode == 0 else f"FAILED (rc={p.returncode})"))
        print("test: " + "; ".join(f"{n} {r}" for n, r in results))
        return 0 if all(r in ("ok", "no tests") for _n, r in results) else 1
