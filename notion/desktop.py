"""Read-only SQLite snapshot of Notion Desktop's structured cache (no token).

Missing cache rows are retained. Only explicit alive=false/resolved flags retire
records. Internal CRDT-only bodies are reported as unsupported, never guessed.
"""
import contextlib
import datetime as dt
import json
import os
from pathlib import Path
import sqlite3
import uuid
from tools.archiveio import CollectorError, atomic, inside, load, locked, now
from .keys import nt_id
from .readers.project import project

TYPES = {"text": "paragraph", "header": "heading_1", "sub_header": "heading_2", "sub_sub_header": "heading_3",
         "header_4": "heading_3", "bulleted_list": "bulleted_list_item", "numbered_list": "numbered_list_item",
         "transclusion_container": "synced_block", "transclusion_reference": "synced_block"}
PAGE_TYPES = {"page", "collection_view_page", "personal_home_page"}


def default_path():
    return Path(os.environ.get("APPDATA", Path.home())) / "Notion/notion.db"


def _json(value, fallback):
    if value is None:
        return fallback
    if isinstance(value, (str, bytes)):
        try:
            return json.loads(value)
        except (ValueError, UnicodeError):
            raise CollectorError("bad_cache_json") from None
    return value


def rich_text(value):
    out = []
    for item in value if isinstance(value, list) else []:
        if not isinstance(item, list) or not item or not isinstance(item[0], str):
            continue
        text = item[0]
        annotations, link = {}, None
        for fmt in item[1] if len(item) > 1 and isinstance(item[1], list) else []:
            if isinstance(fmt, list) and fmt:
                key = {"b": "bold", "i": "italic", "s": "strikethrough", "_": "underline", "c": "code"}.get(fmt[0])
                if key:
                    annotations[key] = True
                elif fmt[0] == "a" and len(fmt) > 1:
                    link = {"url": fmt[1]}
        out.append({"type": "text", "text": {"content": text, "link": link}, "plain_text": text, "annotations": annotations})
    return out


def _time(value):
    if isinstance(value, (int, float)) and value > 0:
        return dt.datetime.fromtimestamp(value / 1000, dt.timezone.utc).isoformat().replace("+00:00", "Z")
    return None


def native_block(raw):
    props = raw.get("properties") or {}
    typ = TYPES.get(raw.get("type"), raw.get("type") or "unsupported")
    value = {"rich_text": rich_text(props.get("title", []))}
    if typ in ("image", "file", "pdf", "video", "audio"):
        source = props.get("source", [])
        url = source[0][0] if isinstance(source, list) and source and isinstance(source[0], list) else ""
        value = {"type": "external", "external": {"url": url}, "caption": rich_text(props.get("caption", []))}
        if typ == "file":
            title = rich_text(props.get("title", []))
            value["name"] = "".join(t["plain_text"] for t in title)
    elif typ == "equation":
        value = {"expression": "".join(t["plain_text"] for t in rich_text(props.get("title", [])))}
    elif typ == "table_row":
        value = {"cells": [rich_text(v) for _, v in sorted(props.items())]}
    elif typ in ("bookmark", "embed"):
        source = props.get("link") or props.get("source") or []
        value["url"] = source[0][0] if isinstance(source, list) and source and isinstance(source[0], list) else ""
    out = {"object": "block", "id": raw["id"], "type": typ, typ: value,
           "created_time": _time(raw.get("created_time")), "last_edited_time": _time(raw.get("last_edited_time")),
           "created_by": {"id": raw.get("created_by_id") or raw.get("created_by")},
           "last_edited_by": {"id": raw.get("last_edited_by_id") or raw.get("last_edited_by")},
           "in_trash": raw.get("alive") in (0, False)}
    return out


def read_cache(source):
    source = Path(source)
    if not source.is_file():
        raise CollectorError("desktop_missing")
    # SQLite backup includes the live WAL, unlike copying only notion.db.
    with contextlib.closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True, timeout=10)) as src, \
            contextlib.closing(sqlite3.connect(":memory:")) as db:
        src.backup(db)
        db.row_factory = sqlite3.Row
        available = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "block" not in available:
            raise CollectorError("unsupported_cache_schema")
        wanted = ("id", "type", "properties", "content", "parent_id", "parent_table", "alive", "created_time", "last_edited_time",
                  "created_by", "last_edited_by", "created_by_id", "last_edited_by_id", "format", "crdt_data", "crdt_format_version")
        columns = {r[1] for r in db.execute('PRAGMA table_info("block")')}
        selected = [c for c in wanted if c in columns]
        if not {"id", "type", "properties", "content"}.issubset(columns):
            raise CollectorError("unsupported_cache_schema")
        blocks, crdt_only = {}, 0
        for row in db.execute('SELECT ' + ','.join('"' + c + '"' for c in selected) + ' FROM block'):
            obj = dict(row)
            ident = nt_id(obj["id"])
            if not ident:
                continue
            obj["id"] = ident
            obj["properties"] = _json(obj.get("properties"), {})
            obj["content"] = _json(obj.get("content"), [])
            obj["format"] = _json(obj.get("format"), {})
            if obj.get("crdt_data") and not obj["properties"]:
                crdt_only += 1
                obj["_crdt_only"] = True
            obj.pop("crdt_data", None)
            blocks[ident] = obj
        users, comments, discussions = {}, {}, {}
        for table, target, fields in (("notion_user", users, ("id", "name", "given_name", "family_name")),
                                      ("comment", comments, ("id", "parent_id", "text", "created_time", "last_edited_time", "alive", "created_by_id")),
                                      ("discussion", discussions, ("id", "parent_id", "resolved"))):
            if table not in available:
                continue
            columns = {r[1] for r in db.execute('PRAGMA table_info("' + table + '")')}
            names = [c for c in fields if c in columns]
            for row in db.execute('SELECT ' + ','.join('"' + c + '"' for c in names) + ' FROM "' + table + '"'):
                obj = dict(row)
                if nt_id(obj.get("id")):
                    target[nt_id(obj["id"])] = obj
    return blocks, users, comments, discussions, crdt_only


def sync(root, source, selected=(), max_items=0):
    blocks, cached_users, cached_comments, discussions, crdt_only = read_cache(source)
    root, stamp = Path(root), now()
    selected = {nt_id(v) for v in selected}
    if None in selected:
        raise CollectorError("bad_page_id")
    stats = {"status": "ok", "source": "desktop", "coverage": "cached_only", "blocks_cached": len(blocks),
             "crdt_only_blocks": crdt_only, "pages_written": 0, "pages_scanned": 0, "missing_children": 0,
             "unsupported_pages": 0, "errors": 0, "limited": False}
    with locked(root):
        man = load(root / "manifest.json", {"version": 1, "pages": {}, "data_sources": {}})
        users = load(root / "users.json", {})
        for uid, raw in cached_users.items():
            name = raw.get("name") or " ".join(v for v in (raw.get("given_name"), raw.get("family_name")) if isinstance(v, str))
            users[uid] = {"object": "user", "id": uid, "name": name}
        atomic(root, root / "users.json", users)
        for pid, raw in blocks.items():
            if raw["type"] not in PAGE_TYPES or selected and pid not in selected:
                continue
            if max_items and stats["pages_scanned"] >= max_items:
                stats["limited"] = True
                break
            stats["pages_scanned"] += 1
            path = inside(root, root / "pages" / pid[:2] / (pid + ".json"))
            old = load(path)
            own, order, seen, missing, unsupported = {}, [], set(), 0, False
            def children(node, depth, parent):
                nonlocal missing, unsupported
                for ident in node.get("content", []):
                    bid = nt_id(ident)
                    if not bid or bid in seen:
                        continue
                    seen.add(bid)
                    child = blocks.get(bid)
                    if child is None:
                        missing += 1
                        continue
                    if child["type"] in PAGE_TYPES:
                        # Child pages have their own page-scoped containers.
                        obj = native_block(child)
                        obj["type"] = "child_page"
                        obj["child_page"] = {"title": "".join(t["plain_text"] for t in rich_text(child["properties"].get("title", [])))}
                    else:
                        if child.get("_crdt_only"):
                            unsupported = True
                            continue
                        obj = native_block(child)
                    own[bid] = obj
                    order.append([bid, depth, parent])
                    if child["type"] not in PAGE_TYPES and depth < 200:
                        children(child, depth + 1, bid)
            children(raw, 0, None)
            stats["missing_children"] += missing
            if unsupported or raw.get("_crdt_only"):
                # Keep readable siblings and any previously captured bodies.
                # Unsupported cached CRDT data must not erase known content.
                stats["unsupported_pages"] += 1
            page_native = native_block(raw)
            page_native = {k: v for k, v in page_native.items() if k not in ("type", TYPES.get(raw["type"], raw["type"]))}
            properties = {"Name": {"type": "title", "title": rich_text(raw["properties"].get("title", []))}}
            if raw.get("_crdt_only") and old:
                properties = old["page"].get("properties", properties)
            for key, value in raw["properties"].items():
                if key != "title":
                    text = rich_text(value)
                    if text:
                        properties[key] = {"type": "rich_text", "rich_text": text}
            page_native.update(object="page", properties=properties,
                               parent={"type": "page_id", "page_id": raw.get("parent_id")} if raw.get("parent_table") == "block" else {"type": "workspace", "workspace": True})
            comment_map = {}
            for cid, c in cached_comments.items():
                discussion = discussions.get(nt_id(c.get("parent_id")), {})
                parent = nt_id(discussion.get("parent_id"))
                if parent != pid and parent not in own:
                    continue
                obj = {"object": "comment", "id": cid, "rich_text": rich_text(_json(c.get("text"), [])),
                       "created_time": _time(c.get("created_time")), "last_edited_time": _time(c.get("last_edited_time")),
                       "created_by": {"id": c.get("created_by_id")}}
                if c.get("alive") in (0, False) or discussion.get("resolved") in (1, True):
                    obj["_removed_at"] = _time(c.get("last_edited_time")) or stamp
                comment_map[cid] = obj
            value = {"page": page_native, "blocks": own, "comments": comment_map, "order": order}
            if old:
                # Missing rows/order entries can be cache eviction. Preserve old
                # observations and relative order until API proves a deletion.
                for kind in ("blocks", "comments"):
                    for ident, obj in old.get(kind, {}).items():
                        value[kind].setdefault(ident, obj)
                present = {e[0] for e in order}
                value["order"] += [e for e in old.get("order", []) if e[0] not in present]
            if old is None or project(old) != project(value):
                atomic(root, path, value)
                stats["pages_written"] += 1
            man["pages"][pid] = {"path": path.relative_to(root).as_posix(), "title": "".join(t["plain_text"] for t in page_native["properties"]["Name"]["title"]),
                                  "parent": page_native["parent"], "last_edited_time": page_native.get("last_edited_time"),
                                  "in_trash": page_native["in_trash"], "fetched_at": stamp, "source": "desktop",
                                  "cache_complete": missing == 0 and not unsupported and not raw.get("_crdt_only")}
        man["generation"] = uuid.uuid4().hex
        atomic(root, root / "manifest.json", man)
    return stats
