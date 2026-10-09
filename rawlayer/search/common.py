# -*- coding: utf-8 -*-
"""Shared machinery of the raw-search indexes: config, ledger reading, the index driver (full rebuild and
incremental catch-up), version gate, locking, time and rendering helpers.

Each app with a search.py (<label>/search.py) owns one index file <data_root>/index/<name>.sqlite and plugs
into this module with a SourceIndex subclass (module attribute SPEC) plus `add_arguments(parser)` and
`run(args) -> exit code` for its search subcommand (cli.py wires them up).

THE INDEX IS A PURE FUNCTION OF THE LEDGER
------------------------------------------
The index is built only from committed ledger records (<data_root>/raw, written by `capture`). Records are
applied in ledger order (manifest seq, then the manifest's segment order, then line order), by the same code
for a full rebuild and for an incremental catch-up, so catching up manifest by manifest and rebuilding from
scratch give the same rows. Nothing else is read: no source database, no config beyond `data_root`.

* Committed only. A manifest counts once its file exists and every segment it lists exists under its final
  name. A segment still named *.part means capture is between COMMIT and finalising (SPEC 9.1 steps 7-8, or a
  crash there awaiting recovery): the catch-up stops before that manifest, keeps the watermark and reports
  `behind` (exit 0). A manifest that cannot be read, or a segment that is missing or does not decompress, is a
  ledger defect: the catch-up stops before it and the command exits 1. Manifests are taken in an unbroken run
  from seq 1; a gap ends the run (capture itself refuses such a ledger).
* Watermark. meta stores head_seq plus the sha256 and run id of the manifest file at head_seq. Every update
  re-checks them; a missing file or a different hash/run means the ledger was reset or re-created (genesis
  redone), and the index is rebuilt from scratch. A new manifest whose type is not 'capture' (a future
  'purge', SPEC 12, rewrites old segments) also forces a full rebuild, as does a catch-up larger than the
  source's full_rebuild_records.
* Version gate. meta stores schema_version, tokenizer (textindex.TOKENIZER_VERSION) and extractor_version.
  `index` rebuilds an index whose stamps differ; a search refuses it (exit 2, "run manage.py collect
  --rebuild-index").
* Crash safety. A full rebuild writes <name>.sqlite.new, checks it (FTS5 'integrity-check' against the content
  table, PRAGMA quick_check) and only then swaps it in with os.replace (retried, Windows). An incremental
  catch-up applies one manifest per transaction together with its watermark (journal_mode=DELETE), so a crash
  leaves the index at the previous manifest. Before any rebuild or swap the live index is opened read-write
  once, which rolls back a hot journal left by a crashed update.
* One writer. <data_root>/index/<name>.lock is locked without blocking (msvcrt on Windows, flock elsewhere); a
  second writer gets exit 75. Searches open the index with mode=ro + PRAGMA query_only and never write.

SOURCE PLUG-IN CONTRACT (see SourceIndex)
-----------------------------------------
  name, origins, schema_version, extractor_version, fts_tables, full_rebuild_records
  create_tables(con)      every table and the FTS5 virtual tables; no FTS triggers
  create_triggers(con)    FTS maintenance triggers (installed after a full build, used by catch-ups); runs
                           inside the driver's transaction, so one con.execute per statement -- never
                           con.executescript, which would COMMIT it
  applier(con, full)      -> object with record(rec, seq), end_manifest(seq), finish()
                           record: one ledger record of `origins` (any kind); must be total -- odd values are
                           counted in the counts table (bump_counts), never raised
                           end_manifest: incremental mode derives the rows the manifest touched; both modes
                           persist counters here
                           finish: full mode derives every row (called once, after the last manifest)
  stats(con)              -> {name: int} for reports
The driver owns transactions, the meta and counts tables, the watermark and the swap.
"""
import argparse
import datetime as _dt
import gzip
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
import unicodedata
import zlib
from pathlib import Path
from typing import NamedTuple

from rawlayer.capture import fsio, ledger, paths

from . import textindex as ti

EXIT_OK, EXIT_ERR, EXIT_USAGE, EXIT_BUSY = 0, 1, 2, 75
COMMON_SCHEMA = "1"                 # the meta / counts tables below and the meta keys the driver writes
DEFAULT_TZ = "+09:00"
_MAN_RE = re.compile(r"^([0-9]{8})-([0-9]{13}-[0-9a-f]{4})\.json$")
_SEG_RE = re.compile(r"^ledger/([a-z_]+)/[0-9]{4}/seg-[0-9]{8}-([a-z_]+)-[0-9]{3}\.jsonl\.gz$")

META_DDL = """
CREATE TABLE meta(k TEXT PRIMARY KEY, v TEXT) WITHOUT ROWID;
CREATE TABLE counts(k TEXT PRIMARY KEY, n INTEGER NOT NULL) WITHOUT ROWID;
"""


class UsageError(Exception):
    """Bad arguments, bad query, missing or outdated index, unusable config -> exit 2."""


class Busy(Exception):
    """Another process holds the index writer lock -> exit 75."""


class LedgerDefect(Exception):
    """A manifest or segment that a committed ledger should have cannot be read -> exit 1."""


# ------------------------------------------------------------------------------------------------ config

def load_data_root(config_path):
    """data_root, as a Path, from a capture config file; nothing else in it is read. It must be an existing
    absolute directory inside the protected data parent (capture.paths.DATA_PARENT, the ACL'd and Search-excluded
    folder), and <data_root>/index must not lead out of it through a junction: the index holds plaintext."""
    try:                        # the path as given, as Config opens it: Path() would drop a trailing separator
        with open(config_path, encoding="utf-8") as fh:
            d = json.load(fh)
    except (OSError, ValueError):
        raise UsageError("config file unreadable") from None
    dr = d.get("data_root") if isinstance(d, dict) else None
    root = Path(dr) if isinstance(dr, str) else None
    # is_absolute() before is_dir(): Path("") is ".", an existing directory
    if root is None or not root.is_absolute() or not root.is_dir():
        raise UsageError("config: data_root must be an existing absolute directory")
    parent = Path(paths.DATA_PARENT).resolve()
    if not paths.inside(root.resolve(), parent):
        raise UsageError("config: data_root is outside the protected data parent")
    idx = root / "index"
    if idx.exists(follow_symlinks=False) and not paths.inside(idx.resolve(), parent):
        raise UsageError("data_root\\index leads outside the protected data parent")
    try:                        # one app, one ledger: never index or search another app's data root
        foreign = ledger.foreign_origins(root / "raw")
    except (OSError, ledger.LedgerCorrupt):
        foreign = []            # what cannot be read is the index's to report (LedgerDefect)
    if foreign:
        raise UsageError("config: the data root's ledger belongs to another app")
    return root


def index_dir(data_root):
    return Path(data_root) / "index"


def index_path(data_root, spec):
    return index_dir(data_root) / (spec.name + ".sqlite")


def _plus(path, tail):
    """`path` with `tail` appended to its last part: <x>.sqlite -> <x>.sqlite.new, <x>.sqlite-journal."""
    return path.with_name(path.name + tail)


# ------------------------------------------------------------------------------------------------ lock

def _lock(fd):
    if os.name == "nt":
        import msvcrt  # noqa: PLC0415
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    else:
        import fcntl  # noqa: PLC0415
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(fd):
    if os.name == "nt":
        import msvcrt  # noqa: PLC0415
        os.lseek(fd, 0, 0)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl  # noqa: PLC0415
        fcntl.flock(fd, fcntl.LOCK_UN)


class IndexLock:
    """Writer lock <data_root>/index/<name>.lock (non-blocking: msvcrt on Windows, flock elsewhere -- both per
    open file, so a second IndexLock in the same process is Busy too; released by the OS if we die)."""

    def __init__(self, idir, name):
        idir = Path(idir)
        idir.mkdir(parents=True, exist_ok=True)
        self.fd = os.open(idir / (name + ".lock"), os.O_RDWR | os.O_CREAT)
        try:
            _lock(self.fd)
        except OSError:
            os.close(self.fd)
            raise Busy() from None

    def release(self):
        if self.fd is None:
            return
        try:
            _unlock(self.fd)
        except OSError:
            pass
        os.close(self.fd)
        self.fd = None

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.release()


# ------------------------------------------------------------------------------------------------ ledger

class ManifestRef(NamedTuple):
    seq: int
    run: str
    path: Path


class LedgerView:
    """Read-only view of <data_root>/raw for the indexers (never takes capture's run lock: manifests are
    replaced atomically and segments are final once renamed)."""

    def __init__(self, data_root):
        self.raw = Path(data_root) / "raw"
        self.man_dir = self.raw / "manifests"

    def manifests(self):
        """Manifest files in an unbroken run from seq 1 (a gap or a duplicated seq ends the run)."""
        try:
            names = [p.name for p in self.man_dir.iterdir()]
        except OSError:
            return []
        found = []
        for n in names:
            m = _MAN_RE.fullmatch(n)
            if m:
                found.append(ManifestRef(int(m.group(1)), m.group(2), self.man_dir / n))
        found.sort()        # (seq, run) is unique per name, so the path never takes part in the order
        out = []
        for ref in found:
            if ref.seq != len(out) + 1:
                break
            out.append(ref)
        return out

    @staticmethod
    def file_sha(ref):
        try:
            return hashlib.sha256(ref.path.read_bytes()).hexdigest()
        except OSError:
            return None

    def load(self, ref):
        """(manifest dict, sha256 of its bytes). Raises LedgerDefect when it cannot be read or lacks the fields
        the index needs (seq, run, segments with a ledger path and their origin)."""
        try:
            data = ref.path.read_bytes()
            man = json.loads(data)
        except (OSError, ValueError):
            raise LedgerDefect(f"manifest {ref.seq} unreadable") from None
        if not isinstance(man, dict) or man.get("seq") != ref.seq or man.get("run") != ref.run \
                or not isinstance(man.get("segments"), list):
            raise LedgerDefect(f"manifest {ref.seq} malformed")
        for s in man["segments"]:
            g = _SEG_RE.fullmatch(s.get("path", "")) if isinstance(s, dict) and isinstance(s.get("path"), str) else None
            if g is None or not g.group(1) == g.group(2) == s.get("origin"):
                raise LedgerDefect(f"manifest {ref.seq} malformed segment entry")
        return man, hashlib.sha256(data).hexdigest()

    def seg_path(self, seg):
        return self.raw.joinpath(*seg["path"].split("/"))

    def commit_state(self, man):
        """'ok' when every segment is final; 'mid_commit' when one is still *.part; 'missing' otherwise.
        *.part is looked for first: capture only ever renames *.part to the final name, so a rename that lands
        between the two checks is seen as one or the other, never as neither (a false 'missing' defect)."""
        state = "ok"
        for s in man["segments"]:
            p = self.seg_path(s)
            if _plus(p, ".part").exists():
                state = "mid_commit"
                continue
            if p.exists():
                continue
            return "missing"
        return state

    def records(self, man, origins):
        """Records of the manifest's segments whose origin is in `origins`, in ledger order."""
        for s in man["segments"]:
            if s["origin"] not in origins:
                continue
            try:
                with gzip.open(self.seg_path(s), "rb") as fh:
                    for line in fh:
                        yield json.loads(line)
            except (OSError, EOFError, zlib.error, ValueError):
                raise LedgerDefect(f"segment of manifest {man['seq']} unreadable") from None

    @staticmethod
    def pending_records(man, origins):
        return sum(s["records"] for s in man["segments"]
                   if s["origin"] in origins and isinstance(s.get("records"), int) and not isinstance(s["records"], bool))


# ------------------------------------------------------------------------------------------------ sources

class SourceIndex:
    """Base of a source plug-in (see the module docstring)."""
    name = ""
    origins = ()
    schema_version = ""
    extractor_version = ""
    fts_tables = ()
    full_rebuild_records = 200_000          # a catch-up larger than this rebuilds instead (bulk FTS build)

    def stamp(self):
        return {"source": self.name, "schema_version": f"{COMMON_SCHEMA}.{self.schema_version}",
                "tokenizer": ti.TOKENIZER_VERSION, "extractor_version": self.extractor_version}

    def create_tables(self, con):
        raise NotImplementedError

    def create_triggers(self, con):
        raise NotImplementedError

    def applier(self, con, full):
        raise NotImplementedError

    def stats(self, con):
        return {}


def is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def clean(s):
    """A str SQLite can store (lone surrogates from a JSON \\ud800 escape become U+FFFD)."""
    try:
        s.encode("utf-8")
        return s
    except UnicodeEncodeError:
        return s.encode("utf-8", "surrogatepass").decode("utf-8", "replace")


def fts_triggers(table, fts):
    """The three FTS maintenance triggers of a content table with an ng shadow column (SourceIndex
    create_triggers: one statement per con.execute, never executescript -- see the module docstring)."""
    return (
        f"CREATE TRIGGER {table}_ai AFTER INSERT ON {table} BEGIN\n"
        f"  INSERT INTO {fts}(rowid, ng) VALUES (new.id, new.ng);\nEND;",
        f"CREATE TRIGGER {table}_ad AFTER DELETE ON {table} BEGIN\n"
        f"  INSERT INTO {fts}({fts}, rowid, ng) VALUES ('delete', old.id, old.ng);\nEND;",
        f"CREATE TRIGGER {table}_au AFTER UPDATE OF ng ON {table} BEGIN\n"
        f"  INSERT INTO {fts}({fts}, rowid, ng) VALUES ('delete', old.id, old.ng);\n"
        f"  INSERT INTO {fts}(rowid, ng) VALUES (new.id, new.ng);\nEND;",
    )


def create_triggers(con, table, fts):
    for stmt in fts_triggers(table, fts):
        con.execute(stmt)


def bump_counts(con, counter):
    """Add a Counter of event counts (bad values, skipped kinds, ...) to the counts table."""
    for k, n in sorted(counter.items()):
        if n:
            con.execute("INSERT INTO counts(k, n) VALUES(?, ?) ON CONFLICT(k) DO UPDATE SET n = n + excluded.n",
                        (k, n))


def read_counts(con):
    return dict(con.execute("SELECT k, n FROM counts ORDER BY k").fetchall())


def _set_meta(con, **kv):
    for k, v in kv.items():
        con.execute("INSERT OR REPLACE INTO meta(k, v) VALUES(?, ?)", (k, None if v is None else str(v)))


def _utc_now(clock):
    return _dt.datetime.fromtimestamp(clock(), _dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ------------------------------------------------------------------------------------------------ driver

def _settle(path):
    """Open an existing index read-write once (SQLite rolls back a hot journal on first read) and return its
    meta, or None when it is not a usable index."""
    con = None
    try:
        con = sqlite3.connect(path, timeout=30, isolation_level=None)
        return dict(con.execute("SELECT k, v FROM meta").fetchall())
    except sqlite3.DatabaseError:
        return None
    finally:
        if con is not None:
            con.close()


def _head(meta):
    try:
        return int(meta.get("head_seq"))
    except (TypeError, ValueError):
        return None


def _watermark_ok(view, refs, meta):
    head = _head(meta)
    if head is None or head < 0:
        return False
    if head == 0:
        return True
    if head > len(refs):
        return False
    ref = refs[head - 1]
    return ref.run == meta.get("head_run") and view.file_sha(ref) == meta.get("head_sha256")


def update(data_root, spec, rebuild=False, clock=time.time):
    """Bring <data_root>/index/<spec.name>.sqlite up to the committed ledger. Returns a report dict (counts
    only). Raises Busy when another writer holds the lock."""
    t0 = time.monotonic()
    with IndexLock(index_dir(data_root), spec.name):
        view = LedgerView(data_root)
        refs = view.manifests()
        path = index_path(data_root, spec)
        meta = _settle(path) if path.exists() else None
        reason = "requested" if rebuild else None
        if reason is None:
            if not path.exists():
                reason = "missing"
            elif meta is None:
                reason = "unreadable"
            elif any(meta.get(k) != v for k, v in spec.stamp().items()):
                reason = "version"
            elif not _watermark_ok(view, refs, meta):
                reason = "ledger_reset"
        plan = None
        if reason is None:
            plan = _plan(view, refs, _head(meta), spec)
            reason = plan["reason"]
        if reason is not None:
            rep = _rebuild(view, refs, path, spec, reason, clock)
        else:
            rep = _catch_up(view, path, spec, plan, clock)
        rep.update(source=spec.name, ledger_seq=len(refs), seconds=round(time.monotonic() - t0, 1))
        rep["behind"] = rep["seq"] < len(refs)
        return rep


def _plan(view, refs, head, spec):
    """Manifests to apply after `head`, or a reason to rebuild instead."""
    todo, blocked, pending = [], None, 0
    for ref in refs[head:]:
        try:
            man, sha = view.load(ref)
        except LedgerDefect:
            blocked = ("defect", ref.seq)
            break
        st = view.commit_state(man)
        if st != "ok":
            blocked = ("mid_commit" if st == "mid_commit" else "defect", ref.seq)
            break
        if man.get("type") != "capture":
            return {"reason": "manifest_type"}
        pending += view.pending_records(man, spec.origins)
        # only what the catch-up reads: a real manifest also carries per-container facts (large), and a long
        # catch-up must not hold hundreds of them
        todo.append((ref, {"seq": man["seq"], "segments": man["segments"]}, sha))
    if pending > spec.full_rebuild_records:
        return {"reason": "large_catch_up"}
    return {"reason": None, "todo": todo, "blocked": blocked}


def _writer(path):
    con = sqlite3.connect(path, timeout=60, isolation_level=None)
    con.execute("PRAGMA journal_mode=DELETE")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("PRAGMA temp_store=MEMORY")
    return con


def _catch_up(view, path, spec, plan, clock):
    con = _writer(path)
    applied = records = 0
    blocked = plan["blocked"]
    try:
        app = spec.applier(con, full=False)
        for ref, man, sha in plan["todo"]:
            con.execute("BEGIN IMMEDIATE")
            try:
                for rec in view.records(man, spec.origins):
                    app.record(rec, ref.seq)
                    records += 1
                app.end_manifest(ref.seq)
                _set_meta(con, head_seq=ref.seq, head_sha256=sha, head_run=ref.run, updated_at=_utc_now(clock))
                con.execute("COMMIT")
            except LedgerDefect:
                con.execute("ROLLBACK")
                blocked = ("defect", ref.seq)
                break
            except BaseException:
                con.execute("ROLLBACK")
                raise
            applied += 1
        head = _head(dict(con.execute("SELECT k, v FROM meta").fetchall()))
        stats = spec.stats(con)
    finally:
        con.close()
    return {"action": "updated" if applied else "current", "reason": None, "seq": head, "manifests": applied,
            "records": records, "blocked": blocked[0] if blocked else None,
            "blocked_seq": blocked[1] if blocked else None, **stats}


def _remove(p):
    Path(p).unlink(missing_ok=True)


def _rebuild(view, refs, path, spec, reason, clock):
    tmp = _plus(path, ".new")
    for p in (tmp, _plus(tmp, "-journal")):
        _remove(p)
    con = sqlite3.connect(tmp, isolation_level=None)
    applied = records = 0
    blocked = None
    head = (0, "", "")
    ok = False
    try:
        # durability while building is irrelevant: only a checked file is ever swapped in
        con.execute("PRAGMA journal_mode=OFF")
        con.execute("PRAGMA synchronous=OFF")
        con.execute("PRAGMA temp_store=MEMORY")
        con.execute("PRAGMA cache_size=-200000")
        con.executescript(META_DDL)
        spec.create_tables(con)
        app = spec.applier(con, full=True)
        for ref in refs:
            try:
                man, sha = view.load(ref)
            except LedgerDefect:
                blocked = ("defect", ref.seq)
                break
            st = view.commit_state(man)
            if st != "ok":
                blocked = ("mid_commit" if st == "mid_commit" else "defect", ref.seq)
                break
            con.execute("BEGIN")
            for rec in view.records(man, spec.origins):     # a defect inside a segment aborts the whole build
                app.record(rec, ref.seq)
                records += 1
            app.end_manifest(ref.seq)
            con.execute("COMMIT")
            head = (ref.seq, sha, ref.run)
            applied += 1
        con.execute("BEGIN")
        app.finish()
        for fts in spec.fts_tables:
            con.execute(f"INSERT INTO {fts}({fts}) VALUES('rebuild')")
        spec.create_triggers(con)
        now = _utc_now(clock)
        _set_meta(con, **spec.stamp(), head_seq=head[0], head_sha256=head[1], head_run=head[2], built_at=now,
                  updated_at=now, build_reason=reason)
        con.execute("COMMIT")
        for fts in spec.fts_tables:          # the index against the content table (not a row count)
            con.execute(f"INSERT INTO {fts}({fts}, rank) VALUES('integrity-check', 1)")
        if con.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
            raise RuntimeError("new index failed quick_check")
        con.execute("PRAGMA journal_mode=DELETE")
        stats = spec.stats(con)
        ok = True
    finally:
        con.close()
        if not ok:
            _remove(tmp)
    _remove(_plus(path, "-journal"))    # _settle rolled a hot one back; the old file is being replaced anyway
    try:
        fsio.replace_retry(tmp, path)
    except PermissionError:
        _remove(tmp)
        raise RuntimeError("index file is held open by another process (a running search?); retry") from None
    return {"action": "rebuilt", "reason": reason, "seq": head[0], "manifests": applied, "records": records,
            "blocked": blocked[0] if blocked else None, "blocked_seq": blocked[1] if blocked else None, **stats}


# ------------------------------------------------------------------------------------------------ readers

def open_ro(path):
    con = sqlite3.connect(fsio.ro_uri(path, immutable=False), uri=True, timeout=15)
    con.execute("PRAGMA query_only=1")
    con.execute("PRAGMA temp_store=MEMORY")
    return con


def freshness(data_root, meta):
    """{"seq", "ledger_seq", "stale"} of an index's watermark against the ledger as it is now."""
    view = LedgerView(data_root)
    refs = view.manifests()
    head = _head(meta)
    return {"seq": head, "ledger_seq": len(refs),
            "stale": head is None or head != len(refs) or not _watermark_ok(view, refs, meta)}


def open_search(data_root, spec):
    """(read-only connection with rs_contains registered and sqlite3.Row rows, meta, freshness)."""
    path = index_path(data_root, spec)
    hint = f"run: python -s -B manage.py collect {spec.name}"
    if not path.exists():
        raise UsageError(f"no {spec.name} index yet -- {hint}")
    con = open_ro(path)
    try:
        meta = dict(con.execute("SELECT k, v FROM meta").fetchall())
    except sqlite3.DatabaseError:
        # also what a read-only connection says about a hot journal (an index run that died mid-update):
        # the next index step opens it read-write, which rolls the journal back
        con.close()
        raise UsageError(f"the {spec.name} index cannot be read (damaged, or left mid-update by an index run "
                         f"that died) -- {hint} --rebuild-index if that does not help") from None
    if any(meta.get(k) != v for k, v in spec.stamp().items()):
        con.close()
        raise UsageError(f"the {spec.name} index was built by another version (schema/tokenizer/extractor) -- "
                         f"{hint} --rebuild-index")
    con.row_factory = sqlite3.Row
    ti.register_functions(con)
    return con, meta, freshness(data_root, meta)


def warn_if_stale(spec, fresh):
    """stderr note when a search runs on an index that is not at the ledger's head."""
    if not fresh["stale"]:
        return
    seq, led = fresh["seq"], fresh["ledger_seq"]
    what = "is behind" if seq is not None and seq < led else "does not match"
    note(f"the {spec.name} index {what} the ledger (index seq {seq}, ledger seq {led}); results may miss or "
         f"misstate the newest captures -- run: python -s -B manage.py collect {spec.name}")


def status(data_root, spec):
    """Index state without writing anything (the lock is only probed when the index folder exists)."""
    path = index_path(data_root, spec)
    rep = {"source": spec.name, "state": "missing", "seq": None, "ledger_seq": len(LedgerView(data_root).manifests())}
    if index_dir(data_root).is_dir():
        try:
            IndexLock(index_dir(data_root), spec.name).release()
            rep["writer_busy"] = False
        except Busy:
            rep["writer_busy"] = True
    if not path.exists():
        rep["stale"] = True
        return rep
    try:
        con = open_ro(path)
    except sqlite3.DatabaseError:
        rep.update(state="unreadable", stale=True)
        return rep
    try:
        meta = dict(con.execute("SELECT k, v FROM meta").fetchall())
        fr = freshness(data_root, meta)
        rep.update(fr)
        rep["state"] = "ok" if all(meta.get(k) == v for k, v in spec.stamp().items()) else "outdated"
        for k in ("built_at", "updated_at", "build_reason", "schema_version", "tokenizer", "extractor_version"):
            rep[k] = meta.get(k)
        if rep["state"] == "ok":
            rep.update(spec.stats(con))
    except sqlite3.DatabaseError:
        rep.update(state="unreadable", stale=True)
    finally:
        con.close()
    return rep


# ------------------------------------------------------------------------------------------------ time

_TZ_RE = re.compile(r"^(?:UTC|Z|([+-])([0-9]{1,2}):?([0-9]{2}))$", re.I)
_DUR_RE = re.compile(r"^([0-9]+)\s*([mhdw])$", re.I)
_UNIT_S = {"m": 60, "h": 3600, "d": 86400, "w": 604800}


def parse_tz(s):
    m = _TZ_RE.fullmatch((s or "").strip())
    if not m:
        raise UsageError(f"--tz {s!r}: use an offset like +09:00 (or UTC)")
    if m.group(1) is None:
        return _dt.timezone.utc
    hh, mm = int(m.group(2)), int(m.group(3))
    if hh > 14 or mm > 59:
        raise UsageError(f"--tz {s!r}: offset out of range")
    off = _dt.timedelta(hours=hh, minutes=mm)
    return _dt.timezone(-off if m.group(1) == "-" else off)


def tz_label(tz):
    off = int(tz.utcoffset(None).total_seconds())
    sign = "-" if off < 0 else "+"
    off = abs(off)
    return f"{sign}{off // 3600:02d}:{off % 3600 // 60:02d}"


def tz_offset_s(tz):
    return int(tz.utcoffset(None).total_seconds())


def parse_when(s, tz, end=False, now=None):
    """Unix seconds for --since/--until: YYYY, YYYY-MM, YYYY-MM-DD, 'YYYY-MM-DD HH:MM' (also with T) in tz, or
    a duration back from now (30m, 12h, 3d, 2w). With end=True the result is the LAST second of the given
    period, so --until is inclusive (2024-10 covers all of October)."""
    v = (s or "").strip()
    m = _DUR_RE.fullmatch(v)
    if m:
        now = time.time() if now is None else now
        return int(now - int(m.group(1)) * _UNIT_S[m.group(2).lower()])
    for fmt, unit in (("%Y-%m-%d %H:%M", "min"), ("%Y-%m-%dT%H:%M", "min"), ("%Y-%m-%d", "d"), ("%Y-%m", "m"),
                      ("%Y", "y")):
        try:
            d = _dt.datetime.strptime(v, fmt).replace(tzinfo=tz)
        except ValueError:
            continue
        if not end:
            return int(d.timestamp())
        if unit == "y":
            nxt = d.replace(year=d.year + 1)
        elif unit == "m":
            nxt = (d.replace(day=28) + _dt.timedelta(days=5)).replace(day=1)
        elif unit == "d":
            nxt = d + _dt.timedelta(days=1)
        else:
            nxt = d + _dt.timedelta(minutes=1)
        return int(nxt.timestamp()) - 1
    raise UsageError(f"cannot parse date {s!r}; use YYYY-MM-DD, YYYY-MM, YYYY, 'YYYY-MM-DD HH:MM' or 3d/12h/30m/2w")


def fmt_ts(ts, tz, fmt="%Y-%m-%d %H:%M"):
    if ts is None or ts <= 0:
        return "?"
    try:
        return _dt.datetime.fromtimestamp(ts, tz).strftime(fmt)
    except (OverflowError, OSError, ValueError):
        return "?"


def month_sql(col, tz):
    return f"strftime('%Y-%m', {col} + {tz_offset_s(tz)}, 'unixepoch')"


def clip_buckets(rows, limit):
    """Keep the newest `limit` buckets of an ascending series -> (kept, number of older ones dropped).
    limit <= 0 keeps everything."""
    rows = list(rows)
    if not limit or limit <= 0 or len(rows) <= limit:
        return rows, 0
    return rows[-limit:], len(rows) - limit


# ------------------------------------------------------------------------------------------------ cursors

_CURSOR_RE = re.compile(r"^([0-9]{1,12})\.([0-9]{1,19})(?:\.([0-9]{1,19}))?$")
_I63 = 2 ** 63


def parse_cursor(s, flag, before=True):
    """'<ts>.<n>.<row>' keyset cursor -> (ts, n, row) ints. The row id breaks ties between rows that share (ts, n)
    (the same logId in two rooms, or several log_no 0 rows), so a page boundary never skips one of them.
    The older two-part form '<ts>.<n>' still works and means the same as before: every row of that (ts, n) is
    past the cursor (row 0 for --before, the largest row id for --after)."""
    m = _CURSOR_RE.fullmatch((s or "").strip())
    if not m or int(m.group(2)) >= _I63 or (m.group(3) is not None and int(m.group(3)) >= _I63):
        raise UsageError(f"{flag} {s!r}: expected a cursor like 1727758980.12345.678 (printed with each page)")
    row = int(m.group(3)) if m.group(3) is not None else (0 if before else _I63 - 1)
    return int(m.group(1)), int(m.group(2)), row


# ------------------------------------------------------------------------------------------------ rendering

def cells(text):
    """Terminal cells a string takes (Hangul and other wide characters take two)."""
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)


def pad(text, width, *, right=False):
    """Pad or cut to exactly `width` cells, so columns line up with Korean labels."""
    out, used = [], 0
    for ch in text:
        step = 2 if unicodedata.east_asian_width(ch) in "WF" else 1
        if used + step > width:
            break
        out.append(ch)
        used += step
    fill = " " * (width - used)
    s = "".join(out)
    return fill + s if right else s + fill


def flat(text):
    """Whitespace (newlines included) collapsed to single spaces."""
    return " ".join((text or "").split())


def clip(text, width):
    """Cut to at most `width` cells, with an ellipsis when cut."""
    if cells(text) <= width:
        return text
    return pad(text, max(1, width - 1)).rstrip() + "…"


def dumps(obj):
    return json.dumps(obj, ensure_ascii=False, indent=1)


def note(msg):
    print("# note: " + msg, file=sys.stderr)


def setup_stdio():
    """UTF-8 console streams (a Windows console defaults to cp949 here). Called from main() only, never on
    import: importing this package must not change the importer's streams."""
    for stream in (sys.stdout, sys.stderr):
        rc = getattr(stream, "reconfigure", None)
        if rc is not None:
            try:
                rc(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass


def show_fts(expr):
    """An FTS5 expression with the SEP token made visible."""
    return (expr or "").replace(ti.SEP, "<S>")


def like(col, value):
    """(sql, params) for a substring filter on a label column, wildcards escaped."""
    return f"{col} LIKE ? ESCAPE '\\'", [ti.like_pattern(value)]


# ------------------------------------------------------------------------------------------------ arguments

def add_search_arguments(p, modes):
    """Arguments every source's search subcommand shares."""
    p.add_argument("--config", required=True, help="capture config file (only data_root is read)")
    p.add_argument("query", nargs="?", default=None, metavar="QUERY",
                   help="search terms: words AND-ed, \"exact phrase\", a OR b, -exclude, prefix*")
    p.add_argument("-q", "--query", dest="query_opt", default=None, metavar="QUERY",
                   help="the query as an option (use this when it starts with '-')")
    p.add_argument("--sender", help="substring of the sender name (or an exact user id)")
    p.add_argument("--since", metavar="DATE", help="YYYY-MM-DD, YYYY-MM, YYYY, 'YYYY-MM-DD HH:MM' or 3d/12h")
    p.add_argument("--until", metavar="DATE", help="inclusive: 2024-10 ends with the last second of October")
    p.add_argument("--tz", default=DEFAULT_TZ, help="fixed UTC offset for dates, months and display "
                                                    "(default +09:00)")
    p.add_argument("--mode", choices=modes, default="hits")
    p.add_argument("--context", type=int, default=0, metavar="N", help="N messages before and after each hit")
    p.add_argument("--limit", type=int, default=20, help="rows (default 20); timeline: newest N months")
    p.add_argument("--width", type=int, default=140, help="snippet width in characters (default 140)")
    p.add_argument("--include-deleted", action="store_true", help="also show deleted messages (hidden by default)")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.add_argument("--explain", action="store_true", help="print the translated FTS5 expression")


def query_text(a):
    if a.query is not None and a.query_opt is not None:
        raise UsageError("give the query once: either QUERY or -q")
    q = a.query if a.query is not None else a.query_opt
    return q if q is not None and q.strip() else None


def check_numbers(a):
    if a.limit < 0 or (a.limit == 0 and a.mode != "timeline"):
        raise UsageError("--limit must be at least 1 (0 = no limit is allowed with --mode timeline only)")
    if a.context < 0:
        raise UsageError("--context must not be negative")
    if a.width < 20:
        raise UsageError("--width must be at least 20")


class Parser(argparse.ArgumentParser):
    """argparse that raises UsageError instead of exiting, so main() owns the exit code."""

    def error(self, message):
        raise UsageError(message)
