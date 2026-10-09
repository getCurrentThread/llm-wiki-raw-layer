"""Lazy bounded partitions and nonshrinking state shards; no reader exclusions."""
import json
from pathlib import Path
import re
from rawlayer.capture import canon, fsio
from rawlayer.capture.model import Container, ContainerError, Obs
from ..keys import gm_id, MAX_MS

MAX_BYTES = 16 * 1024 * 1024


def safe_rel(rel, folder):
    pattern = r"exports/[0-9]{8}T[0-9]{12}Z\.json" if folder == "exports" else r"state/[0-9a-f]{1,2}\.json"
    return isinstance(rel, str) and re.fullmatch(pattern, rel) is not None


def _json(data):
    def reject(value):
        raise ValueError("nonfinite")
    return json.loads(data, parse_constant=reject)


def partition(data):
    if len(data) > MAX_BYTES:
        raise ValueError("partition cap")
    obj = _json(data)
    if not isinstance(obj, dict) or obj.get("version") != 1 or not isinstance(obj.get("messages"), list) \
            or len(obj["messages"]) > 100000 or not all(isinstance(v, dict) for v in obj["messages"]):
        raise ValueError("partition")
    return obj["messages"]


def _label_id(value):
    return canon.pk_text(value) if isinstance(value, str) else None


def _state(value):
    if not isinstance(value, dict) or not isinstance(value.get("label_ids"), list) \
            or not all(_label_id(v) for v in value["label_ids"]) \
            or value["label_ids"] != sorted(set(value["label_ids"])):
        raise ValueError("state")
    result = {"label_ids": value["label_ids"]}
    if "_removedAt" in value:
        if not isinstance(value["_removedAt"], str):
            raise ValueError("tombstone")
        result["_removedAt"] = value["_removedAt"]
    return result


def containers(root, origin="gm_archive"):
    root = Path(root)
    manifest = root / "manifest.json"
    if not manifest.resolve().is_relative_to(root.resolve()):
        raise ValueError("path")
    before = fsio.read_shared(manifest)
    man = _json(before)
    if not isinstance(man, dict) or man.get("version") != 1:
        raise ValueError("manifest")
    out, paths, seen_messages = [], [], set()
    for field, folder in (("files", "exports"), ("state_files", "state")):
        files = man.get(field)
        if not isinstance(files, list) or not all(isinstance(v, str) for v in files) \
                or files != sorted(set(files)):
            raise ValueError("files")
        paths.extend((rel, folder) for rel in files)
    referenced = {rel for rel, _ in paths}
    paths.append(("labels.json", "labels"))
    for rel, folder in paths:
        c = Container(origin, rel, canon.source_id(rel))

        def read(rel=rel, folder=folder, c=c):
            try:
                if folder != "labels" and not safe_rel(rel, folder):
                    raise ValueError("path")
                path = root / rel
                if not path.resolve().is_relative_to(root.resolve()):
                    raise ValueError("path")
                if fsio.read_shared(manifest) != before:
                    raise ValueError("unstable")
                data = fsio.read_shared(path)
                c.asof_s = fsio.mtime_s(path)
                obs = []
                if folder == "exports":
                    for value in partition(data):
                        ident = gm_id(value.get("id"))
                        if ident is None:
                            c.notes["pk_bad"] += 1
                            continue
                        ts = value.get("internal_date_ms")
                        if type(ts) is not int or not 0 <= ts <= MAX_MS or gm_id(value.get("thread_id")) is None \
                                or not isinstance(value.get("body_text"), str) or not isinstance(value.get("attachments"), list):
                            raise ValueError("message")
                        if any(k in value for k in ("label_ids", "_removedAt", "raw", "attachmentId")):
                            raise ValueError("immutable content")
                        obs.append(Obs("msg", "gm:msg:" + ident, value, {}, ts, c.asof_s))
                    ids = [ob.key for ob in obs]
                    if len(set(ids)) != len(ids) or seen_messages.intersection(ids):
                        raise ValueError("duplicate content")
                else:
                    values = _json(data)
                    if not isinstance(values, dict):
                        raise ValueError("objects")
                    for ident, value in sorted(values.items()):
                        if (gm_id(ident) if folder == "state" else _label_id(ident)) is None:
                            c.notes["pk_bad"] += 1
                            continue
                        if folder == "state":
                            if ident[:2] != Path(rel).stem:
                                raise ValueError("shard")
                            native, kind = _state(value), "lbls"
                        else:
                            if not isinstance(value, dict) or value.get("id") != ident \
                                    or not all(isinstance(value.get(k), str) for k in ("name", "type")):
                                raise ValueError("label")
                            native, kind = {k: value[k] for k in ("id", "name", "type", "_removedAt") if k in value}, "label"
                        obs.append(Obs(kind, f"gm:{kind}:{ident}", native, {}, src_asof_s=c.asof_s))
                if fsio.read_shared(manifest) != before:
                    raise ValueError("unstable")
                if folder == "exports":
                    seen_messages.update(ob.key for ob in obs)
                yield from obs
            except (OSError, ValueError, TypeError, AttributeError):
                raise ContainerError("error", "bad_file") from None

        c.iter_obs = read
        out.append(c)
    if out:
        for folder in ("exports", "state"):
            for path in (root / folder).rglob("*.json"):
                if path.relative_to(root).as_posix() not in referenced:
                    out[-1].notes["orphan_file"] += 1
    if fsio.read_shared(manifest) != before:
        raise ValueError("unstable")
    return out
