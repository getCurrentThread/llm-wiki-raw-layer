"""One lazy, atomically replaced snapshot per container; tombstones retain all keys."""
import json
from pathlib import Path
from rawlayer.capture import canon, fsio
from rawlayer.capture.model import Container, ContainerError, Obs
from ..keys import nt_id, iso_ms
from .project import project


def _reject_constant(value):
    raise ValueError("nonfinite JSON")


def _objects(value):
    if isinstance(value, dict):
        values = value.values()
    elif isinstance(value, list):
        values = value
    else:
        raise ValueError("objects")
    result = {}
    for obj in values:
        if not isinstance(obj, dict) or nt_id(obj.get("id")) is None:
            raise ValueError("id")
        result[nt_id(obj["id"])] = project(obj)
    return result


def _page(obj, expected):
    page = obj.get("page")
    if not isinstance(page, dict) or nt_id(page.get("id")) != expected:
        raise ValueError("page")
    page = project(page)
    if "_gone_at" in obj:
        page["_gone_at"] = obj["_gone_at"]
    blocks, comments = _objects(obj.get("blocks", {})), _objects(obj.get("comments", {}))
    order, seen = [], set()
    if not isinstance(obj.get("order"), list):
        raise ValueError("order")
    for entry in obj["order"]:
        if not isinstance(entry, list) or len(entry) != 3:
            raise ValueError("order")
        bid, depth, parent = entry
        if parent is not None and nt_id(parent) is None:
            raise ValueError("order parent")
        bid, parent = nt_id(bid), nt_id(parent) if parent is not None else None
        if bid not in blocks or isinstance(depth, bool) or not isinstance(depth, int) or depth < 0:
            raise ValueError("order")
        if bid not in seen:
            order.append([bid, depth, parent])
            seen.add(bid)
    result = [Obs("page", "nt:page:" + expected, page, {"order": order}, iso_ms(page.get("last_edited_time")))]
    for kind, values in (("block", blocks), ("comment", comments)):
        for ident, value in sorted(values.items()):
            result.append(Obs(kind, f"nt:{kind}:{expected}:{ident}", value, {},
                              iso_ms(value.get("last_edited_time") or value.get("created_time"))))
    return result


def containers(root, origin="nt_archive"):
    root = Path(root)
    if not root.is_dir():
        raise OSError("archive missing")
    out = []
    paths = sorted((root / "pages").rglob("*.json")) + sorted((root / "ds").glob("*.json")) + [root / "users.json"]
    for path in paths:
        rel = path.relative_to(root).as_posix()
        c = Container(origin, rel, canon.source_id(rel))

        def read(path=path, c=c, rel=rel):
            try:
                if not path.resolve().is_relative_to(root.resolve()):
                    raise ValueError("path")
                obj = json.loads(fsio.read_shared(path), parse_constant=_reject_constant)
                c.asof_s = fsio.mtime_s(path)
                if rel.startswith("pages/"):
                    ident = nt_id(path.stem)
                    if ident is None or not isinstance(obj, dict):
                        raise ValueError("page")
                    values = _page(obj, ident)
                elif rel.startswith("ds/"):
                    ident = nt_id(path.stem)
                    if not isinstance(obj, dict) or ident is None or nt_id(obj.get("id")) != ident:
                        raise ValueError("ds")
                    values = [Obs("ds", "nt:ds:" + ident, project(obj), {})]
                else:
                    values = [Obs("user", "nt:user:" + ident, value, {}) for ident, value in sorted(_objects(obj).items())]
                for ob in values:
                    ob.src_asof_s = c.asof_s
                yield from values
            except (OSError, ValueError, AttributeError, TypeError):
                raise ContainerError("error", "bad_file") from None

        c.iter_obs = read
        out.append(c)
    return out
