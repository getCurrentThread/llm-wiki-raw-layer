# -*- coding: utf-8 -*-
"""The core of a project: the files every project built on rawlayer carries identically (CORE.md), listed with
their sha256 in CORE_SHA256SUMS at the project root (`python -s -B tools/coresums.py` checks or rewrites it).
Line endings do not count (CRLF is read as LF), so a checkout with
autocrlf still matches. Standard library only, no project import."""
import hashlib
import os
from pathlib import Path

CORE = ("manage.py", "CORE.md", "rawlayer", "tests/__init__.py", "tests/core")
SUMS = "CORE_SHA256SUMS"
_SKIP_DIRS = {"__pycache__"}
_SKIP_EXT = (".pyc", ".pyo")


def files(project):
    """Relative paths ('/'-separated, sorted) of the core files present in `project`."""
    project = Path(project)
    out = []
    for entry in CORE:
        p = project.joinpath(*entry.split("/"))
        if p.is_file():
            out.append(entry)
        elif p.is_dir():
            # os.walk, not Path.walk: Path.walk lists a symlinked directory as a file, os.walk as a directory
            for d, subs, names in os.walk(p):
                subs[:] = sorted(s for s in subs if s not in _SKIP_DIRS)
                for n in names:
                    if not n.endswith(_SKIP_EXT):
                        out.append(Path(d, n).relative_to(project).as_posix())
    return sorted(out)


def digest(path):
    return hashlib.sha256(Path(path).read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def sums_text(project):
    return "".join(f"{digest(Path(project, *rel.split('/')))}  {rel}\n" for rel in files(project))


def read_sums(project):
    """{relpath: sha256} from the project's CORE_SHA256SUMS ({} when it is missing)."""
    p = Path(project, SUMS)
    if not p.is_file():
        return {}
    out = {}
    with p.open(encoding="ascii") as fh:
        for ln in fh:
            h, _, rel = ln.rstrip("\r\n").partition("  ")
            out[rel] = h
    return out


def check(project):
    """[(problem, relpath)] where the core files of `project` differ from its CORE_SHA256SUMS:
    missing (listed, not present), extra (present, not listed), changed (other content)."""
    listed = read_sums(project)
    present = files(project)
    bad = [("missing", rel) for rel in sorted(set(listed) - set(present))]
    bad += [("extra", rel) for rel in present if rel not in listed]
    bad += [("changed", rel) for rel in present
            if rel in listed and digest(Path(project, *rel.split("/"))) != listed[rel]]
    return bad


def write(project):
    """Rewrite the project's CORE_SHA256SUMS from its core files; returns how many it lists."""
    text = sums_text(project)
    Path(project, SUMS).write_text(text, encoding="ascii", newline="\n")
    return text.count("\n")
