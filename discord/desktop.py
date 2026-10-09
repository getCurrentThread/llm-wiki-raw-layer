"""Token-free, read-only recovery of message JSON from Discord Desktop cache.

The scan strategy (embedded JSON plus bounded gzip streams) follows discrawl's
MIT-licensed discorddesktop scanner. See THIRD_PARTY.md for pinned provenance.
Cache eviction is never evidence of message deletion.
"""
import datetime as dt
import json
import os
from pathlib import Path
import re
import zlib
from tools.archiveio import CollectorError, atomic, load, locked, now
from .collector import CHANNEL_TYPES, _heads, dce_message
from .keys import snowflake_ms
from .readers.archive import msg_native

MAX_BYTES = 32 * 1024 * 1024


def default_path():
    return Path(os.environ.get("APPDATA", Path.home())) / "discord"


def json_values(data):
    text = data.decode("utf-8", "replace")
    decoder, pos = json.JSONDecoder(), 0
    # Starting only at JSON object/array prefixes avoids treating arbitrary
    # media bytes and cache headers as structured records.
    starts = re.compile(r'\{\s*"|\[\s*\{')
    while True:
        match = starts.search(text, pos)
        if not match:
            return
        try:
            obj, end = decoder.raw_decode(text, match.start())
            yield obj
            pos = end
        except (ValueError, RecursionError):
            pos = match.start() + 1


def payloads(data):
    yield data
    for match in re.finditer(b"\x1f\x8b\x08", data):
        try:
            decoder = zlib.decompressobj(31)
            value = decoder.decompress(data[match.start():], MAX_BYTES + 1)
            if decoder.eof and len(value) <= MAX_BYTES:
                yield value
        except zlib.error:
            continue


def scan(source):
    source = Path(source)
    if not source.is_dir():
        raise CollectorError("desktop_missing")
    # Deliberately exclude Local Storage, cookies, keychains and session files.
    roots = [source / "Cache/Cache_Data", source / "Service Worker/CacheStorage"]
    messages, channels, routes = {}, {}, {}
    stats = {"files_scanned": 0, "files_skipped": 0, "json_values": 0, "messages_found": 0,
             "unclassified_channels": 0, "source": "desktop", "coverage": "cached_only"}

    def visit(obj, mtime):
        if isinstance(obj, list):
            for child in obj:
                visit(child, mtime)
        elif isinstance(obj, dict):
            ident, cid, author = obj.get("id"), obj.get("channel_id"), obj.get("author")
            if snowflake_ms(ident) is not None and snowflake_ms(cid) is not None and isinstance(author, dict) \
                    and snowflake_ms(author.get("id")) is not None and isinstance(obj.get("content", ""), str):
                key = (cid, ident)
                rank = (obj.get("edited_timestamp") or obj.get("timestamp") or "", mtime)
                if key not in messages or rank > messages[key][0]:
                    messages[key] = (rank, obj)
            elif snowflake_ms(ident) is not None and "channel_id" not in obj and isinstance(obj.get("type"), int) and obj.get("type") in CHANNEL_TYPES \
                    and (obj.get("guild_id") or obj.get("type") in (1, 3) or "recipients" in obj):
                channels[ident] = obj
            for child in obj.values():
                if isinstance(child, (dict, list)):
                    visit(child, mtime)

    for root in roots:
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            try:
                if not path.resolve().is_relative_to(source.resolve()):
                    raise CollectorError("unsafe_path")
                before = path.stat()
                if not 0 < before.st_size <= MAX_BYTES:
                    stats["files_skipped"] += 1
                    continue
                data = path.read_bytes()
                after = path.stat()
                if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                    stats["files_skipped"] += 1
                    continue
                stats["files_scanned"] += 1
                for payload in payloads(data):
                    for match in re.finditer(rb"/channels/(@me|[0-9]{1,19})/([0-9]{1,19})", payload):
                        gid, cid = (v.decode("ascii") for v in match.groups())
                        routes[cid] = "0" if gid == "@me" else gid
                    if b'"author"' not in payload and b'"recipients"' not in payload and b'"guild_id"' not in payload:
                        continue
                    for obj in json_values(payload):
                        stats["json_values"] += 1
                        visit(obj, before.st_mtime_ns)
            except OSError:
                stats["files_skipped"] += 1
    grouped = {}
    for (cid, mid), (_, msg) in messages.items():
        grouped.setdefault(cid, []).append(msg)
        ch = channels.setdefault(cid, {"id": cid, "name": "", "type": 0})
        gid = msg.get("guild_id") or ch.get("guild_id") or routes.get(cid)
        if gid:
            ch["guild_id"] = gid
    stats["messages_found"] = len(messages)
    stats["unclassified_channels"] = sum(not c.get("guild_id") and c.get("type") not in (1, 3) for c in channels.values())
    return channels, grouped, stats


def sync(root, source, patterns=(), selected=(), max_items=0):
    channels, grouped, stats = scan(source)
    root = Path(root)
    stamp = now()
    stats.update(status="ok", channels=0, messages_written=0, partitions_written=0, errors=0, limited=False)
    with locked(root):
        man = load(root / "manifest.json", {"version": 1, "guilds": {}, "channels": {}})
        users = load(root / "users.json", {})
        for cid, values in sorted(grouped.items(), key=lambda p: int(p[0])):
            if selected and cid not in selected:
                continue
            if max_items and stats["channels"] >= max_items:
                stats["limited"] = True
                break
            raw = channels[cid]
            old = man["channels"].get(cid, {"id": cid, "files": []})
            gid = raw.get("guild_id") or ("0" if raw.get("type") in (1, 3) else old.get("guild_id", "cache_unknown"))
            guild = man["guilds"].setdefault(gid, {"id": gid, "name": "Direct Messages" if gid == "0" else "Cached guild"})
            name = raw.get("name") or old.get("name", "Cached channel")
            if any(p.casefold() in (guild["name"] + "\n" + name).casefold() for p in patterns):
                continue
            heads = _heads(root, old)
            delta = []
            for obj in sorted(values, key=lambda m: int(m["id"])):
                msg = dce_message(obj)
                previous = heads.get(msg["id"])
                if previous and (previous.get("_removedAt") or (previous.get("timestampEdited") or "") > (msg.get("timestampEdited") or "")):
                    continue
                if previous is None or msg_native(previous) != msg_native(msg):
                    delta.append(msg)
                author = obj["author"]
                uid = author["id"]
                user = dict(users.get(uid, {}), id=uid, name=author.get("global_name") or author.get("username", ""), isBot=bool(author.get("bot")))
                user.setdefault("nicks", {})
                users[uid] = user
            ch = {"id": cid, "type": CHANNEL_TYPES.get(raw.get("type", 0), "GuildTextChat"), "guild_id": gid,
                  "parent_id": raw.get("parent_id") if raw.get("type") in (10, 11, 12) else old.get("parent_id"),
                  "name": name, "topic": raw.get("topic", old.get("topic")), "category": old.get("category"),
                  "category_id": old.get("category_id"), "files": list(old["files"]),
                  "last_message_id": str(max([int(m["id"]) for m in values] + [int(old.get("last_message_id") or "0")])), "error": ""}
            for start in range(0, len(delta), 10000):
                chunk = delta[start:start + 10000]
                clock = dt.datetime.now(dt.timezone.utc)
                if ch["files"]:
                    last = dt.datetime.strptime(Path(ch["files"][-1]).stem, "%Y%m%dT%H%M%S%fZ").replace(tzinfo=dt.timezone.utc)
                    clock = max(clock, last + dt.timedelta(microseconds=1))
                bucket = old["files"][0].split("/")[1] if old["files"] else gid
                rel = f"exports/{bucket}/{cid}/{clock.strftime('%Y%m%dT%H%M%S%fZ')}.json"
                atomic(root, root / rel, {"guild": guild, "channel": {"id": cid, "name": name, "type": ch["type"]},
                                        "exportedAt": stamp, "messages": chunk, "messageCount": len(chunk)}, immutable=True)
                ch["files"].append(rel)
                stats["partitions_written"] += 1
            man["channels"][cid] = ch
            stats["channels"] += 1
            stats["messages_written"] += len(delta)
        atomic(root, root / "users.json", users)
        import uuid
        man["generation"] = uuid.uuid4().hex
        atomic(root, root / "manifest.json", man)
    return stats
