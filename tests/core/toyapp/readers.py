# -*- coding: utf-8 -*-
"""The toy sources: one directory per origin (a, b, m), every container a file <name>.jsonl of JSON lines.

  {"id": "<id>", "v": <any JSON>, "t": <ms>}   one observation, key toy:<origin>:<id> (native {"id", "v"})
  {"fail": "io"}                                reading the container fails with OSError
  {"fail": "<status>", "note": "<note>"}        the container fails with ContainerError(status, note)
  a line that is not a JSON object              note bad_line, skipped

b: a marker file <name>.partial next to a container makes it 'partial' (read, but not completely).
m: list.json names the containers (["c1", ...]); a listed container without its file is 'missing'. One pass reads
   STAMP before and after; a changed STAMP is Unstable. `between` (tests) runs after the pass's first file."""
import json
from pathlib import Path

from rawlayer.capture import canon, fsio
from rawlayer.capture.model import Container, ContainerError, Obs
from rawlayer.capture.multiver import OriginRead
from rawlayer.capture.origins import Unstable

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
