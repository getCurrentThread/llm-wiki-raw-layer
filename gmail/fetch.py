"""Read-only Gmail sync over IMAP: immutable content, nonshrinking label state, manifest last."""
import copy
import datetime as dt
import hashlib
import json
from pathlib import Path
import sys
import time
import uuid
from appkit.archive import FetchError, atomic, bind, inside, load, locked, now, read_token
from .apps import EXCLUDED
from .imap import ImapClient
from .origins import gm_id
from .mime import extract

PARTITION_BYTES = 16 * 1024 * 1024


def _labels(value):
    if not isinstance(value, list) or not all(isinstance(v, str) and v and ":" not in v
                                              and not any(ord(c) < 32 for c in v) for v in value):
        raise FetchError("bad_response")
    return sorted(set(value))


def _directory(rows, old, stamp):
    result, current = copy.deepcopy(old), set()
    for obj in rows:
        result[obj["id"]] = obj
        current.add(obj["id"])
    for ident in set(result) - current:
        result[ident].setdefault("_removedAt", stamp)
    return result


def _archive(root, man):
    from .origins import safe_rel, partition
    known = set()
    files = man.get("files")
    if not isinstance(files, list) or files != sorted(set(files)):
        raise FetchError("bad_archive")
    # Recover fully written partitions after a crash before manifest publication.
    # Hold IDs only; one bounded partition is decoded at a time.
    existing = set(files)
    existing.update(p.relative_to(root).as_posix() for p in (root / "exports").glob("*.json"))
    for rel in sorted(existing):
        if not safe_rel(rel, "exports"):
            raise FetchError("unsafe_path")
        path = inside(root, root / rel)
        try:
            rows = partition(path.read_bytes())
        except (OSError, ValueError, TypeError):
            raise FetchError("bad_archive") from None
        for obj in rows:
            ident = gm_id(obj.get("id"))
            if ident is None or ident in known:
                raise FetchError("bad_archive")
            known.add(ident)
    man["files"] = sorted(existing)
    states = {}
    shards = man.get("state_files")
    if not isinstance(shards, list) or shards != sorted(set(shards)):
        raise FetchError("bad_archive")
    shards = sorted(set(shards) | {p.relative_to(root).as_posix() for p in (root / "state").glob("*.json")})
    man["state_files"] = shards
    for rel in shards:
        if not safe_rel(rel, "state"):
            raise FetchError("unsafe_path")
        obj = load(inside(root, root / rel))
        if not isinstance(obj, dict):
            raise FetchError("bad_archive")
        for ident, value in obj.items():
            if gm_id(ident) is None or ident[:2] != Path(rel).stem or not isinstance(value, dict):
                raise FetchError("bad_archive")
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
            raise FetchError("bad_response")
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
         partition_max_messages=1000, max_items=0, time_budget=3000):
    """Every run lists the whole mailbox (IMAP keeps no change history), fetches the eligible messages the archive
    lacks, and rewrites each listed message's labels. Only a run that fetched all of them retires missing IDs."""
    root, stamp = Path(root), now()
    deadline = time.monotonic() + time_budget
    stats = {"status": "ok", "messages_written": 0, "partitions_written": 0, "errors": 0,
             "limited": False, "excluded": 0}
    exclude, volatile = set(exclude_labels), set(volatile_labels)
    attempts = 0
    with locked(root):
        # The address is read before any mailbox listing.
        address = client.address().lower()
        bind(root, address)
        man = load(inside(root, root / "manifest.json"), {"version": 1, "files": [], "state_files": []})
        if not isinstance(man, dict) or man.get("version") != 1:
            raise FetchError("bad_archive")
        for key in ("cursor", "genesis_complete"):  # the Gmail API source's history bookkeeping
            man.pop(key, None)
        man["binding_sha256"] = hashlib.sha256(address.encode()).hexdigest()
        known, states = _archive(root, man)
        writer = Writer(root, man, stats, partition_max_messages)
        labels = load(inside(root, root / "labels.json"), {})
        if not isinstance(labels, dict):
            raise FetchError("bad_archive")

        def state(values):
            return {"label_ids": sorted(set(values) - volatile)}

        try:
            labels = _directory(client.labels(), labels, stamp)
            members = client.members()
            for ident in sorted(members):
                values = members[ident]
                if ident not in known and set(values) & exclude:
                    stats["excluded"] += 1
                elif ident not in known:
                    if max_items and attempts >= max_items or time.monotonic() >= deadline:
                        stats["limited"] = True
                        break
                    attempts += 1
                    obj = client.message(ident)
                    values = obj["labelIds"]
                    # A message may enter an excluded category after the listing.
                    if set(values) & exclude:
                        stats["excluded"] += 1
                    else:
                        writer.add(extract(obj, body_max_bytes))
                        known.add(ident)
                states[ident] = state(values)
                if writer.chunk and len(writer.chunk) >= writer.cap:
                    writer.flush()
                    _publish(root, man, states, labels)
            writer.flush()
            if not stats["limited"]:
                for ident in (known | set(states)) - set(members):
                    states.setdefault(ident, state([])).setdefault("_removedAt", stamp)
            _publish(root, man, states, labels)
        except FetchError as e:
            if e.category in ("auth_required", "account_changed"):
                raise
            stats["errors"] += 1
            writer.flush()
            # Fully written content and the labels read so far are kept; nothing is retired.
            _publish(root, man, states, load(inside(root, root / "labels.json"), {}))
    stats["requests"] = client.requests
    return stats


def sync_config(cfg, auth_only=False, full=False, max_items=0):
    # full: every run already lists the whole mailbox
    # Google shows the app password in four groups of four: spaces are no part of it
    password = "".join(read_token((), cfg.gm_imap_password_file, r"[^ -~]").split())
    client = ImapClient(cfg.gm_imap_user, password)
    try:
        if auth_only:
            client.address()
            return {"status": "authenticated", "errors": 0}
        return sync(cfg.sources["gm_archive"], client, cfg.gm_exclude_labels, cfg.gm_volatile_labels,
                    cfg.gm_body_max_bytes, cfg.gm_partition_max_messages, max_items=max_items)
    finally:
        client.close()


def main(argv=None):
    import contextlib
    from rawlayer.capture import publicout
    from appkit.archive import main as archive_main
    target = sys.stdout

    class PublicStream:
        # appkit.archive owns the CLI semantics. Validate its aggregate JSON at
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
        rc = archive_main("gmail", sync_config, list(sys.argv[1:] if argv is None else argv))
    return rc or int(output.rejected)


if __name__ == "__main__":
    raise SystemExit(main())
