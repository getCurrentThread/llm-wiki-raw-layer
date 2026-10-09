"""Read-only Notion API collection via the official ntn executable/keychain."""
import copy
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
import uuid
from tools.archiveio import CollectorError, atomic, bind, inside, load, locked, now
from .keys import nt_id, iso_ms
from .readers.project import project
from .search import page_title

API_VERSION = "2026-03-11"


def executable(configured=""):
    if configured:
        path = Path(configured)
        if not path.is_file() or path.suffix.lower() != ".exe" and os.name == "nt":
            raise CollectorError("cli_missing")
        return str(path)
    found = shutil.which("ntn.exe" if os.name == "nt" else "ntn")
    if found:
        return found
    if os.name == "nt":
        path = Path(os.environ.get("APPDATA", "")) / "npm/node_modules/ntn/dist/ntn-win32-x64/ntn.exe"
        if path.is_file():
            return str(path)
    raise CollectorError("cli_missing")


class NotionClient:
    def __init__(self, cli="", runner=subprocess.run, sleep=time.sleep):
        self.cli, self.runner, self.sleep = executable(cli), runner, sleep
        self.requests, self.last = 0, 0.0

    def request(self, path, method="GET", body=None, query=None):
        if not re.fullmatch(r"/v1/[A-Za-z0-9_%/-]+", path):
            raise CollectorError("bad_endpoint")
        argv = [self.cli, "api", path, "-X", method, "--notion-version", API_VERSION]
        if body is not None:
            argv += ["--data", "@-"]
        argv += [f"{k}=={v}" for k, v in (query or {}).items()]
        # Pin production; a caller's debug URL must never receive keychain credentials.
        env = {k: v for k, v in os.environ.items() if k not in ("NOTION_API_BASE_URL", "NOTION_ENV")}
        env.update(NOTION_API_BASE_URL="https://api.notion.com", NOTION_ENV="prod")
        for attempt in range(6):
            self.sleep(max(0, 0.35 - (time.monotonic() - self.last)))
            self.last = time.monotonic()
            try:
                r = self.runner(argv, input=json.dumps(body) if body is not None else "", env=env,
                                capture_output=True, text=True, encoding="utf-8", timeout=90)
            except (OSError, subprocess.SubprocessError):
                raise CollectorError("transport_error") from None
            self.requests += 1
            try:
                value = json.loads(r.stdout)
            except ValueError:
                value = None
            if isinstance(value, dict) and value.get("object") != "error" and r.returncode == 0:
                return value
            detail = (r.stderr + " " + r.stdout).lower()
            status = value.get("status") if isinstance(value, dict) else None
            if status is None:
                match = re.search(r"\b(400|401|403|404|409|429|500|502|503|504|529)\b", detail)
                status = int(match[1]) if match else None
            if status in (429, 500, 502, 503, 504, 529) and attempt < 5:
                delay = re.search(r"retry[-_ ]after[^0-9]*(\d+(?:\.\d+)?)", detail)
                self.sleep(min(120, float(delay[1]) if delay else 2 ** attempt))
                continue
            if status == 401 or status is None and any(x in detail for x in ("not logged", "login", "credential", "token")):
                raise CollectorError("auth_required", status)
            raise CollectorError("api_error", status)
        raise CollectorError("retry_exhausted")

    def items(self, path, method="GET", body=None, query=None):
        cursor, seen = None, set()
        while True:
            b, q = dict(body or {}), dict(query or {})
            target = b if method == "POST" else q
            target["page_size"] = 100
            if cursor:
                target["start_cursor"] = cursor
            obj = self.request(path, method, b if method == "POST" else None, q)
            if not isinstance(obj.get("results"), list):
                raise CollectorError("bad_response")
            yield from obj["results"]
            if not obj.get("has_more"):
                break
            cursor = obj.get("next_cursor")
            if not isinstance(cursor, str) or not cursor or cursor in seen:
                raise CollectorError("pagination_stalled")
            seen.add(cursor)


def _page_path(root, pid):
    return inside(root, Path(root) / "pages" / pid[:2] / (pid + ".json"))


def _complete_properties(client, page):
    # Inline relations, people and rich text can be truncated at 25 items.
    for prop in page.get("properties", {}).values():
        typ = prop.get("type") if isinstance(prop, dict) else None
        values = prop.get(typ) if isinstance(typ, str) else None
        if typ not in ("title", "rich_text", "relation", "people") or not isinstance(values, list):
            continue
        if not prop.get("has_more") and len(values) < 25:
            continue
        # Property IDs can contain URL-escaped punctuation; ntn handles the encoded path.
        from urllib.parse import quote, unquote
        ident = quote(unquote(str(prop.get("id", ""))), safe="")
        if not ident:
            raise CollectorError("bad_response")
        rows = list(client.items(f"/v1/pages/{page['id']}/properties/{ident}"))
        prop[typ] = [v[typ] for v in rows if typ in v]
        prop.pop("has_more", None)


def fetch_page(client, pid, comments=True):
    try:
        before = client.request(f"/v1/pages/{pid}")
    except CollectorError as e:
        if e.status in (403, 404):
            raise CollectorError("page_gone", e.status) from None
        raise
    if nt_id(before.get("id")) != pid:
        raise CollectorError("bad_response")
    _complete_properties(client, before)
    blocks, order, comment_map, visiting = {}, [], {}, set()

    def read_comments(ident):
        if comments:
            for obj in client.items("/v1/comments", query={"block_id": ident}):
                cid = nt_id(obj.get("id"))
                if cid is None:
                    raise CollectorError("bad_response")
                comment_map[cid] = obj

    def children(ident, depth, parent):
        if ident in visiting or depth > 200:
            raise CollectorError("block_cycle")
        visiting.add(ident)
        for obj in client.items(f"/v1/blocks/{ident}/children"):
            bid = nt_id(obj.get("id"))
            if bid is None:
                raise CollectorError("bad_response")
            if bid in blocks:
                continue
            blocks[bid] = obj
            order.append([bid, depth, parent])
            read_comments(bid)
            typ = obj.get("type")
            source = bid
            if typ == "synced_block":
                synced = obj.get("synced_block", {}).get("synced_from")
                if isinstance(synced, dict):
                    source = nt_id(synced.get("block_id"))
                    if source is None:
                        raise CollectorError("bad_response")
            if (obj.get("has_children") or source != bid) and typ not in ("child_page", "child_database"):
                children(source, depth + 1, bid)
        visiting.remove(ident)

    read_comments(pid)
    children(pid, 0, None)
    after = client.request(f"/v1/pages/{pid}")
    if any(before.get(k) != after.get(k) for k in ("last_edited_time", "in_trash", "archived")):
        raise CollectorError("page_unstable")
    return {"page": before, "blocks": blocks, "order": order, "comments": comment_map}


def merge(old, new, stamp, comments=True):
    new = copy.deepcopy(new)
    if old:
        for kind in ("blocks", "comments"):
            for ident, obj in old.get(kind, {}).items():
                if ident not in new[kind]:
                    missing = copy.deepcopy(obj)
                    if kind != "comments" or comments:
                        missing.setdefault("_removed_at", stamp)
                    new[kind][ident] = missing
    return new


def sync(root, client, page_ids=(), comments=True, full=False, max_items=0):
    root = Path(root)
    stamp = now()
    stats = {"status": "ok", "pages_attempted": 0, "pages_fetched": 0, "pages_written": 0, "pages_gone": 0,
             "data_sources": 0, "errors": 0, "error_counts": {}, "comments_enabled": comments,
             "user_directory_available": True, "limited": False}

    def failed(e):
        stats["errors"] += 1
        category = e.category + ("_" + str(e.status) if isinstance(e.status, int) else "")
        stats["error_counts"][category] = stats["error_counts"].get(category, 0) + 1
    with locked(root):
        me = client.request("/v1/users/me")
        if not isinstance(me.get("id"), str):
            raise CollectorError("bad_response")
        bind(root, me["id"])
        users = load(root / "users.json", {})
        try:
            for user in client.items("/v1/users"):
                uid = nt_id(user.get("id"))
                if uid is None:
                    raise CollectorError("bad_response")
                users[uid] = user
        except CollectorError as e:
            if e.status != 403:
                raise
            # Content access need not include directory access. Keep known users;
            # page/block authors still remain searchable by their UUIDs.
            stats["user_directory_available"] = False
        users.setdefault(nt_id(me["id"]), me)
        atomic(root, root / "users.json", users)
        man = load(root / "manifest.json", {"version": 1, "pages": {}, "data_sources": {}})
        if man.get("version") != 1 or not isinstance(man.get("pages"), dict) or not isinstance(man.get("data_sources"), dict):
            raise CollectorError("bad_archive")
        state = load(root / "_collector_state.json", {})
        last_sweep = iso_ms(state.get("last_full_sweep")) or 0
        sweep = full or (iso_ms(stamp) - last_sweep >= 86400000)
        selected = {nt_id(v) for v in page_ids}
        if None in selected:
            raise CollectorError("bad_page_id")
        candidates, sources = {}, {}
        # Full pagination makes discovery independent of coarse page edit timestamps.
        if not selected:
            for trash in (False, True):
                body = {"sort": {"direction": "descending", "timestamp": "last_edited_time"},
                        "filter": {"in_trash": trash}}
                for obj in client.items("/v1/search", "POST", body=body):
                    ident = nt_id(obj.get("id"))
                    if ident is None:
                        raise CollectorError("bad_response")
                    if obj.get("object") == "page":
                        candidates[ident] = obj
                    elif obj.get("object") == "data_source":
                        sources[ident] = obj
            for sid in sorted(sources):
                try:
                    ds = client.request(f"/v1/data_sources/{sid}")
                    atomic(root, root / "ds" / (sid + ".json"), ds)
                    man["data_sources"][sid] = {"path": f"ds/{sid}.json", "title": page_title(ds), "parent": ds.get("parent")}
                    stats["data_sources"] += 1
                    for obj in client.items(f"/v1/data_sources/{sid}/query", "POST"):
                        if obj.get("object") == "page" and nt_id(obj.get("id")):
                            candidates[nt_id(obj["id"])] = obj
                except CollectorError as e:
                    failed(e)
        else:
            candidates = {pid: {} for pid in selected}
        if sweep:
            for pid in man["pages"]:
                if not selected or pid in selected:
                    candidates.setdefault(pid, {})
        for pid, meta in candidates.items():
            old_meta = man["pages"].get(pid, {})
            old_path = _page_path(root, pid)
            fetched = iso_ms(old_meta.get("fetched_at"))
            edited = iso_ms(meta.get("last_edited_time"))
            if not sweep and old_path.exists() and fetched and edited and edited < fetched // 60000 * 60000 \
                    and meta.get("in_trash") == old_meta.get("in_trash"):
                continue
            if max_items and stats["pages_attempted"] >= max_items:
                stats["limited"] = True
                break
            old = load(old_path)
            stats["pages_attempted"] += 1
            try:
                value = fetch_page(client, pid, comments)
            except CollectorError as e:
                # Only the page lookup can establish lost page access. A missing child
                # or comments permission failure must preserve the previous full snapshot.
                if e.category == "page_gone" and old:
                    value = copy.deepcopy(old)
                    value.setdefault("_gone_at", stamp)
                    atomic(root, old_path, value)
                    stats["pages_gone"] += 1
                else:
                    failed(e)
                continue
            stats["pages_fetched"] += 1
            value = merge(old, value, stamp, comments)
            for obj in [value["page"], *value["blocks"].values(), *value["comments"].values()]:
                for field in ("created_by", "last_edited_by"):
                    user = obj.get(field)
                    uid = nt_id(user.get("id")) if isinstance(user, dict) else None
                    if uid:
                        users.setdefault(uid, user)
            if old is None or project(old) != project(value):
                atomic(root, old_path, value)
                stats["pages_written"] += 1
            man["pages"][pid] = {"path": old_path.relative_to(root).as_posix(), "title": page_title(value["page"]),
                                  "parent": value["page"].get("parent"), "last_edited_time": value["page"].get("last_edited_time"),
                                  "in_trash": value["page"].get("in_trash", False), "fetched_at": stamp}
        man["generation"] = uuid.uuid4().hex
        atomic(root, root / "users.json", users)
        atomic(root, root / "manifest.json", man)
        if sweep and not stats["limited"] and not stats["errors"]:
            state["last_full_sweep"] = stamp
        atomic(root, root / "_collector_state.json", state)
    stats["requests"] = getattr(client, "requests", 0)
    return stats


def sync_config(cfg, auth_only=False, full=False, max_items=0):
    if cfg.nt_source == "desktop":
        from . import desktop
        source = cfg.nt_desktop_path or desktop.default_path()
        if auth_only:
            if not Path(source).is_file():
                raise CollectorError("desktop_missing")
            return {"status": "local_ready", "authentication_required": False}
        return desktop.sync(cfg.sources["nt_archive"], source, cfg.nt_page_ids, max_items or cfg.nt_max_pages)
    client = NotionClient(cfg.nt_cli)
    if auth_only:
        value = client.request("/v1/users/me")
        return {"status": "authenticated", "authenticated": bool(nt_id(value.get("id")))}
    return sync(cfg.sources["nt_archive"], client, cfg.nt_page_ids, cfg.nt_comments, full, max_items or cfg.nt_max_pages)


if __name__ == "__main__":
    from tools.archiveio import main
    raise SystemExit(main("notion", sync_config))
