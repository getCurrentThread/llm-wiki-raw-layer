"""Read-only Discord API v10 collector; immutable DCE-shaped JSON partitions."""
import copy
import datetime as dt
import json
import os
from pathlib import Path
import re
import time
from urllib import error, parse, request
import uuid
from tools.archiveio import CollectorError, atomic, bind, inside, load, locked, now
from .keys import snowflake_ms
from .readers.archive import _safe_rel, msg_native

CHANNEL_TYPES = {0: "GuildTextChat", 1: "DirectTextChat", 2: "GuildVoiceChat", 3: "DirectGroupTextChat",
                 4: "GuildCategory", 5: "GuildNewsChat", 10: "GuildNewsThread", 11: "GuildPublicThread",
                 12: "GuildPrivateThread", 13: "GuildStageVoice", 15: "GuildForum", 16: "GuildMedia"}
MESSAGE_TYPES = {0: "Default", 6: "ChannelPinnedMessage", 19: "Reply", 20: "ChatInputCommand",
                 21: "ThreadStarterMessage", 23: "ContextMenuCommand"}


def _id(value):
    if snowflake_ms(value) is None:
        raise CollectorError("bad_response")
    return value


TOKEN_VARS = ("DISCORD_BOT_TOKEN", "DISCORD_TOKEN")


def token_path(configured):
    """The token file as a Path: absolute, under the data parent, in a folder that passes capture's precheck.
    Raises CollectorError("unsafe_token_path") or precheck's ConfigError."""
    from rawlayer.capture import paths, precheck
    path = Path(configured)
    if not configured or not path.is_absolute() or \
            not path.resolve().is_relative_to(Path(paths.DATA_PARENT).resolve()):
        raise CollectorError("unsafe_token_path")
    precheck.check(path.parent)
    return path


def machine_token_vars():
    """The TOKEN_VARS set machine-wide (what SYSTEM sees, and what token() prefers to the file); [] off Windows."""
    if os.name != "nt":
        return []
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment") as k:
            found = []
            for name in TOKEN_VARS:
                try:
                    if winreg.QueryValueEx(k, name)[0]:
                        found.append(name)
                except OSError:
                    pass
            return found
    except OSError:
        return []


def token(configured=""):
    value = os.environ.get("DISCORD_BOT_TOKEN") or os.environ.get("DISCORD_TOKEN")
    if not value and configured:
        path = token_path(configured)
        try:
            value = path.read_text(encoding="utf-8").strip()
        except OSError:
            raise CollectorError("auth_required") from None
    if not value or any(x in value for x in ("\n", "\r", "\x00")):
        raise CollectorError("auth_required")
    return value


class DiscordClient:
    def __init__(self, credential, token_type="auto", opener=request.urlopen, sleep=time.sleep):
        if token_type not in ("auto", "bot", "user"):
            raise CollectorError("bad_token_type")
        self.credential = credential.removeprefix("Bot ")
        self.token_type, self.opener, self.sleep = token_type, opener, sleep
        self.requests, self.wait_until, self.is_bot = 0, 0.0, None

    def request(self, path, query=None, bot=None):
        if not re.fullmatch(r"/[A-Za-z0-9_/@-]+", path):
            raise CollectorError("bad_endpoint")
        if bot is None:
            bot = self.is_bot if self.is_bot is not None else self.token_type != "user"
        auth = ("Bot " if bot else "") + self.credential
        url = "https://discord.com/api/v10" + path
        if query:
            url += "?" + parse.urlencode(query)
        req = request.Request(url, headers={"Authorization": auth, "User-Agent": "DiscordBot (https://github.com/Tyrrrz/DiscordChatExporter, 1.0)"})
        for attempt in range(6):
            self.sleep(max(0, self.wait_until - time.monotonic()))
            try:
                self.requests += 1
                with self.opener(req, timeout=60) as response:
                    value = json.loads(response.read())
                    if response.headers.get("X-RateLimit-Remaining") == "0":
                        self.wait_until = time.monotonic() + float(response.headers.get("X-RateLimit-Reset-After", "1"))
                    return value
            except error.HTTPError as e:
                status = e.code
                retry = e.headers.get("Retry-After")
                try:
                    body = json.loads(e.read())
                    retry = body.get("retry_after", retry) if isinstance(body, dict) else retry
                except (ValueError, OSError):
                    pass
                finally:
                    e.close()
                if status in (429, 500, 502, 503, 504) and attempt < 5:
                    self.sleep(min(120, float(retry or 2 ** attempt)))
                    continue
                raise CollectorError("auth_failed" if status == 401 else "api_error", status) from None
            except (error.URLError, OSError, ValueError):
                raise CollectorError("transport_error") from None
        raise CollectorError("retry_exhausted")

    def authenticate(self):
        try:
            me = self.request("/users/@me", bot=self.token_type != "user")
        except CollectorError as e:
            if self.token_type != "auto" or e.status != 401:
                raise
            me = self.request("/users/@me", bot=False)
        if not isinstance(me, dict):
            raise CollectorError("bad_response")
        _id(me.get("id"))
        self.is_bot = bool(me.get("bot"))
        return me

    def guilds(self):
        cursor = None
        while True:
            q = {"limit": 200}
            if cursor:
                q["after"] = cursor
            rows = self.request("/users/@me/guilds", q)
            if not isinstance(rows, list):
                raise CollectorError("bad_response")
            yield from rows
            if len(rows) < 200:
                return
            next_cursor = max((_id(v.get("id")) for v in rows), key=int)
            if cursor is not None and int(next_cursor) <= int(cursor):
                raise CollectorError("pagination_stalled")
            cursor = next_cursor

    def archived(self, cid, kind, joined=False):
        cursor, seen = None, set()
        route = f"/channels/{cid}/" + ("users/@me/" if joined else "") + f"threads/archived/{kind}"
        while True:
            q = {"limit": 100}
            if cursor:
                q["before"] = cursor
            obj = self.request(route, q)
            if not isinstance(obj, dict) or not isinstance(obj.get("threads"), list):
                raise CollectorError("bad_response")
            yield from obj["threads"]
            if not obj.get("has_more"):
                return
            if not obj["threads"]:
                raise CollectorError("pagination_stalled")
            last = obj["threads"][-1]
            cursor = last.get("id") if joined else last.get("thread_metadata", {}).get("archive_timestamp")
            if not isinstance(cursor, str) or cursor in seen:
                raise CollectorError("pagination_stalled")
            seen.add(cursor)

    def messages(self, cid, lower=0):
        # The API returns newest first. Backward paging avoids skipping a burst of
        # >100 new messages regardless of the undocumented ordering of `after`.
        before = None
        while True:
            q = {"limit": 100}
            if before:
                q["before"] = before
            rows = self.request(f"/channels/{cid}/messages", q)
            if not isinstance(rows, list):
                raise CollectorError("bad_response")
            if not rows:
                return
            ids = [int(_id(v.get("id"))) for v in rows]
            if before and max(ids) >= int(before):
                raise CollectorError("pagination_stalled")
            for obj, ident in zip(rows, ids):
                if ident > lower:
                    yield obj
            if min(ids) <= lower or len(rows) < 100:
                return
            before = str(min(ids))


def dce_message(obj):
    """Port the DCE JSON field mapping used by the raw-layer projection contract."""
    ident = _id(obj.get("id"))
    author = obj.get("author")
    if not isinstance(author, dict):
        raise CollectorError("bad_response")
    _id(author.get("id"))
    out = {"id": ident, "type": MESSAGE_TYPES.get(obj.get("type", 0), str(obj.get("type"))),
           "timestamp": obj.get("timestamp"), "timestampEdited": obj.get("edited_timestamp"),
           "isPinned": bool(obj.get("pinned")), "content": obj.get("content", ""),
           "author": {"id": author["id"], "name": author.get("username", ""), "isBot": bool(author.get("bot"))},
           "attachments": [{"id": a.get("id"), "fileName": a.get("filename"),
                            "fileSizeBytes": a.get("size"), "url": a.get("url")} for a in obj.get("attachments", [])],
           "embeds": copy.deepcopy(obj.get("embeds", [])),
           "stickers": [{"id": s.get("id"), "name": s.get("name")} for s in obj.get("sticker_items", [])]}
    ref = obj.get("message_reference")
    if isinstance(ref, dict):
        out["reference"] = {"messageId": ref.get("message_id"), "channelId": ref.get("channel_id"), "guildId": ref.get("guild_id")}
    interaction = obj.get("interaction_metadata") or obj.get("interaction")
    if isinstance(interaction, dict):
        out["interaction"] = {"id": interaction.get("id"), "name": interaction.get("name", "")}
    snapshots = obj.get("message_snapshots")
    if isinstance(snapshots, list):
        out["forwardedMessage"] = [{"content": s.get("message", {}).get("content", "")} for s in snapshots]
    return out


def _heads(root, ch):
    heads = {}
    files = ch.get("files", [])
    if not isinstance(files, list) or files != sorted(files) or len(set(files)) != len(files):
        raise CollectorError("bad_archive")
    for rel in files:
        if not _safe_rel(rel):
            raise CollectorError("unsafe_path")
        obj = load(inside(root, Path(root) / rel))
        if not isinstance(obj, dict) or obj.get("channel", {}).get("id") != ch["id"] \
                or not isinstance(obj.get("messages"), list) or type(obj.get("messageCount")) is not int \
                or obj["messageCount"] != len(obj["messages"]):
            raise CollectorError("bad_archive")
        for msg in obj["messages"]:
            heads[_id(msg.get("id"))] = msg
    return heads


def discover(client, selected=()):
    guilds, channels, errors = {}, {}, 0
    if selected:
        for cid in selected:
            _id(cid)
            ch = client.request(f"/channels/{cid}")
            channels[cid] = ch
            gid = ch.get("guild_id", "0")
            if gid != "0" and gid not in guilds:
                guilds[gid] = client.request(f"/guilds/{gid}")
        return guilds, channels, errors
    for guild in client.guilds():
        gid = _id(guild.get("id"))
        guilds[gid] = guild
        try:
            base = client.request(f"/guilds/{gid}/channels")
            if not isinstance(base, list):
                raise CollectorError("bad_response")
            for ch in base:
                ch = dict(ch, guild_id=gid)
                channels[_id(ch.get("id"))] = ch
            active = client.request(f"/guilds/{gid}/threads/active")
            for ch in active.get("threads", []):
                channels[_id(ch.get("id"))] = dict(ch, guild_id=gid)
            for ch in base:
                if ch.get("type") not in (0, 5, 15, 16):
                    continue
                for kind, joined in (("public", False), ("private", True)):
                    try:
                        for thread in client.archived(ch["id"], kind, joined):
                            channels[_id(thread.get("id"))] = dict(thread, guild_id=gid)
                    except CollectorError as e:
                        if e.status not in (403, 404):
                            errors += 1
        except CollectorError:
            errors += 1
    if not client.is_bot:
        for ch in client.request("/users/@me/channels"):
            channels[_id(ch.get("id"))] = ch
    return guilds, channels, errors


def sync(root, client, channel_ids=(), refresh_days=7, patterns=(), max_items=0):
    root, stamp = Path(root), now()
    stats = {"status": "ok", "channels": 0, "messages_written": 0, "tombstones": 0,
             "partitions_written": 0, "errors": 0, "limited": False}
    with locked(root):
        me = client.authenticate()
        bind(root, me["id"])
        man = load(root / "manifest.json", {"version": 1, "guilds": {}, "channels": {}})
        if man.get("version") != 1 or not isinstance(man.get("channels"), dict) or not isinstance(man.get("guilds"), dict):
            raise CollectorError("bad_archive")
        users = load(root / "users.json", {})
        guilds, discovered, errors = discover(client, channel_ids)
        stats["errors"] += errors
        for gid, g in guilds.items():
            man["guilds"][gid] = {"id": gid, "name": g.get("name", "")}
        man["guilds"].setdefault("0", {"id": "0", "name": "Direct Messages"})
        cutoff_ms = int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000) - refresh_days * 86400000
        cutoff_id = max(0, (cutoff_ms - 1420070400000) << 22)
        for cid, raw in sorted(discovered.items(), key=lambda p: int(p[0])):
            if raw.get("type") == 4:
                continue
            gid = raw.get("guild_id", "0")
            parent_raw = discovered.get(raw.get("parent_id"), {})
            if raw.get("type") in (10, 11, 12) and not parent_raw and raw.get("parent_id"):
                try:
                    parent_raw = client.request(f"/channels/{_id(raw['parent_id'])}")
                except CollectorError:
                    parent_raw = {}
            category_id = parent_raw.get("parent_id") if raw.get("type") in (10, 11, 12) else raw.get("parent_id")
            category = discovered.get(category_id, {}).get("name")
            label = "\n".join(str(v or "") for v in (man["guilds"].get(gid, {}).get("name"), category, raw.get("name"))).casefold()
            if any(p.casefold() in label for p in patterns):
                continue
            if max_items and stats["channels"] >= max_items:
                stats["limited"] = True
                break
            old = man["channels"].get(cid, {"id": cid, "files": [], "last_message_id": "0"})
            try:
                heads = _heads(root, old)
                high = max((int(k) for k in heads), default=0)
                lower = min(high, cutoff_id) if refresh_days and high else high
                messages = [] if raw.get("type") in (15, 16) else list(client.messages(cid, lower))
                fresh = {_id(m.get("id")): dce_message(m) for m in messages}
                if refresh_days and not fresh and any(lower < int(mid) <= high and not msg.get("_removedAt")
                                                     for mid, msg in heads.items()):
                    # READ_MESSAGE_HISTORY loss returns [] rather than an error.
                    # An entirely empty refresh cannot establish mass deletion.
                    raise CollectorError("empty_refresh")
                if messages and all(m.get("type", 0) == 0 and not any(m.get(field) for field in
                                   ("content", "attachments", "embeds", "sticker_items", "message_snapshots")) for m in messages):
                    raise CollectorError("message_content_unavailable")
                delta = [m for mid, m in sorted(fresh.items(), key=lambda p: int(p[0]))
                         if mid not in heads or msg_native(m) != msg_native(heads[mid])]
                if refresh_days and high:
                    for mid, msg in heads.items():
                        if lower < int(mid) <= high and mid not in fresh and not msg.get("_removedAt"):
                            delta.append(dict(msg, _removedAt=stamp))
                            stats["tombstones"] += 1
                ch = {"id": cid, "type": CHANNEL_TYPES.get(raw.get("type"), str(raw.get("type"))), "guild_id": gid,
                      "parent_id": raw.get("parent_id") if raw.get("type") in (10, 11, 12) else None,
                      "category_id": category_id, "category": category, "name": raw.get("name", ""), "topic": raw.get("topic"),
                      "files": list(old["files"]), "last_message_id": str(max(high, max((int(k) for k in fresh), default=0))), "error": ""}
                for obj in messages:
                    author = obj["author"]
                    uid = _id(author.get("id"))
                    user = dict(users.get(uid, {}), id=uid, name=author.get("global_name") or author.get("username", ""), isBot=bool(author.get("bot")))
                    user.setdefault("nicks", {})
                    if obj.get("member", {}).get("nick"):
                        user["nicks"][gid] = obj["member"]["nick"]
                    users[uid] = user
                for start in range(0, len(delta), 10000):
                    chunk = delta[start:start + 10000]
                    name = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
                    # Clock regression must not reorder immutable versions.
                    if ch["files"] and name <= Path(ch["files"][-1]).name:
                        last = dt.datetime.strptime(Path(ch["files"][-1]).stem, "%Y%m%dT%H%M%S%fZ")
                        name = (last + dt.timedelta(microseconds=1)).strftime("%Y%m%dT%H%M%S%fZ")
                    bucket = old["files"][0].split("/")[1] if old["files"] else gid
                    rel = f"exports/{bucket}/{cid}/{name}.json"
                    partition = {"guild": man["guilds"].get(gid, {"id": gid, "name": ""}), "channel": {"id": cid, "name": ch["name"], "type": ch["type"]},
                                 "exportedAt": stamp, "messageCount": len(chunk), "messages": chunk}
                    atomic(root, root / rel, partition, immutable=True)
                    ch["files"].append(rel)
                    stats["partitions_written"] += 1
                man["channels"][cid] = ch
                stats["channels"] += 1
                stats["messages_written"] += len(delta)
            except (CollectorError, ValueError, TypeError, KeyError):
                stats["errors"] += 1
                # Preserve known partitions/cursor; a failed or incomplete fetch
                # must never infer deletion or advance the channel watermark.
                if cid in man["channels"]:
                    man["channels"][cid]["error"] = "collection_failed"
        atomic(root, root / "users.json", users)
        man["generation"] = uuid.uuid4().hex
        atomic(root, root / "manifest.json", man)
    stats["requests"] = getattr(client, "requests", 0)
    stats["token_type"] = "bot" if client.is_bot else "user"
    return stats


def sync_config(cfg, auth_only=False, full=False, max_items=0):
    if cfg.dc_source == "desktop":
        from . import desktop
        source = cfg.dc_desktop_path or desktop.default_path()
        if auth_only:
            if not Path(source).is_dir():
                raise CollectorError("desktop_missing")
            return {"status": "local_ready", "authentication_required": False}
        return desktop.sync(cfg.sources["dc_archive"], source, cfg.dc_exclude, cfg.dc_channel_ids, max_items)
    client = DiscordClient(token(cfg.dc_token_file), cfg.dc_token_type)
    if auth_only:
        client.authenticate()
        return {"status": "authenticated", "token_type": "bot" if client.is_bot else "user"}
    return sync(cfg.sources["dc_archive"], client, cfg.dc_channel_ids, cfg.dc_refresh_days, cfg.dc_exclude, max_items)


if __name__ == "__main__":
    from tools.archiveio import main
    raise SystemExit(main("discord", sync_config))
