# -*- coding: utf-8 -*-
"""mm_archive (SPEC 7.4, 10.2): mattertok's archive directory -- manifest.json (channels), posts/**.jsonl (one
file per channel) and users.json.

The whole origin is read in one pass between two reads of manifest.json; if the manifest changed under us the
pass is repeated (origins.MultiVersionMixin, max 3) and otherwise the origin is `unstable`. The pass is diffed as
a whole by the multi-version rule (rawlayer.capture.multiver): a key may be observed several times (lines, files,
posts) and its versions are judged together (Q3)."""
import json
import os
from pathlib import Path

from rawlayer.capture import canon, fsio
from rawlayer.capture.model import Obs
from rawlayer.capture.multiver import OriginRead
from rawlayer.capture.origins import Unstable

from ..keys import mm_ts_ms

CH_WHITELIST = ("channel_id", "type", "team_name", "channel_name", "display", "inaccessible_before")


def _mm_id(v):
    """Mattermost ids are strings; anything else (or a string pk_text rejects) is pk_bad."""
    return canon.pk_text(v) if isinstance(v, str) else None


def _lines(data, notes):
    parts = data.split(b"\n")
    if parts[-1] != b"":
        notes["partial_tail"] += 1
    for ln in parts[:-1]:
        if ln.endswith(b"\r"):
            ln = ln[:-1]
        if not ln.strip():
            continue
        try:
            o = json.loads(ln.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            notes["bad_line"] += 1
            continue
        if not isinstance(o, dict):
            notes["bad_line"] += 1
            continue
        yield o


def excluded(c, patterns):
    """mattertok is_excluded_channel: display + LF + channel_name, casefolded, contains a pattern."""
    s = (str(c.get("display") or "") + "\n" + str(c.get("channel_name") or "")).casefold()
    return any(p in s for p in patterns)


def _safe_rel(path):
    """A manifest channel path stays below posts/: relative, '/'-separated, no empty / '.' / '..' parts."""
    if not isinstance(path, str) or not path or "\\" in path or ":" in path or "\x00" in path:
        return False
    return all(part not in ("", ".", "..") for part in path.split("/"))


def read_once(root, patterns, denylist, origin):
    root = Path(root)
    manifest = root / "manifest.json"
    rd = OriginRead(origin, denylist)
    man_bytes = fsio.read_shared(manifest)
    man = json.loads(man_bytes)
    channels = man.get("channels")
    if not isinstance(channels, dict):
        raise ValueError("manifest channels")
    referenced = set()
    plan = []
    for cid in sorted(channels):
        c = channels[cid]
        if not isinstance(c, dict):
            rd.notes["bad_line"] += 1
            continue
        if isinstance(c.get("path"), str):
            referenced.add(c["path"])       # an excluded channel's file is known, not an orphan
        if excluded(c, patterns):
            rd.notes["excluded"] += 1
            continue
        plan.append((cid, c))

    obs = rd.add
    # manifest.json container: ch records
    man_sid = canon.source_id("manifest.json")
    rd.srcmap[man_sid] = "manifest.json"
    rd.containers[man_sid] = {"status": "ok"}
    rd.asof[man_sid] = fsio.mtime_s(manifest)
    files = []
    for cid, c in plan:
        k = _mm_id(c.get("channel_id"))
        if k is None:
            rd.notes["pk_bad"] += 1
        else:
            native = {f: c[f] for f in CH_WHITELIST if f in c}
            obs(Obs("ch", f"mm:ch:{k}", native, {}, None, rd.asof[man_sid]), man_sid)
        path = c.get("path")
        if not _safe_rel(path):
            rd.notes["bad_line"] += 1      # missing, absolute, or leaving posts/ (traversal)
            continue
        files.append(("posts/" + path, cid, c))
    # channel files in relpath order
    for rel, cid, c in sorted(files, key=lambda t: t[0]):
        sid = canon.source_id(rel)
        rd.srcmap[sid] = rel
        p = root.joinpath(*rel.split("/"))
        try:
            data = fsio.read_shared(p)
        except FileNotFoundError:
            status = "ok" if c.get("post_count") == 0 else "missing"
            rd.containers[sid] = {"status": status}
            continue
        except OSError:
            rd.containers[sid] = {"status": "error"}
            rd.notes["read_error"] += 1
            continue
        rd.containers[sid] = {"status": "ok"}
        asof = rd.asof[sid] = fsio.mtime_s(p)
        for o in _lines(data, rd.notes):
            pid = _mm_id(o.get("id"))
            if pid is None:
                rd.notes["pk_bad"] += 1
                continue
            if o.get("channel_id") != cid:
                rd.notes["channel_mismatch"] += 1
            obs(Obs("post", f"mm:post:{pid}", o, {}, mm_ts_ms(o.get("create_at")), asof), sid)
            if "_files" in o or "_reactions" in o:
                rd.notes["slim_post"] += 1
                continue
            md = o.get("metadata")
            for f in (md.get("files") if isinstance(md, dict) else None) or []:
                fid = _mm_id(f.get("id")) if isinstance(f, dict) else None
                if fid is None:
                    rd.notes["pk_bad"] += 1
                    continue
                obs(Obs("file", f"mm:file:{fid}", f, {}, None, asof), sid)
    # posts/**.jsonl the manifest does not point at. os.walk, not Path.walk: Path.walk lists links and junctions as
    # files and never enters a junction, which would change the orphan count the manifest records.
    posts_root = root / "posts"
    for dp, _d, fs in os.walk(posts_root):
        for f in fs:
            if f.endswith(".jsonl"):
                rel = Path(dp, f).relative_to(posts_root).as_posix()
                if rel not in referenced:
                    rd.notes["orphan_file"] += 1
    # users.json container
    users_sid = canon.source_id("users.json")
    rd.srcmap[users_sid] = "users.json"
    up = root / "users.json"
    try:
        users = json.loads(fsio.read_shared(up))
        if not isinstance(users, dict):
            raise ValueError("users")
        rd.containers[users_sid] = {"status": "ok"}
        asof = rd.asof[users_sid] = fsio.mtime_s(up)
        for uid in sorted(users):
            k = _mm_id(uid)
            v = users[uid]
            if k is None or not isinstance(v, dict):
                rd.notes["pk_bad"] += 1
                continue
            native = {"id": uid}
            native.update(v)
            obs(Obs("user", f"mm:user:{k}", native, {}, None, asof), users_sid)
    except FileNotFoundError:
        rd.containers[users_sid] = {"status": "missing"}
    except (OSError, ValueError):
        rd.containers[users_sid] = {"status": "error"}
        rd.notes["read_error"] += 1
    if fsio.read_shared(manifest) != man_bytes:
        raise Unstable()
    return rd

