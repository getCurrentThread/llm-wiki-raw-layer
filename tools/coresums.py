# -*- coding: utf-8 -*-
"""python -s -B tools/coresums.py [--check | --write] -- the core files against CORE_SHA256SUMS.

A developer tool, not a manage.py command: check (`manage.py check`) compares the core against the recorded
sums; this is what rewrites them after a deliberate core change. With no option, or --check, it compares (exit 0
when they match, 1 when not). Run from the project root."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rawlayer import conf, coreset  # noqa: E402


def main(argv):
    if argv not in ([], ["--check"], ["--write"]):
        print("usage: python -s -B tools/coresums.py [--check | --write]", file=sys.stderr)
        return 2
    if argv == ["--write"]:
        n = coreset.write(conf.PROJECT_DIR)
        print(f"coresums: wrote {coreset.SUMS} files={n}")
        return 0
    bad = coreset.check(conf.PROJECT_DIR)
    for kind, rel in bad:
        print(f"coresums: {kind} {rel}")
    print(f"coresums: {'ok' if not bad else 'DIFFERS'} files={len(coreset.files(conf.PROJECT_DIR))}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
