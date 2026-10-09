# -*- coding: utf-8 -*-
"""What a source reader hands the diff engine."""
from collections import Counter

OPS = ("put", "absent", "purged")
# The origin order and the statuses a manifest may hold come from the installed apps (rawlayer.registry).


class ContainerError(Exception):
    """The container cannot be trusted this run; `status` says how (error / unreadable / ...)."""

    def __init__(self, status, note):
        super().__init__(note)
        self.status = status
        self.note = note


class Obs:
    __slots__ = ("kind", "key", "native", "derived", "ts_ms", "src_asof_s")

    def __init__(self, kind, key, native, derived, ts_ms=None, src_asof_s=None):
        self.kind, self.key, self.native, self.derived = kind, key, native, derived
        self.ts_ms, self.src_asof_s = ts_ms, src_asof_s


class Container:
    def __init__(self, origin, relpath, sid, status="ok", asof_s=None):
        self.origin = origin
        self.relpath = relpath    # text, never a Path: srcmap.json holds it and canon.source_id hashes it
        self.sid = sid
        self.status = status
        self.asof_s = asof_s
        self.tables = None
        self.wal = None
        self.notes = Counter()
        self.iter_obs = None      # callable -> iterator of Obs; may raise ContainerError
        self.cleanup = None       # callable run after the container is processed

    def facts_base(self):
        f = {"status": self.status}
        if self.tables is not None:
            f["tables"] = dict(self.tables)
        if self.wal is not None:
            f["wal"] = dict(self.wal)
        return f
