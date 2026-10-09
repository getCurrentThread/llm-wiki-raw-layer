"""DCE JSON partitions, observed in manifest order within one stable pass."""
import json
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from rawlayer.capture import canon, fsio
from rawlayer.capture.model import Obs
from rawlayer.capture.multiver import OriginRead
from rawlayer.capture.origins import Unstable
from ..keys import dc_id, snowflake_ms

CH_FIELDS = ("id", "type", "guild_id", "parent_id", "category_id", "category", "name", "topic")
MSG_FIELDS = ("id", "type", "timestamp", "timestampEdited", "isPinned", "content", "_removedAt")


def _reject_constant(value):
    raise ValueError("nonfinite JSON")


def _load(data):
    return json.loads(data, parse_constant=_reject_constant)


def _text_projection(value, field=""):
    if isinstance(value, list):
        return [_text_projection(v, field) for v in value]
    if isinstance(value, dict):
        return {k: _text_projection(v, k) for k, v in value.items()
                if k in ("title", "description", "text", "content", "name", "value", "fields",
                         "url", "author", "footer", "inline")}
    if field == "url" and isinstance(value, str) and value.startswith(("https://", "http://")):
        u = urlsplit(value)
        return urlunsplit((u.scheme, u.netloc, u.path, "", ""))
    return value


def msg_native(message):
    """Shared projection contract; display and CDN rotation never change the signature."""
    out = {f: message[f] for f in MSG_FIELDS if f in message}
    author = message.get("author")
    out["author"] = {"id": author.get("id")} if isinstance(author, dict) else {}
    for field, fields in (("attachments", ("id", "fileName", "fileSizeBytes")),
                          ("stickers", ("id", "name"))):
        values = message.get(field)
        out[field] = [{k: v[k] for k in fields if k in v} for v in values if isinstance(v, dict)] \
            if isinstance(values, list) else []
    for field in ("embeds", "forwardedMessage"):
        if field in message:
            out[field] = _text_projection(message[field])
    for field, fields in (("reference", ("messageId", "channelId", "guildId")),
                          ("interaction", ("id", "name"))):
        value = message.get(field)
        if isinstance(value, dict):
            out[field] = {k: value[k] for k in fields if k in value}
    return out


def _safe_rel(rel):
    return isinstance(rel, str) and rel.startswith("exports/") and rel.endswith(".json") \
        and not any(x in rel for x in ("\\", ":", "\x00")) \
        and all(p not in ("", ".", "..") for p in rel.split("/"))


def _inside(root, path):
    return path.resolve().is_relative_to(root.resolve())


def read_once(root, patterns, denylist, origin):
    root = Path(root)
    manifest = root / "manifest.json"
    before = fsio.read_shared(manifest)
    man = _load(before)
    if not isinstance(man, dict) or man.get("version") != 1 or not isinstance(man.get("channels"), dict) \
            or not isinstance(man.get("guilds"), dict):
        raise ValueError("manifest")
    rd = OriginRead(origin, denylist)
    referenced, guilds, first_sources = set(), set(), {}

    def container(rel, status="ok", path=None):
        sid = canon.source_id(rel)
        rd.srcmap[sid] = rel
        rd.containers[sid] = {"status": status}
        if status == "ok":
            rd.asof[sid] = fsio.mtime_s(path or manifest)
        return sid

    for cid, ch in sorted(man["channels"].items()):
        if not isinstance(ch, dict):
            raise ValueError("channel")
        files = ch.get("files")
        if not isinstance(files, list):
            files = []
            malformed = True
        else:
            malformed = False
        referenced.update(r for r in files if isinstance(r, str))
        gid = dc_id(ch.get("guild_id"))
        guild = man["guilds"].get(gid, {})
        if not isinstance(guild, dict):
            raise ValueError("guild")
        label = "\n".join(str(v or "") for v in (guild.get("name"), ch.get("category"), ch.get("name"))).casefold()
        if any(p in label for p in patterns):
            rd.notes["excluded"] += 1
            continue
        if dc_id(cid) is None or ch.get("id") != cid or gid is None:
            rd.notes["pk_bad"] += 1
            container("manifest.json#ch/" + cid, "error")
            continue
        # A failed partition invalidates every observation of this channel, including metadata.
        parsed, failure = [], "bad_file" if malformed else None
        valid_files = all(_safe_rel(r) for r in files)
        if not valid_files:
            failure = "bad_file"
        elif files != sorted(files) or len(set(files)) != len(files):
            failure = "order_bad"
        if failure is None:
            for rel in files:
                try:
                    path = root / rel
                    if not _inside(root / "exports", path):
                        raise ValueError("path")
                    obj = _load(fsio.read_shared(path))
                    messages = obj.get("messages")
                    if not isinstance(messages, list) or len(messages) > 10000 or not all(isinstance(m, dict) for m in messages):
                        raise ValueError("messages")
                    count = obj.get("messageCount")
                    if isinstance(count, bool) or not isinstance(count, int) or count != len(messages):
                        failure = "count_mismatch"
                        break
                    if not isinstance(obj.get("channel"), dict) or obj["channel"].get("id") != cid:
                        failure = "channel_mismatch"
                        break
                    # Projection may reject malformed URLs. Complete it before adding any
                    # channel observations so a bad partition cannot discard healthy channels.
                    projected = []
                    for msg in messages:
                        mid = dc_id(msg.get("id"))
                        ts_ms = snowflake_ms(mid)
                        projected.append((mid, ts_ms, msg_native(msg)) if ts_ms is not None else None)
                    parsed.append((rel, path, projected))
                except (OSError, ValueError, AttributeError):
                    failure = "bad_file"
                    break
        if failure:
            rd.notes[failure] += 1
            container("manifest.json#ch/" + cid, "error")
            for rel in files:
                if _safe_rel(rel):
                    container(rel, "error")
            continue
        sid = container("manifest.json#ch/" + cid)
        rd.add(Obs("ch", "dc:ch:" + cid, {k: ch[k] for k in CH_FIELDS if k in ch}, {}), sid)
        guilds.add(gid)
        for rel, path, messages in parsed:
            sid = container(rel, path=path)
            for msg in messages:
                if msg is None:
                    rd.notes["pk_bad"] += 1
                    continue
                mid, ts_ms, native = msg
                ob = Obs("msg", f"dc:msg:{cid}:{mid}", native, {}, ts_ms, rd.asof[sid])
                rd.add(ob, sid)
                # Immutable refresh copies keep the first source of this signature. The last
                # observation still selects the head (A -> B -> A), without source-only churn.
                signature = canon.record_h(ob.native, ob.derived)
                if ob.key in rd.keys:
                    source = first_sources.setdefault((ob.key, signature), sid)
                    rd.keys[ob.key][signature].src = source
    for gid in sorted(guilds):
        guild = man["guilds"].get(gid)
        if not isinstance(guild, dict) or guild.get("id") != gid:
            container("manifest.json#g/" + gid, "error")
            rd.notes["pk_bad"] += 1
            continue
        sid = container("manifest.json#g/" + gid)
        rd.add(Obs("guild", "dc:guild:" + gid, {k: guild[k] for k in ("id", "name") if k in guild}, {}), sid)
    for path in (root / "exports").rglob("*.json"):
        if path.relative_to(root).as_posix() not in referenced:
            rd.notes["orphan_file"] += 1
    sid = canon.source_id("users.json")
    try:
        path = root / "users.json"
        users = _load(fsio.read_shared(path))
        if not isinstance(users, dict):
            raise ValueError("users")
        container("users.json", path=path)
        for uid, user in sorted(users.items()):
            if dc_id(uid) is None or not isinstance(user, dict) or user.get("id") != uid:
                rd.notes["pk_bad"] += 1
                continue
            rd.add(Obs("user", "dc:user:" + uid, {k: user[k] for k in ("id", "name", "isBot", "nicks") if k in user}, {}), sid)
    except (OSError, ValueError):
        container("users.json", "error")
        rd.notes["bad_file"] += 1
    if fsio.read_shared(manifest) != before:
        raise Unstable()
    return rd
