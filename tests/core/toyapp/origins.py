# -*- coding: utf-8 -*-
"""toyapp's capture side: its inputs (resolve), vocabulary (WORDS, PATTERNS), reader and ORIGINS.

The toy sources: one directory per origin (a, b, m), every container a file <name>.jsonl of JSON lines.

  {"id": "<id>", "v": <any JSON>, "t": <ms>}   one observation, key toy:<origin>:<id> (native {"id", "v"})
  {"fail": "io"}                                reading the container fails with OSError
  {"fail": "<status>", "note": "<note>"}        the container fails with ContainerError(status, note)
  a line that is not a JSON object              note bad_line, skipped

b: a marker file <name>.partial next to a container makes it 'partial' (read, but not completely).
m: list.json names the containers (["c1", ...]); a listed container without its file is 'missing'. One pass reads
   STAMP before and after; a changed STAMP is Unstable. `between` (tests) runs after the pass's first file."""
from pathlib import Path
import json
from rawlayer.capture import canon, fsio
from rawlayer.capture.model import Container, ContainerError, Obs
from rawlayer.capture.multiver import OriginRead
from rawlayer.capture.origins import Guard, MultiVersionMixin, Origin, PendingAbsentMixin, Unstable


def resolve(cfg):
    root = Path(cfg.sources["toy_dir"])
    return {"toy_a": root / "a", "toy_b": root / "b", "toy_m": root / "m"}


WORDS = ("bad_line",)

PATTERNS = (
    r"^toy:[a-z_]+:[a-z0-9]+$",         # key (pending lists); an id with other characters is unprintable
)


between = None


def key(origin, ident):
    return f"toy:{origin}:{ident}"


def _lines(path, notes):
    data = Path(path).read_bytes()
    for ln in data.splitlines():
        if not ln.strip():
            continue
        try:
            o = json.loads(ln)
        except ValueError:
            notes["bad_line"] += 1
            continue
        if not isinstance(o, dict):
            notes["bad_line"] += 1
            continue
        if "fail" in o:
            if o["fail"] == "io":
                raise OSError(5, "toy read failure")
            raise ContainerError(o["fail"], o.get("note", "bad_line"))
        yield o


def _obs(o, origin, kind, asof_s):
    v = o.get("v")
    return Obs(kind, key(origin, o["id"]), {"id": o["id"], "v": v}, {}, o.get("t"), asof_s)


def containers(origin, root, kind, status="ok", partial=False):
    """One Container per <name>.jsonl of `root`, in name order."""
    root = Path(root)
    out = []
    for path in sorted(root.iterdir(), key=lambda p: p.name):
        name = path.name
        if not name.endswith(".jsonl"):
            continue
        rel = root.name + "/" + name
        st = "partial" if partial and path.with_name(name[:-len(".jsonl")] + ".partial").exists() else status
        c = Container(origin, rel, canon.source_id(rel), status=st, asof_s=fsio.mtime_s(path))

        def it(path=path, c=c):
            for o in _lines(path, c.notes):
                yield _obs(o, origin, kind, c.asof_s)
        c.iter_obs = it
        out.append(c)
    return out


def _stamp(root):
    try:
        return (Path(root) / "STAMP").read_bytes()
    except FileNotFoundError:
        return b""


def read_versions(origin, root, denylist):
    """One pass of a multi-version origin (MultiVersionMixin.read_once)."""
    root = Path(root)
    rd = OriginRead(origin, denylist)
    stamp = _stamp(root)
    listed = json.loads((root / "list.json").read_bytes())
    if not isinstance(listed, list):
        raise ValueError("list.json")
    for i, name in enumerate(sorted(listed)):
        rel = "m/" + name + ".jsonl"
        sid = canon.source_id(rel)
        rd.srcmap[sid] = rel
        path = root / (name + ".jsonl")
        if not path.exists():
            rd.containers[sid] = {"status": "missing"}
            continue
        rd.containers[sid] = {"status": "ok"}
        asof = fsio.mtime_s(path)
        for o in _lines(path, rd.notes):
            rd.add(_obs(o, origin, "rec", asof), sid)
        if i == 0 and between is not None:
            between(root)
    if _stamp(root) != stamp:
        raise Unstable()
    return rd


TOY_LAST = ("toy_last", "CREATE TABLE IF NOT EXISTS toy_last(key TEXT PRIMARY KEY, h TEXT NOT NULL,\n"
                        "  seq INTEGER NOT NULL) WITHOUT ROWID;")


class ToyA(Origin):
    """Plain: a key that disappears is vanished (the origin is suspect). Keeps the last h per key in toy_last."""
    name = "toy_a"
    kinds = frozenset({"item"})
    day_kinds = frozenset({"item"})
    state_tables = (TOY_LAST,)

    def read(self, ctx):
        return containers(self.name, ctx.inputs.toy_a, "item")

    def apply_put(self, cur, rec):
        cur.execute("INSERT OR REPLACE INTO toy_last(key, h, seq) VALUES(?,?,?)", (rec["key"], rec["h"], rec["seq"]))

    def estimate_peak(self, inputs):
        root = inputs.toy_a
        return sum(fsio.size(p) for p in root.iterdir()) if root.is_dir() else 0

    def requires_system(self, cfg):
        return cfg.toy_system


class ToyB(PendingAbsentMixin, Origin):
    """Two-phase absent behind a shrink guard that trips on more than 3 new missing keys."""
    name = "toy_b"
    kinds = frozenset({"item"})
    statuses = PendingAbsentMixin.statuses | {"unreadable"}
    fail_statuses = frozenset({"unreadable"})
    fail_default = "unreadable"
    guard = Guard(min_obs_ratio=0.9, max_new_abs=3, max_new_frac=0.01)

    def read(self, ctx):
        return containers(self.name, ctx.inputs.toy_b, "item", status="clean", partial=True)


class ToyM(MultiVersionMixin, Origin):
    """Read in one stable pass; every version of a key is recorded once."""
    name = "toy_m"
    kinds = frozenset({"rec"})
    day_kinds = frozenset({"rec"})
    retry_sleep = 0

    def read_once(self, ctx):
        return read_versions(self.name, ctx.inputs.toy_m, ctx.denylist)


ORIGINS = (ToyA, ToyB, ToyM)
