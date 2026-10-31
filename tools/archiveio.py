"""Private, atomic archive publication shared by the app-owned collectors."""
import contextlib
import datetime as dt
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid


class CollectorError(ValueError):
    def __init__(self, category, status=None):
        self.category, self.status = category, status
        super().__init__(category)  # Never include remote bodies, paths, IDs, or credentials.


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def load(path, default=None):
    path = Path(path)
    if not path.exists():
        return default
    def reject(value):
        raise CollectorError("bad_archive")
    return json.loads(path.read_text(encoding="utf-8"), parse_constant=reject)


def inside(root, path):
    path = Path(path)
    if not path.resolve().is_relative_to(Path(root).resolve()):
        raise CollectorError("unsafe_path")
    return path


def atomic(root, path, value, immutable=False):
    """Write value (JSON, sorted and ASCII; or bytes as they are) to a private temporary file, fsync, then replace,
    retried as capture retries its own replaces (a reader or a scanner may hold the old file)."""
    from rawlayer.capture.fsio import replace_retry
    path = inside(root, path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if immutable and path.exists():
        raise CollectorError("partition_exists")
    if not isinstance(value, bytes):
        value = (json.dumps(value, ensure_ascii=True, allow_nan=False, sort_keys=True) + "\n").encode("ascii")
    tmp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with tmp.open("xb") as fh:
            fh.write(value)
            fh.flush()
            os.fsync(fh.fileno())
        try:
            replace_retry(tmp, path)
        except PermissionError:
            raise CollectorError("archive_busy") from None
    finally:
        tmp.unlink(missing_ok=True)


def credential_path(value):
    """A token or credential file as a Path: absolute, under the data parent, in a folder that passes capture's
    precheck. Raises CollectorError("unsafe_token_path") or precheck's ConfigError."""
    from rawlayer.capture import paths, precheck
    path = Path(value)
    if not value or not path.is_absolute() or not path.resolve().is_relative_to(Path(paths.DATA_PARENT).resolve()):
        raise CollectorError("unsafe_token_path")
    precheck.check(path.parent)
    return path


def sync_first(ctx, enabled, archive, app):
    """An origin's read on live capture with the app's *_sync on: run the app's collector into its archive first
    (unless the collector itself started this capture: RAWLAYER_ARCHIVE_READY). Any collector error stops the
    origin's read, so nothing half-fetched is diffed."""
    if enabled and ctx.inputs.name == "live" and os.environ.get("RAWLAYER_ARCHIVE_READY") != "1":
        import importlib
        from rawlayer.capture.precheck import check
        check(archive)
        if importlib.import_module(app + ".collector").sync_config(ctx.cfg).get("errors"):
            raise CollectorError("collection_incomplete")


@contextlib.contextmanager
def locked(root):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    path = inside(root, root / ".collector.lock")
    with path.open("a+b") as fh:
        fh.seek(0, 2)
        if fh.tell() == 0:
            fh.write(b"0")
            fh.flush()
        fh.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise CollectorError("collector_busy") from None
        try:
            yield
        finally:
            fh.seek(0)
            if os.name == "nt":
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(fh, fcntl.LOCK_UN)


def bind(root, ident):
    import hashlib
    digest = hashlib.sha256(ident.encode("utf-8")).hexdigest()
    path = Path(root) / "_collector_identity.json"
    old = load(path)
    if old is not None and old.get("sha256") != digest:
        raise CollectorError("account_changed")
    atomic(root, path, {"sha256": digest})


def boolean(value):
    if not isinstance(value, bool):
        raise ValueError("boolean")
    return value


def text(value):
    if not isinstance(value, str):
        raise ValueError("text")
    return value


def nonnegative(value):
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 100000:
        raise ValueError("nonnegative integer")
    return value


def identifiers(value):
    if not isinstance(value, list) or not all(isinstance(x, str) and x for x in value):
        raise ValueError("identifier list")
    return value


def main(app, sync_config, argv=None):
    import argparse
    from rawlayer import registry
    from rawlayer.capture.config import Config
    from rawlayer.capture import precheck
    from rawlayer.management.base import default_config
    p = argparse.ArgumentParser(description=f"Fetch {app} into its private archive; print aggregate status only")
    p.add_argument("--config")
    p.add_argument("--auth-check", action="store_true")
    p.add_argument("--full", action="store_true")
    p.add_argument("--max-items", type=int, default=0, help="bound this run; 0 means unlimited")
    p.add_argument("--capture", action="store_true", help="run ledger capture/index after successful collection")
    a = p.parse_args(argv)
    if a.max_items < 0:
        p.error("--max-items must be nonnegative")
    try:
        registry.select(app)
        cfg = Config(a.config or default_config(app))
        precheck.check(cfg.data_root)
        precheck.check(cfg.sources[registry.get().apps[0].source_keys[0]])
        report = sync_config(cfg, auth_only=a.auth_check, full=a.full, max_items=a.max_items)
        print(json.dumps({"app": app, **report}, sort_keys=True))
        if a.capture and not a.auth_check:
            # Disable automatic API sync for this child: the archive was just completed.
            env = dict(os.environ, RAWLAYER_ARCHIVE_READY="1")
            root = Path(__file__).resolve().parent.parent
            result = subprocess.run([sys.executable, "-s", "-B", str(root / "manage.py"), "collect", app,
                                     "--config", str(cfg.path)], env=env)
            return result.returncode or (75 if report.get("errors", 0) else 0)
        return 0 if report.get("errors", 0) == 0 else 75
    except CollectorError as e:
        print(json.dumps({"app": app, "status": e.category, "http_status": e.status}))
        return 2 if e.category in ("auth_required", "auth_failed", "account_changed") else 75
    except Exception as e:
        print(json.dumps({"app": app, "status": "error", "error_type": type(e).__name__}))
        return 1
