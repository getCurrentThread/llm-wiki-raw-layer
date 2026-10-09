# -*- coding: utf-8 -*-
"""The only writer of the capture pipeline's public outputs (SPEC 11): manifests, log.md and console lines.

Allowed leaves: int, bool, None, finite float, and strings that fully match one of the patterns
below. Bare words must be code literals registered with words(); a string that came from data is
never registered, so it fails here instead of leaking. Prose goes through lit(), which is only ever
called on string literals in this package. The rules are those the existing ledger's manifests were
written under, so a manifest this code writes is accepted (or refused) exactly as before.

The words and patterns of the installed apps (their origin names, kinds, statuses and vocab.py) are added by the
registry (install()); the first check loads it.
"""
import json
import math
import os
import re
from pathlib import Path

_VOCAB = set()
_APP_WORDS = frozenset()
_APP_PATTERNS = ()
_installed = False


def words(*ws):
    for w in ws:
        if not _WORD.fullmatch(w):
            raise ValueError("vocabulary word shape")
        _VOCAB.add(w)


_WORD = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,39}$")
_PATTERNS = [re.compile(p) for p in (
    r"^[0-9]{13}-[0-9a-f]{4}$",                                   # run id
    r"^[0-9a-f]{8,64}$",                                          # hex / hash prefix / source_id / git sha
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$",                              # date
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$",  # UTC datetime
    r"^[0-9]+(\.[0-9]+){0,3}$",                                   # version
    r"^[0-9]{1,20}$",                                             # decimal
    r"^ledger/[a-z_]+/[0-9]{4}/seg-[0-9]{8}-[a-z_]+-[0-9]{3}\.jsonl\.gz$",
)]


class LeakError(ValueError):
    pass


def install(app_words, app_patterns):
    """Registry: the installed apps' words and patterns (replacing the previous set)."""
    global _APP_WORDS, _APP_PATTERNS, _installed
    for w in app_words:
        if not _WORD.fullmatch(w):
            raise ValueError("vocabulary word shape")
    _APP_WORDS = frozenset(app_words)
    _APP_PATTERNS = tuple(re.compile(p) for p in app_patterns)
    _installed = True


def uninstall():
    global _APP_WORDS, _APP_PATTERNS, _installed
    _APP_WORDS, _APP_PATTERNS, _installed = frozenset(), (), False


def ok_str(s: str) -> bool:
    if not _installed:
        from .. import registry     # noqa: PLC0415 -- the registry imports the apps, which import this module
        registry.get()
    if s in _VOCAB or s in _APP_WORDS:
        return True
    # fullmatch: '$' alone lets a trailing newline through
    return any(p.fullmatch(s) for p in _PATTERNS) or any(p.fullmatch(s) for p in _APP_PATTERNS)


def check(obj, path="$"):
    if obj is None or isinstance(obj, bool) or isinstance(obj, int):
        return
    if isinstance(obj, float):
        if not math.isfinite(obj):
            raise LeakError("non-finite float at " + path)
        return
    if isinstance(obj, str):
        if not ok_str(obj):
            raise LeakError("unregistered string at " + path)
        return
    if isinstance(obj, dict):
        for k, v in obj.items():
            if not isinstance(k, str) or not ok_str(k):
                raise LeakError("unregistered key at " + path)
            check(v, path + "." + k)
        return
    if isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            check(v, f"{path}[{i}]")
        return
    raise LeakError("unsupported type at " + path)


class Lit(str):
    """Prose written as a literal in this package's source."""


def lit(s: str) -> Lit:
    if not s.isascii():
        raise LeakError("non-ascii literal")
    return Lit(s)


def render_json(obj, indent=1) -> bytes:
    check(obj)
    return json.dumps(obj, sort_keys=True, indent=indent, ensure_ascii=True, allow_nan=False).encode("ascii")


def cell(v) -> str:
    if isinstance(v, Lit):
        return str(v)
    check(v)
    if v is None:
        return "-"
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, float):
        return f"{v:.4g}"
    return str(v)


def _kw(k):
    """A keyword argument name of say()/append_line(): an identifier written in this package's source
    (tests/core/test_literals.py checks no call site passes **mapping)."""
    if not _WORD.fullmatch(k):
        raise LeakError("keyword shape")
    return k


def say(*parts, **kv):
    """Console line: literal prose + validated values."""
    out = [cell(x) for x in parts] + [f"{_kw(k)}={cell(v)}" for k, v in kv.items()]
    line = " ".join(out)
    print(line, flush=True)


def append_line(path, *parts, **kv):
    out = [cell(x) for x in parts] + [f"{_kw(k)}={cell(v)}" for k, v in kv.items()]
    data = (" ".join(out) + "\n").encode("ascii")
    with Path(path).open("ab") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())


# Vocabulary of the core (the apps add theirs through the registry).
words(
    # ops
    "put", "absent", "purged", "capture", "purge",
    # core statuses, the day of an undated record, input sets
    "ok", "skipped", "suspect", "unstable", "error", "missing", "none", "live", "T1", "T2",
    # manifest fields
    "v", "type", "seq", "run", "prev_sha256", "started_at", "finished_at", "input_set", "impl", "name",
    "git_head", "dirty", "python", "pycryptodome", "segments", "path", "origin", "sha256", "bytes", "records",
    "origins", "status", "counts", "notes", "pending", "add", "drop", "containers", "kinds", "n", "digest",
    "days", "tables", "versions", "wal", "state",
    # notes of the engine and the core mixins
    "value_bad", "dup_key", "denied", "vanished", "room_gone", "read_error", "disk_floor", "shrink_guard",
    "key_unprintable",
)
