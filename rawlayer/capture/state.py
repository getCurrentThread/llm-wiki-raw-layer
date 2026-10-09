# -*- coding: utf-8 -*-
"""state.sqlite: a cache of the fold. It is only ever changed by applying a committed manifest, so
`replay` (rebuild from the ledger alone) reproduces it exactly (SPEC 9.4, V4)."""
import os
import sqlite3

from .. import registry
from . import fsio
from .ledger import Ledger, LedgerCorrupt, manifest_ok
from .model import OPS

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS heads(
  origin TEXT NOT NULL, key TEXT NOT NULL, rev INTEGER NOT NULL, op TEXT NOT NULL,
  h TEXT, src TEXT, kind TEXT, pending INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(origin, key)) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS heads_src ON heads(origin, src);
"""
SIDECARS = ("-journal", "-wal", "-shm")      # a hot journal next to a new file would be rolled into it


def tables():
    """The core tables, then the installed origins' state_tables."""
    return registry.CORE_TABLES + tuple(t for t, _ddl in registry.state_tables())


class Head:
    __slots__ = ("rev", "op", "h", "src", "kind", "pending")

    def __init__(self, rev, op, h, src, kind, pending):
        self.rev, self.op, self.h, self.src, self.kind, self.pending = rev, op, h, src, kind, pending


def open_state(path):
    con = sqlite3.connect(path, isolation_level=None)  # explicit BEGIN/COMMIT only
    try:
        con.execute("PRAGMA journal_mode=DELETE")
        con.execute("PRAGMA synchronous=FULL")
        con.execute("PRAGMA temp_store=MEMORY")
        con.executescript(SCHEMA + "".join(ddl + "\n" for _t, ddl in registry.state_tables()))
    except BaseException:
        con.close()                 # a damaged file must not stay open (it is set aside next)
        raise
    return con


def head_seq(con):
    r = con.execute("SELECT v FROM meta WHERE k='head_seq'").fetchone()
    return int(r[0]) if r else 0


def heads_for_src(con, origin, src):
    return {k: Head(rev, op, h, s, kind, p) for k, rev, op, h, s, kind, p in con.execute(
        "SELECT key, rev, op, h, src, kind, pending FROM heads WHERE origin=? AND src=?", (origin, src))}


def heads_for_origin(con, origin):
    return {k: Head(rev, op, h, s, kind, p) for k, rev, op, h, s, kind, p in con.execute(
        "SELECT key, rev, op, h, src, kind, pending FROM heads WHERE origin=?", (origin,))}


def head(con, origin, key):
    r = con.execute("SELECT rev, op, h, src, kind, pending FROM heads WHERE origin=? AND key=?", (origin, key)).fetchone()
    return Head(*r) if r else None


def hist_for_origin(con, origin):
    out = {}
    for k, h in con.execute("SELECT key, h FROM hist WHERE origin=?", (origin,)):
        out.setdefault(k, set()).add(h)
    return out


def _record_ok(rec):
    """The fields apply_manifest binds, with the types SQLite stores: a record of another shape is a ledger defect
    (LedgerCorrupt), never a SQLite error that could be mistaken for a damaged state file."""
    return isinstance(rec, dict) and rec.get("op") in OPS \
        and all(isinstance(rec.get(k), str) for k in ("origin", "key", "kind")) \
        and all(isinstance(rec.get(k), int) and not isinstance(rec.get(k), bool) for k in ("rev", "seq")) \
        and all(rec.get(k) is None or isinstance(rec.get(k), str) for k in ("h", "src"))


def apply_manifest(con, ledger: Ledger, manifest):
    """Fold one committed manifest into state (records, then pending add/drop, then head_seq)."""
    seq = manifest["seq"]
    if head_seq(con) != seq - 1:
        raise LedgerCorrupt("state not at seq-1")
    cur = con.cursor()
    cur.execute("BEGIN")
    try:
        for rec in ledger.iter_records(manifest):
            if not _record_ok(rec):
                raise LedgerCorrupt("record shape")
            o, k, rev, op = rec["origin"], rec["key"], rec["rev"], rec["op"]
            if rec["seq"] != seq:
                raise LedgerCorrupt("record seq")
            r = cur.execute("SELECT rev FROM heads WHERE origin=? AND key=?", (o, k)).fetchone()
            if rev != (r[0] if r else 0) + 1:
                raise LedgerCorrupt("rev not contiguous")
            cur.execute("INSERT OR REPLACE INTO heads(origin,key,rev,op,h,src,kind,pending) VALUES(?,?,?,?,?,?,?,0)",
                        (o, k, rev, op, rec["h"], rec["src"], rec["kind"]))
            if op == "put":
                spec = registry.origin(o)
                if spec is not None:
                    spec.apply_put(cur, rec)        # the origin's state_tables
        for o, p in (manifest.get("origins") or {}).items():
            pend = p.get("pending") or {}
            for k in pend.get("add", []):
                cur.execute("UPDATE heads SET pending=? WHERE origin=? AND key=?", (seq, o, k))
            for k in pend.get("drop", []):
                cur.execute("UPDATE heads SET pending=0 WHERE origin=? AND key=?", (o, k))
        cur.execute("INSERT OR REPLACE INTO meta(k,v) VALUES('head_seq',?)", (str(seq),))
        cur.execute("COMMIT")
    except BaseException:
        cur.execute("ROLLBACK")
        raise


def catch_up(con, ledger: Ledger):
    have = head_seq(con)
    n = 0
    for seq, _run, path in ledger.manifests():
        if seq > have:
            man = ledger.read_manifest(path)
            if not manifest_ok(man):
                raise LedgerCorrupt("manifest")
            apply_manifest(con, ledger, man)
            n += 1
    return n


def set_aside(path, tag):
    """Move a damaged state file and its sidecars to <name>.bad-<tag><suffix> (kept as evidence)."""
    for suf in ("",) + SIDECARS:
        src = fsio.suffixed(path, suf)
        if src.exists():
            fsio.replace_retry(src, fsio.suffixed(path, f".bad-{tag}{suf}"))


def rebuild(ledger: Ledger, path):
    for suf in ("",) + SIDECARS:
        p = fsio.suffixed(path, suf)
        if p.exists():
            p.unlink()
    con = open_state(path)
    try:
        catch_up(con, ledger)
    except BaseException:
        con.close()                 # the caller cannot remove the file while it is open (V4 on a bad ledger)
        raise
    return con


def compare(con_a, path_b):
    """Rows in the symmetric difference per table (0 everywhere = identical)."""
    con_a.execute("ATTACH DATABASE ? AS other", (os.fspath(path_b),))   # SQLite binds text, not a Path
    try:
        out = {}
        for t in tables():
            n1 = con_a.execute(f"SELECT COUNT(*) FROM (SELECT * FROM main.{t} EXCEPT SELECT * FROM other.{t})").fetchone()[0]
            n2 = con_a.execute(f"SELECT COUNT(*) FROM (SELECT * FROM other.{t} EXCEPT SELECT * FROM main.{t})").fetchone()[0]
            out[t] = n1 + n2
        return out
    finally:
        con_a.execute("DETACH DATABASE other")
