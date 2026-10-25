"""Immutable Claude Code partitions, read in manifest order in one stable pass."""
import json
from pathlib import Path
from rawlayer.capture import canon, fsio
from rawlayer.capture.model import Obs
from rawlayer.capture.multiver import OriginRead
from rawlayer.capture.origins import Unstable
from ..keys import cc_id, iso_ms


def _reject_constant(value):
    raise ValueError("nonfinite JSON")


def _load(data):
    return json.loads(data, parse_constant=_reject_constant)


def _safe_rel(rel, cid):
    return isinstance(rel, str) and rel.startswith(f"exports/{cid}/") and rel.endswith(".json") \
        and not any(x in rel for x in ("\\", ":", "\x00")) \
        and len(rel.split("/")) == 3 and all(p not in ("", ".", "..") for p in rel.split("/"))


def read_once(root, denylist, origin):
    root = Path(root)
    manifest = root / "manifest.json"
    before = fsio.read_shared(manifest)
    man = _load(before)
    if not isinstance(man, dict) or type(man.get("version")) is not int or man["version"] != 1 \
            or not isinstance(man.get("containers"), dict):
        raise ValueError("manifest")
    rd = OriginRead(origin, denylist)
    referenced = set()

    def container(rel, status="ok", path=None):
        sid = canon.source_id(rel)
        rd.srcmap[sid] = rel
        rd.containers[sid] = {"status": status}
        if status == "ok":
            rd.asof[sid] = fsio.mtime_s(path or manifest)
        return sid

    for cid, info in sorted(man["containers"].items()):
        files = info.get("files") if isinstance(info, dict) else None
        failure = None
        if not isinstance(files, list):
            files, failure = [], "bad_file"
        referenced.update(r for r in files if isinstance(r, str))
        if cc_id(cid) is None or not isinstance(info, dict) or info.get("id") != cid:
            failure = "pk_bad"
        elif not all(_safe_rel(r, cid) for r in files):
            failure = "bad_file"
        elif files != sorted(files) or len(set(files)) != len(files):
            failure = "order_bad"
        parsed = []
        if failure is None:
            for rel in files:
                try:
                    path = root / rel
                    if not path.resolve().is_relative_to((root / "exports" / cid).resolve()) \
                            or not path.resolve().is_relative_to(root.resolve()):
                        raise ValueError("path")
                    obj = _load(fsio.read_shared(path))
                    rows = obj.get("rows")
                    if not isinstance(rows, list) or len(rows) > 10000 or not all(isinstance(r, dict) for r in rows):
                        raise ValueError("rows")
                    if type(obj.get("rowCount")) is not int or obj["rowCount"] != len(rows):
                        failure = "count_mismatch"
                        break
                    if obj.get("container") != cid:
                        failure = "container_mismatch"
                        break
                    parsed.append((rel, path, rows))
                except (OSError, ValueError, AttributeError):
                    failure = "bad_file"
                    break
        if failure:
            rd.notes[failure] += 1
            container("manifest.json#container/" + cid, "error")
            for rel in files:
                if _safe_rel(rel, cid):
                    container(rel, "error")
            continue
        container("manifest.json#container/" + cid)
        for rel, path, rows in parsed:
            sid = container(rel, path=path)
            for row in rows:
                uuid = cc_id(row.get("uuid"))
                if uuid is None:
                    rd.notes["pk_bad"] += 1
                    continue
                ts_ms = iso_ms(row.get("ts"))
                if ts_ms is None:
                    rd.notes["ts_bad"] += 1
                    continue
                rd.add(Obs("msg", f"cc:msg:{cid}:{uuid}", row, {}, ts_ms, rd.asof[sid]), sid)
    for path in (root / "exports").rglob("*.json"):
        if path.relative_to(root).as_posix() not in referenced:
            rd.notes["orphan_file"] += 1
    if fsio.read_shared(manifest) != before:
        raise Unstable()
    return rd
