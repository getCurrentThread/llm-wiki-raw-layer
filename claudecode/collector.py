"""Read-only Claude Code transcript projection into immutable archive partitions."""
import datetime as dt
import json
from pathlib import Path
import uuid

from rawlayer.capture.canon import pk_text
from tools import archiveio
from tools.archiveio import CollectorError
from .keys import iso_ms


PREFIXES = ("<local-command-stdout>", "<bash-stdout>", "<bash-stderr>",
            "<local-command-stderr>", "<task-notification>")
DAY_MS = 24 * 60 * 60 * 1000


def _text(value):
    return value if isinstance(value, str) else ""


def _tool(block):
    name = _text(block.get("name"))
    args = block.get("input")
    args = args if isinstance(args, dict) else {}
    if name in ("Bash", "PowerShell"):
        summary = " | ".join(s for s in (_text(args.get("description")),
                                         _text(args.get("command"))[:200]) if s)
    else:
        summary = next((_text(args.get(k))[:200] for k in
                        ("file_path", "path", "pattern", "url", "query", "description", "prompt")
                        if _text(args.get(k))), "")
    return {"name": name, "summary": summary}


def _transcript(obj, n, agent):
    role = obj.get("type")
    message = obj.get("message")
    if role not in ("user", "assistant") or not isinstance(message, dict) \
            or obj.get("isMeta") is True or obj.get("isCompactSummary") is True:
        return None
    content = message.get("content")
    if isinstance(content, str):
        text, tools = content, []
    elif isinstance(content, list):
        blocks = [b for b in content if isinstance(b, dict)]
        if role == "user" and blocks and all(b.get("type") == "tool_result" for b in blocks):
            return None
        text = "\n\n".join(_text(b.get("text")) for b in blocks if b.get("type") == "text")
        tools = [_tool(b) for b in blocks if b.get("type") == "tool_use"]
    else:
        return None
    if text.lstrip().startswith(PREFIXES) or not (text or tools):
        return None
    return {"session": _text(obj.get("sessionId")), "agent": agent,
            "uuid": _text(obj.get("uuid")), "parent": _text(obj.get("parentUuid")),
            "n": n, "ts": _text(obj.get("timestamp")), "role": role,
            "cwd": _text(obj.get("cwd")), "branch": _text(obj.get("gitBranch")),
            "model": _text(message.get("model")) if role == "assistant" else "",
            "text": text, "tools": tools, "from": "transcript"}


def _history(obj, sessions, clock):
    session, timestamp = _text(obj.get("sessionId")), obj.get("timestamp")
    if session in sessions or isinstance(timestamp, bool) or not isinstance(timestamp, int) \
            or clock - timestamp <= DAY_MS:
        return None
    try:
        stamp = (dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc) +
                 dt.timedelta(milliseconds=timestamp)).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    except (OverflowError, ValueError):
        return None
    return {"session": session, "agent": "", "uuid": f"h{timestamp}.{session}", "parent": "",
            "n": timestamp, "ts": stamp, "role": "prompt", "cwd": _text(obj.get("project")),
            "branch": "", "model": "", "text": _text(obj.get("display")), "tools": [], "from": "history"}


def _sources(source):
    for path in sorted((source / "projects").glob("*/*.jsonl")):
        yield path, path.stem, "session", path.stem, "", path.parent.name
    for path in sorted((source / "projects").glob("*/*/subagents/*.jsonl")):
        session = path.parent.parent.name
        yield path, f"{session}.{path.stem}", "subagent", session, path.stem, path.parents[2].name
    # Workflow agents; a resumed workflow copies an agent's file, so both copies share its container
    for path in sorted((source / "projects").glob("*/*/subagents/workflows/*/agent-*.jsonl")):
        session = path.parents[3].name
        yield path, f"{session}.{path.stem}", "subagent", session, path.stem, path.parents[4].name
    path = source / "history.jsonl"
    if path.exists():
        yield path, "history", "history", "", "", ""


def _sync(root, claude_dir, max_items=0, full=False, clock=None):
    root, source = Path(root), Path(claude_dir)
    stats = {"status": "ok", "files_seen": 0, "files_changed": 0, "files_gone": 0,
             "rows_written": 0, "partitions_written": 0, "errors": 0, "lines_bad": 0, "limited": False}
    current = dt.datetime.now(dt.timezone.utc).timestamp() * 1000 if clock is None else clock
    with archiveio.locked(root):
        man = archiveio.load(root / "manifest.json", {"version": 1, "files": {}, "containers": {}})
        sources = list(_sources(source))
        seen = {p.relative_to(source).as_posix() for p, *_ in sources}
        stats["files_seen"] = len(sources)
        stats["files_gone"] = len(set(man["files"]) - seen)
        sessions = {c["session"] for c in man["containers"].values() if c["kind"] != "history"}
        sessions.update(session for _, cid, kind, session, _, _ in sources
                        if kind != "history" and pk_text(cid) is not None)
        for path, cid, kind, session, agent, project in sources:
            if pk_text(cid) is None:
                stats["errors"] += 1
                continue
            rel = path.relative_to(source).as_posix()
            try:
                if not path.resolve().is_relative_to(source.resolve()):
                    raise CollectorError("unsafe_path")
                before = path.stat()
                fingerprint = {"size": before.st_size, "mtime_ns": before.st_mtime_ns, "container": cid}
                if not full and man["files"].get(rel) == fingerprint:
                    continue
                if max_items and stats["files_changed"] >= max_items:
                    stats["limited"] = True
                    break
                stats["files_changed"] += 1
                data = path.read_bytes()
            except FileNotFoundError:
                stats["files_gone"] += 1
                continue
            except (OSError, ValueError, RecursionError):
                stats["errors"] += 1
                continue
            meta = {}
            if agent:
                try:
                    meta = archiveio.load(path.with_suffix(".meta.json"), {})
                except (OSError, ValueError, RecursionError):
                    pass
                if not isinstance(meta, dict):
                    meta = {}
            old = man["containers"].get(cid, {"files": []})
            known = set()
            for partition in old["files"]:
                try:
                    stored = archiveio.load(archiveio.inside(root, root / partition))
                    known.update(row["uuid"] for row in stored["rows"])
                except (OSError, ValueError, TypeError, KeyError, RecursionError):
                    raise CollectorError("bad_archive") from None
            rows = []
            for n, line in enumerate(data.split(b"\n")[:-1]):
                try:
                    obj = json.loads(line)
                except (ValueError, UnicodeError, RecursionError):
                    stats["lines_bad"] += 1
                    continue
                if not isinstance(obj, dict):
                    continue
                if kind == "history" and pk_text(_text(obj.get("sessionId"))) is None:
                    stats["lines_bad"] += 1
                    continue
                row = _history(obj, sessions, current) if kind == "history" else _transcript(obj, n, agent)
                if row is not None and (pk_text(row["uuid"]) is None or pk_text(row["session"]) is None
                                        or iso_ms(row["ts"]) is None):
                    stats["lines_bad"] += 1
                    continue
                if row is not None and row["uuid"] not in known:
                    rows.append(row)
                    known.add(row["uuid"])
            container = {"id": cid, "kind": kind, "session": session, "agent": agent, "project": project,
                         "agent_type": _text(meta.get("agentType")) or old.get("agent_type", ""),
                         "description": _text(meta.get("description")) or old.get("description", ""),
                         "files": list(old["files"])}
            for start in range(0, len(rows), 10000):
                chunk = rows[start:start + 10000]
                stamp = dt.datetime.now(dt.timezone.utc)
                if container["files"]:
                    last = dt.datetime.strptime(Path(container["files"][-1]).stem, "%Y%m%dT%H%M%S%fZ").replace(tzinfo=dt.timezone.utc)
                    stamp = max(stamp, last + dt.timedelta(microseconds=1))
                partition = f"exports/{cid}/{stamp.strftime('%Y%m%dT%H%M%S%fZ')}.json"
                archiveio.atomic(root, root / partition,
                                 {"container": cid, "exportedAt": archiveio.now(), "rows": chunk,
                                  "rowCount": len(chunk)}, immutable=True)
                container["files"].append(partition)
                stats["partitions_written"] += 1
            stats["rows_written"] += len(rows)
            man["containers"][cid] = container
            man["files"][rel] = fingerprint
        man["generation"] = uuid.uuid4().hex
        archiveio.atomic(root, root / "manifest.json", man)
    return stats


def sync(root, claude_dir, max_items=0, full=False, clock=None):
    try:
        return _sync(root, claude_dir, max_items, full, clock)
    except CollectorError:
        raise
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        raise CollectorError("archive_error") from None


def sync_config(cfg, auth_only=False, full=False, max_items=0):
    value = getattr(cfg, "cc_claude_dir", "")
    try:
        source = Path(value)
        ready = isinstance(value, (str, Path)) and source.is_absolute() and source.is_dir()
    except (TypeError, ValueError, OSError):
        ready = False
    if not ready:
        raise CollectorError("source_missing")
    if auth_only:
        return {"status": "local_ready", "authentication_required": False}
    return sync(cfg.sources["cc_archive"], source, max_items, full)


if __name__ == "__main__":
    raise SystemExit(archiveio.main("claudecode", sync_config))
