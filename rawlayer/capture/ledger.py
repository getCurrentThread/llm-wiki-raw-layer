# -*- coding: utf-8 -*-
"""Append-only ledger files: segments, manifests, inflight bookkeeping (SPEC 3, 9)."""
import gzip
import hashlib
import heapq
import json
import os
import re
from pathlib import Path

from .. import registry
from . import fsio, publicout
from .config import INPUT_SETS
from .publicout import lit

PART_LIMIT = 256 * 1024 * 1024
SPILL_LIMIT = 192 * 1024 * 1024
_MAN_RE = re.compile(r"^([0-9]{8})-([0-9]{13}-[0-9a-f]{4})\.json$")
_SEG_PATH = re.compile(r"^ledger/([a-z_]+)/[0-9]{4}/seg-[0-9]{8}-([a-z_]+)-[0-9]{3}\.jsonl\.gz$")
_ISO = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")


class LedgerCorrupt(Exception):
    pass


class Ledger:
    """The ledger under one raw directory. Every path attribute is a Path."""

    def __init__(self, raw_dir):
        self.raw = Path(raw_dir)
        self.ledger_dir = self.raw / "ledger"
        self.man_dir = self.raw / "manifests"
        self.state_dir = self.raw / ".state"
        self.work_root = self.raw / ".work"
        self.verify_root = self.raw / ".verify"
        self.log_md = self.raw / "log.md"
        self.inflight = self.state_dir / "inflight.json"
        self.denylist_path = self.raw / "denylist.json"

    def ensure_dirs(self):
        for d in (self.raw, self.ledger_dir, self.man_dir, self.state_dir, self.work_root, self.verify_root):
            d.mkdir(parents=True, exist_ok=True)

    # ---- manifests
    def manifests(self):
        """[(seq, run, path)] in seq order. A gap or a duplicate seq raises LedgerCorrupt (capture, replay and
        export-state stop with exit 1)."""
        out = []
        if self.man_dir.is_dir():
            for p in self.man_dir.iterdir():
                m = _MAN_RE.fullmatch(p.name)
                if m:
                    out.append((int(m.group(1)), m.group(2), p))
        out.sort()
        if seq_gaps(out):
            raise LedgerCorrupt("manifest seq gap")
        return out

    def read_manifest(self, path):
        return json.loads(Path(path).read_bytes())

    def manifest_path(self, seq, run):
        return self.man_dir / f"{seq:08d}-{run}.json"

    def iter_records(self, manifest):
        for seg in manifest.get("segments", []):
            yield from self.iter_segment(self.raw.joinpath(*seg["path"].split("/")))

    def seal(self):
        """{defect: count} of the hash chain, from the files alone: each manifest's prev_sha256 is the sha256 of the
        bytes of the one before, and each segment has its manifest's size and sha256. Empty when sound."""
        bad = {}
        prev = None
        for _seq, _run, path in self.manifests():
            data = Path(path).read_bytes()
            m = json.loads(data)
            if m.get("prev_sha256") != (hashlib.sha256(prev).hexdigest() if prev is not None else None):
                bad["prev_chain"] = bad.get("prev_chain", 0) + 1
            for s in m.get("segments", []):
                p = self.raw.joinpath(*s["path"].split("/"))
                if not p.is_file():
                    bad["segment_missing"] = bad.get("segment_missing", 0) + 1
                elif p.stat().st_size != s.get("bytes") or fsio.sha256_file(p) != s.get("sha256"):
                    bad["segment_mismatch"] = bad.get("segment_mismatch", 0) + 1
            prev = data
        return bad

    @staticmethod
    def iter_segment(path):
        with gzip.open(path, "rb") as fh:
            for line in fh:
                yield json.loads(line)

    def denylist(self):
        if not self.denylist_path.exists():
            return frozenset()
        with self.denylist_path.open(encoding="utf-8") as fh:
            d = json.load(fh)
        return frozenset((e["origin"], e["key"]) for e in d.get("keys", []))

    # ---- recovery
    def part_files(self, seq=None):
        out = []
        # os.walk, not Path.walk: Path.walk enters no junction and files a link to a directory as a file
        for dp, _d, fs in os.walk(self.ledger_dir):
            for f in fs:
                if f.endswith(".part") and (seq is None or f.startswith(f"seg-{seq:08d}-")):
                    out.append(Path(dp, f))
        return sorted(out, key=str)         # text order (a Windows Path sorts case-blind)

    def manifest_parts(self):
        if not self.man_dir.is_dir():
            return []
        return sorted((p for p in self.man_dir.iterdir() if p.name.endswith(".part")), key=lambda p: p.name)

    def leftovers(self):
        """.part files and inflight.json still on disk (must be 0 after recovery)."""
        return len(self.part_files()) + len(self.manifest_parts()) + (1 if self.inflight.exists() else 0)

    def _named(self, rel, under):
        """Absolute path of an inflight.json entry, or None unless it is a .part strictly below `under`.
        SPEC 9.1 does not fix the spelling: raw-relative with '/' (what commit writes), data_root-relative
        ('raw\\ledger\\...', SPEC 3's notation) or an absolute path."""
        if not isinstance(rel, str) or not rel.endswith(".part") or "\x00" in rel:
            return None
        # os.path on the entry's text: normpath folds '..' (a Path keeps it), normcase compares case-blind
        if os.path.isabs(rel):
            p = os.path.normpath(rel)
        else:
            parts = re.split(r"[\\/]", rel)
            if len(parts) > 1 and parts[0].lower() == "raw":
                parts = parts[1:]           # below raw only ledger/ and manifests/ hold parts: no clash
            p = os.path.normpath(os.path.join(self.raw, *parts))
        base = os.path.normcase(os.path.normpath(under)) + os.sep
        return Path(p) if os.path.normcase(p).startswith(base) else None

    def recover(self):
        """Finish or discard the commit inflight.json describes (SPEC 9.1 step 9). Only the files it names are
        touched: any other .part is evidence of damage and stays on disk (leftovers() counts it).
        Returns 'none', 'finished' or 'discarded'."""
        fsio.remove_file(fsio.suffixed(self.inflight, ".tmp"))  # a torn atomic_write; inflight.json itself is whole
        if not self.inflight.exists():
            return "none"
        with self.inflight.open(encoding="utf-8") as fh:
            inf = json.load(fh)
        man_part = self._named(inf.get("manifest_part"), self.man_dir)
        parts = [self._named(rel, self.ledger_dir) for rel in inf.get("parts") or []]
        if man_part is None or None in parts or not isinstance(inf.get("seq"), int):
            raise LedgerCorrupt("inflight.json")
        man = _unpart(man_part)
        if man.exists():
            committed = self.read_manifest(man)
            if not manifest_ok(committed):
                raise LedgerCorrupt("manifest")
            for p in parts:
                final = _unpart(p)
                if p.exists():
                    fsio.replace_retry(p, final)
                if final.exists():
                    fsio.make_readonly(final)       # a crash may fall between the rename and the chmod
            self.backfill_log(committed)
            result = "finished"
        else:
            for p in parts:
                fsio.remove_file(p)
            fsio.remove_file(man_part)
            result = "discarded"
        fsio.remove_file(self.inflight)
        return result

    def backfill_log(self, manifest):
        """log.md line of a committed capture whose run died before writing it (recovery)."""
        run = manifest.get("run")
        try:
            with self.log_md.open(encoding="ascii", errors="replace") as fh:
                if any(f" run={run} " in ln for ln in fh):
                    return
        except FileNotFoundError:
            pass
        log_capture(self, manifest)


def _unpart(part: Path) -> Path:
    """The committed path of a .part path: its name without '.part' (a bare '.part' names its directory, as the
    text cut of the name always did)."""
    name = part.name[:-len(".part")]
    return part.with_name(name) if name else part.parent


def seq_gaps(mans):
    """Seq numbers missing from (or duplicated in) 1..max of a manifests() listing."""
    seqs = [s for s, _r, _p in mans]
    top = max(seqs, default=0)
    return len(set(range(1, top + 1)) - set(seqs)) + (len(seqs) - len(set(seqs)))


def _int(x):
    return isinstance(x, int) and not isinstance(x, bool)


def _strs(x):
    return isinstance(x, list) and all(isinstance(v, str) for v in x)


def manifest_ok(m):
    """True when a parsed capture manifest has every field state and export-state index without a guard.
    JSON that parses but fails this (a flipped byte inside a key or a value) is an unreadable manifest."""
    if not isinstance(m, dict) or not _int(m.get("seq")) or not isinstance(m.get("run"), str) \
            or m.get("input_set") not in INPUT_SETS or not isinstance(m.get("finished_at"), str) \
            or not _ISO.fullmatch(m["finished_at"]):
        return False
    segs, origins = m.get("segments"), m.get("origins")
    if not isinstance(segs, list) or not isinstance(origins, dict):
        return False
    order, statuses = registry.origin_order(), registry.statuses()
    for s in segs:
        if not isinstance(s, dict) or not isinstance(s.get("path"), str) or not isinstance(s.get("sha256"), str) \
                or not _int(s.get("records")) or not _int(s.get("bytes")):
            return False
        g = _SEG_PATH.fullmatch(s["path"])
        if g is None or not g.group(1) == g.group(2) == s.get("origin") or s["origin"] not in order:
            return False
    for o, doc in origins.items():
        if o not in order or not isinstance(doc, dict) or doc.get("status") not in statuses:
            return False
        counts, pend = doc.get("counts"), doc.get("pending")
        if not isinstance(counts, dict) or not all(_int(v) for v in counts.values()) \
                or not isinstance(pend, dict) or not _strs(pend.get("add")) or not _strs(pend.get("drop")) \
                or not isinstance(doc.get("notes"), dict) or not isinstance(doc.get("containers"), dict) \
                or not all(isinstance(f, dict) and f.get("status") in statuses for f in doc["containers"].values()):
            return False
    return True


def foreign_origins(raw_dir):
    """The origins the newest manifest under raw_dir names that the selected app does not have (sorted; [] for a
    ledger with no manifest). One app owns one ledger: a non-empty answer means the data root belongs to another
    app, and every command that would read or write it refuses. Only origin names are compared; nothing of the
    manifest is printed. A newest manifest that does not parse raises LedgerCorrupt (OSError passes through)."""
    man_dir = Path(raw_dir) / "manifests"
    try:
        names = [p.name for p in man_dir.iterdir() if _MAN_RE.fullmatch(p.name)]
    except FileNotFoundError:
        return []
    if not names:
        return []
    data = (man_dir / max(names)).read_bytes()
    try:
        m = json.loads(data)
    except ValueError:
        raise LedgerCorrupt("manifest") from None
    named = set(m.get("origins") or {}) if isinstance(m, dict) and isinstance(m.get("origins"), dict) else set()
    if isinstance(m, dict) and isinstance(m.get("segments"), list):
        named |= {s.get("origin") for s in m["segments"] if isinstance(s, dict) and isinstance(s.get("origin"), str)}
    return sorted(named - set(registry.origin_order()))


def log_capture(ledger, manifest):
    """One log.md line per committed capture (written by commit, or by recovery when the run died first)."""
    publicout.append_line(ledger.log_md, lit("-"), manifest["finished_at"], run=manifest["run"],
                          seq=manifest["seq"], input_set=manifest["input_set"],
                          records=sum(s["records"] for s in manifest.get("segments", [])),
                          suspect=sum(1 for o in manifest["origins"].values() if o["status"] == "suspect"))


class OriginSpill:
    """Sorted (key, rev) record lines for one origin in one run, spilled to .work when large.

    Spill line = key NUL rev(10 digits) NUL canonical-record. Lines never contain NUL or newline
    (canonical JSON escapes control characters; pk_text rejects them in keys), so plain string order of
    spill lines equals (key, rev) order.
    """

    def __init__(self, work_dir, origin):
        self.work_dir = work_dir
        self.origin = origin
        self.buf = []
        self.nbytes = 0
        self.runs = []
        self.count = 0
        self._mark = (0, 0, 0)

    def begin(self):
        self._mark = (len(self.buf), self.nbytes, self.count)

    def rollback(self):
        n, b, c = self._mark
        del self.buf[n:]
        self.nbytes, self.count = b, c

    def add(self, key, rev, line):
        s = f"{key}\x00{rev:010d}\x00{line}"
        self.buf.append(s)
        self.nbytes += len(s)
        self.count += 1

    def settle(self):
        """Container boundary: spill if the buffer is large."""
        if self.nbytes >= SPILL_LIMIT:
            self.flush()

    def flush(self):
        if not self.buf:
            return
        self.buf.sort()
        p = Path(self.work_dir, f"spill-{self.origin}-{len(self.runs):04d}.txt")
        with p.open("w", encoding="utf-8", newline="\n") as fh:
            for s in self.buf:
                fh.write(s)
                fh.write("\n")
        self.runs.append(p)
        self.buf = []
        self.nbytes = 0

    def discard(self):
        self.buf = []
        self.nbytes = 0
        self.count = 0
        for p in self.runs:
            fsio.remove_file(p)
        self.runs = []

    def merged(self):
        self.buf.sort()
        files = [p.open(encoding="utf-8", newline="\n") for p in self.runs]
        try:
            streams = [(ln.rstrip("\n") for ln in f) for f in files] + [iter(self.buf)]
            yield from heapq.merge(*streams)
        finally:
            for f in files:
                f.close()


def write_segments(ledger: Ledger, spill: OriginSpill, seq: int, year: int, on_part=None):
    """Write spill as gzip parts (.part). Returns [{path, part_path, origin, records, bytes, sha256}]: path is the
    raw-relative '/' text the manifest holds, part_path the Path of the .part file.
    on_part(rel) runs before each .part file is created (inflight.json bookkeeping, SPEC 9.1 step 5)."""
    if spill.count == 0:
        return []
    d = ledger.ledger_dir / spill.origin / f"{year:04d}"
    d.mkdir(parents=True, exist_ok=True)
    infos = []
    part = -1
    gz = raw = None
    used = 0
    n = 0

    def close():
        nonlocal gz, raw
        if gz is None:
            return
        gz.close()
        raw.flush()
        os.fsync(raw.fileno())
        raw.close()
        gz = raw = None

    def open_next():
        nonlocal gz, raw, part, used, n
        part += 1
        used = 0
        n = 0
        name = f"seg-{seq:08d}-{spill.origin}-{part:03d}.jsonl.gz"
        rel = f"ledger/{spill.origin}/{year:04d}/{name}"
        pp = d / (name + ".part")
        if on_part is not None:
            on_part(rel + ".part")
        raw = pp.open("wb")
        gz = gzip.GzipFile(filename="", mode="wb", fileobj=raw, compresslevel=6, mtime=0)
        infos.append({"path": rel, "part_path": pp, "origin": spill.origin, "records": 0})

    open_next()
    for s in spill.merged():
        line = s.split("\x00", 2)[2]
        b = (line + "\n").encode("ascii")
        if used and used + len(b) > PART_LIMIT:
            close()
            open_next()
        gz.write(b)
        used += len(b)
        infos[-1]["records"] += 1
    close()
    for info in infos:
        info["bytes"] = info["part_path"].stat().st_size
        info["sha256"] = fsio.sha256_file(info["part_path"])
    return infos
