"""Read-only Gmail API sync: immutable content, nonshrinking label state, cursor last."""
import copy
import datetime as dt
import hashlib
import json
from pathlib import Path
import sys
import time
import uuid
from tools.archiveio import CollectorError, atomic, bind, inside, load, locked, now
from .api import GmailClient, pages
from .apps import EXCLUDED
from .auth import authorize, refresh
from .keys import gm_id
from .mime import extract

PARTITION_BYTES = 16 * 1024 * 1024


def _labels(value):
    if not isinstance(value, list) or not all(isinstance(v, str) and v and ":" not in v
                                              and not any(ord(c) < 32 for c in v) for v in value):
        raise CollectorError("bad_response")
    return sorted(set(value))


def _profile(client):
    value = client.request("/profile")
    if not isinstance(value.get("emailAddress"), str) or not value["emailAddress"] \
            or not isinstance(value.get("historyId"), str) or not value["historyId"].isdigit():
        raise CollectorError("bad_response")
    return value


def _directory(client, old, stamp):
    rows = client.request("/labels").get("labels")
    if not isinstance(rows, list) or not all(isinstance(v, dict) for v in rows):
        raise CollectorError("bad_response")
    result, current = copy.deepcopy(old), set()
    for obj in rows:
        ident = obj.get("id")
        _labels([ident])
        if not all(isinstance(obj.get(k), str) for k in ("name", "type")):
            raise CollectorError("bad_response")
        current.add(ident)
        result[ident] = {k: obj[k] for k in ("id", "name", "type")}
    for ident in set(result) - current:
        result[ident].setdefault("_removedAt", stamp)
    return result, current


def _listing(client, labels, stats):
    members = {}
    for _, rows in pages(client, "/messages", {"includeSpamTrash": "true", "maxResults": 500}):
        for obj in rows:
            ident = gm_id(obj.get("id"))
            if ident is None:
                stats["pk_bad"] += 1
                continue
            members.setdefault(ident, set())
    # messages.list has no labelIds in its message objects. Per-label listings
    # discover exclusions without fetching any excluded message's raw content.
    for label in sorted(labels):
        for _, rows in pages(client, "/messages", {"includeSpamTrash": "true", "maxResults": 500, "labelIds": [label]}):
            for obj in rows:
                ident = gm_id(obj.get("id"))
                if ident is None:
                    stats["pk_bad"] += 1
                elif ident in members:
                    members[ident].add(label)
    return {ident: sorted(values) for ident, values in members.items()}


def _archive(root, man):
    from .readers.archive import safe_rel, partition
    known = set()
    files = man.get("files")
    if not isinstance(files, list) or files != sorted(set(files)):
        raise CollectorError("bad_archive")
    # Recover fully written partitions after a crash before manifest publication.
    # Hold IDs only; one bounded partition is decoded at a time.
    existing = set(files)
    existing.update(p.relative_to(root).as_posix() for p in (root / "exports").glob("*.json"))
    for rel in sorted(existing):
        if not safe_rel(rel, "exports"):
            raise CollectorError("unsafe_path")
        path = inside(root, root / rel)
        try:
            rows = partition(path.read_bytes())
        except (OSError, ValueError, TypeError):
            raise CollectorError("bad_archive") from None
        for obj in rows:
            ident = gm_id(obj.get("id"))
            if ident is None or ident in known:
                raise CollectorError("bad_archive")
            known.add(ident)
    man["files"] = sorted(existing)
    states = {}
    shards = man.get("state_files")
    if not isinstance(shards, list) or shards != sorted(set(shards)):
        raise CollectorError("bad_archive")
    shards = sorted(set(shards) | {p.relative_to(root).as_posix() for p in (root / "state").glob("*.json")})
    man["state_files"] = shards
    for rel in shards:
        if not safe_rel(rel, "state"):
            raise CollectorError("unsafe_path")
        obj = load(inside(root, root / rel))
        if not isinstance(obj, dict):
            raise CollectorError("bad_archive")
        for ident, value in obj.items():
            if gm_id(ident) is None or ident[:2] != Path(rel).stem or not isinstance(value, dict):
                raise CollectorError("bad_archive")
            _labels(value.get("label_ids"))
            states[ident] = value
    return known, states


def _write_states(root, man, states):
    buckets = {}
    for ident, value in states.items():
        buckets.setdefault(ident[:2], {})[ident] = value
    for prefix, values in sorted(buckets.items()):
        rel = f"state/{prefix}.json"
        if load(inside(root, root / rel)) != values:
            atomic(root, root / rel, values)
        if rel not in man["state_files"]:
            man["state_files"].append(rel)
    man["state_files"].sort()


def _publish(root, man, states, labels):
    _write_states(root, man, states)
    if load(inside(root, root / "labels.json")) != labels:
        atomic(root, root / "labels.json", labels)
    man["generation"] = uuid.uuid4().hex
    atomic(root, root / "manifest.json", man)


class Writer:
    def __init__(self, root, man, stats, cap):
        self.root, self.man, self.stats = root, man, stats
        self.cap, self.chunk, self.bytes = cap or 1000, [], 0

    def add(self, obj):
        size = len(json.dumps(obj, ensure_ascii=True, sort_keys=True).encode("ascii")) + 2
        if size + 100 > PARTITION_BYTES:
            raise CollectorError("bad_response")
        if self.chunk and (len(self.chunk) >= self.cap or self.bytes + size + 100 > PARTITION_BYTES):
            self.flush()
        self.chunk.append(obj)
        self.bytes += size

    def flush(self):
        if not self.chunk:
            return
        stamp = dt.datetime.now(dt.timezone.utc)
        if self.man["files"]:
            previous = dt.datetime.strptime(Path(self.man["files"][-1]).stem, "%Y%m%dT%H%M%S%fZ").replace(tzinfo=dt.timezone.utc)
            stamp = max(stamp, previous + dt.timedelta(microseconds=1))
        rel = "exports/" + stamp.strftime("%Y%m%dT%H%M%S%fZ") + ".json"
        atomic(self.root, self.root / rel, {"version": 1, "messages": self.chunk}, immutable=True)
        self.man["files"].append(rel)
        self.stats["partitions_written"] += 1
        self.stats["messages_written"] += len(self.chunk)
        self.chunk, self.bytes = [], 0


def sync(root, client, exclude_labels=EXCLUDED, volatile_labels=("UNREAD",), body_max_bytes=131072,
         partition_max_messages=1000, full=False, max_items=0, time_budget=3000):
    root, stamp = Path(root), now()
    deadline = time.monotonic() + time_budget
    stats = {"status": "ok", "messages_written": 0, "partitions_written": 0, "errors": 0,
             "limited": False, "excluded": 0, "pk_bad": 0}
    exclude, volatile = set(exclude_labels), set(volatile_labels)
    attempts = 0
    with locked(root):
        # Profile precedes every mailbox listing, including resync after expiry.
        profile = _profile(client)
        bind(root, profile["emailAddress"].lower())
        man = load(inside(root, root / "manifest.json"), {"version": 1, "files": [], "state_files": [],
                                           "cursor": None, "genesis_complete": False})
        if not isinstance(man, dict) or man.get("version") != 1 \
                or type(man.get("genesis_complete")) is not bool \
                or man.get("cursor") is not None and (not isinstance(man["cursor"], str) or not man["cursor"].isdigit()):
            raise CollectorError("bad_archive")
        old_cursor, old_genesis = man["cursor"], man["genesis_complete"]
        man["binding_sha256"] = hashlib.sha256(profile["emailAddress"].lower().encode()).hexdigest()
        known, states = _archive(root, man)
        writer = Writer(root, man, stats, partition_max_messages)
        labels = load(inside(root, root / "labels.json"), {})
        pending = load(inside(root, root / "_collector_state.json"), {})
        if not isinstance(labels, dict) or not isinstance(pending, dict):
            raise CollectorError("bad_archive")

        def bounded():
            return bool(max_items and attempts >= max_items or time.monotonic() >= deadline)

        def state(values, removed=None):
            result = {"label_ids": sorted(set(values) - volatile)}
            if removed:
                result["_removedAt"] = removed
            return result

        def consistent(values):
            # Partial fetch failures can leave immutable content without state.
            # Never move the cursor past an unreconciled content ID.
            if known - set(values):
                raise CollectorError("collection_failed")

        def fetch(ident, values):
            nonlocal attempts
            if ident in known:
                return values
            if set(values) & exclude:
                stats["excluded"] += 1
                return values
            if bounded():
                stats["limited"] = True
                raise CollectorError("collection_failed")
            attempts += 1
            obj = client.request("/messages/" + ident, {"format": "raw"})
            if obj.get("id") != ident:
                raise CollectorError("bad_response")
            actual = _labels(obj.get("labelIds"))
            # A message may enter an excluded category after the listing.
            if set(actual) & exclude:
                stats["excluded"] += 1
                return actual
            writer.add(extract(obj, body_max_bytes))
            known.add(ident)
            return actual

        try:
            labels, current_labels = _directory(client, labels, stamp)
            sweep = full or not man["genesis_complete"] or bool(pending.get("active"))
            history = []
            cursor = None
            if not sweep:
                try:
                    for obj, rows in pages(client, "/history", {"startHistoryId": man["cursor"],
                                          "historyTypes": ["messageAdded", "messageDeleted", "labelAdded", "labelRemoved"]}, "history"):
                        if not isinstance(obj.get("historyId"), str) or not obj["historyId"].isdigit():
                            raise CollectorError("bad_response")
                        cursor = obj["historyId"]
                        history.extend(rows)
                except CollectorError as e:
                    if e.status != 404:
                        raise
                    sweep = True
                    profile = _profile(client)
            if sweep:
                # An explicit full run can replace a stale genesis plan after a
                # message was deleted before its pending raw fetch completed.
                if full:
                    pending = {}
                if not pending.get("active"):
                    members = _listing(client, current_labels, stats)
                    pending = {"active": True, "history_id": profile["historyId"], "members": members,
                               "pending": sorted(members), "resync": bool(man["genesis_complete"])}
                    atomic(root, root / "_collector_state.json", pending)
                members = pending["members"]
                for ident in list(pending["pending"]):
                    if bounded() and ident not in known and not set(members[ident]) & exclude:
                        stats["limited"] = True
                        break
                    values = fetch(ident, members[ident])
                    states[ident] = state(values)
                    pending["pending"].remove(ident)
                    # Manifest records each completed partition before removing its
                    # pending IDs: replay after a crash cannot duplicate content.
                    if writer.chunk and len(writer.chunk) >= writer.cap:
                        writer.flush()
                        _publish(root, man, states, labels)
                        atomic(root, root / "_collector_state.json", pending)
                writer.flush()
                if not pending["pending"]:
                    for ident in known - set(states):
                        states[ident] = state(members.get(ident, []))
                    for ident in (known | set(states)) - set(members):
                        states[ident].setdefault("_removedAt", stamp)
                    consistent(states)
                    man["cursor"] = pending["history_id"]
                    man["genesis_complete"] = True
                    pending["active"] = False
                _publish(root, man, states, labels)
                atomic(root, root / "_collector_state.json", pending)
            else:
                updates = copy.deepcopy(states)
                touched = set()
                for event in history:
                    for field in ("messagesAdded", "labelsAdded", "labelsRemoved", "messagesDeleted"):
                        entries = event.get(field, [])
                        if not isinstance(entries, list):
                            raise CollectorError("bad_response")
                        for entry in entries:
                            msg = entry.get("message") if isinstance(entry, dict) else None
                            if not isinstance(msg, dict):
                                raise CollectorError("bad_response")
                            ident = gm_id(msg.get("id"))
                            if ident is None:
                                stats["pk_bad"] += 1
                                continue
                            old = updates.get(ident)
                            values = set(old["label_ids"] if old else
                                         _labels(msg.get("labelIds", [] if field == "messagesDeleted" else None)))
                            if field == "messagesAdded":
                                values = set(_labels(msg.get("labelIds")))
                            elif field == "labelsAdded":
                                values.update(_labels(entry.get("labelIds")))
                            elif field == "labelsRemoved":
                                values.difference_update(_labels(entry.get("labelIds")))
                            removed = (old or {}).get("_removedAt")
                            if field == "messagesDeleted":
                                removed = removed or stamp
                            updates[ident] = state(values, removed)
                            touched.add(ident)
                for ident in sorted(touched):
                    if "_removedAt" not in updates[ident]:
                        was_known = ident in known
                        values = fetch(ident, updates[ident]["label_ids"])
                        if not was_known:
                            updates[ident] = state(values)
                writer.flush()
                for ident in sorted(known - set(updates)):
                    if time.monotonic() >= deadline:
                        stats["limited"] = True
                        raise CollectorError("collection_failed")
                    obj = client.request("/messages/" + ident, {"format": "minimal", "fields": "id,labelIds"})
                    if obj.get("id") != ident:
                        raise CollectorError("bad_response")
                    updates[ident] = state(_labels(obj.get("labelIds")))
                consistent(updates)
                states = updates
                man["cursor"] = cursor
                _publish(root, man, states, labels)
        except CollectorError as e:
            if e.category in ("auth_required", "account_changed"):
                raise
            stats["errors"] += 1
            man["cursor"], man["genesis_complete"] = old_cursor, old_genesis
            writer.flush()
            # Fully written content can be captured; failed history state/deletion
            # remains unpublished and the old cursor is retried on the next run.
            _publish(root, man, states, load(inside(root, root / "labels.json"), {}))
            if pending.get("active"):
                atomic(root, root / "_collector_state.json", pending)
    stats["requests"] = getattr(client, "requests", 0)
    return stats


def sync_config(cfg, auth_only=False, full=False, max_items=0):
    client = GmailClient(refresh(cfg.gm_client_file, cfg.gm_token_file))
    if auth_only:
        _profile(client)
        return {"status": "authenticated", "errors": 0}
    return sync(cfg.sources["gm_archive"], client, cfg.gm_exclude_labels, cfg.gm_volatile_labels,
                cfg.gm_body_max_bytes, cfg.gm_partition_max_messages, full, max_items)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--auth" not in argv:
        import contextlib
        from rawlayer.capture import publicout
        from tools.archiveio import main as archive_main
        target = sys.stdout
        if "--help" in argv or "-h" in argv:
            target.write("One-time authorization: python -s -B -m gmail.collector --auth [--config CONFIG]\n")

        class PublicStream:
            # archiveio owns the CLI semantics. Validate its aggregate JSON at
            # this app boundary, without changing Discord/Notion's output.
            def __init__(self):
                self.buffer, self.rejected = "", False

            def write(self, value):
                self.buffer += value
                while "\n" in self.buffer:
                    line, self.buffer = self.buffer.split("\n", 1)
                    if line.startswith("{"):
                        try:
                            obj = json.loads(line)
                            line = publicout.render_json(obj, indent=None).decode("ascii")
                        except (ValueError, TypeError):
                            self.rejected = True
                            line = '{"app": "gmail", "status": "error"}'
                    target.write(line + "\n")
                return len(value)

            def flush(self):
                target.flush()

        output = PublicStream()
        with contextlib.redirect_stdout(output):
            rc = archive_main("gmail", sync_config, argv)
        return rc or int(output.rejected)
    import argparse
    from rawlayer import registry
    from rawlayer.capture.config import Config
    from rawlayer.management.base import default_config
    from rawlayer.capture import publicout
    p = argparse.ArgumentParser(description="Authorize Gmail read-only installed-app OAuth")
    p.add_argument("--auth", action="store_true")
    p.add_argument("--config")
    a = p.parse_args(argv)
    registry.select("gmail")
    try:
        cfg = Config(a.config or default_config("gmail"))
        report = authorize(cfg.gm_client_file, cfg.gm_token_file)
        print(publicout.render_json(report).decode("ascii"))
        return 0
    except CollectorError as e:
        print(publicout.render_json({"status": e.category}).decode("ascii"))
        return 2
    except Exception:
        print(publicout.render_json({"status": "error"}).decode("ascii"))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
