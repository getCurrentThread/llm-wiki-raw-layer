# -*- coding: utf-8 -*-
"""The capture command: lock -> sweep -> recover -> preconditions -> read+diff -> commit (SPEC 9.1)."""
import datetime as _dt
import hashlib
import json
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

from .. import registry
from . import engine, fsio, paths, publicout, state
from .config import ConfigError, InputSet
from .ledger import Ledger, log_capture, write_segments
from .publicout import lit

HARD_FLOOR = 10 * 1024 ** 3
EXIT_OK, EXIT_ERR, EXIT_CFG, EXIT_SUSPECT, EXIT_BUSY = 0, 1, 2, 3, 75


class Busy(Exception):
    pass


def new_run_id():
    return f"{int(time.time() * 1000):013d}-{secrets.token_hex(2)}"


def iso(ts=None):
    t = _dt.datetime.fromtimestamp(time.time() if ts is None else ts, _dt.timezone.utc)
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


class RunLock:
    """<raw>/.state/run.lock, locked without blocking (msvcrt on Windows, flock elsewhere -- both per open file, so a
    second RunLock in the same process is Busy too)."""

    def __init__(self, ledger: Ledger):
        state_dir = Path(ledger.state_dir)
        state_dir.mkdir(parents=True, exist_ok=True)
        self.fd = os.open(state_dir / "run.lock", os.O_RDWR | os.O_CREAT)
        try:
            fsio.lock_fd(self.fd)
        except OSError:
            os.close(self.fd)
            raise Busy()

    def release(self):
        try:
            fsio.unlock_fd(self.fd)
        except OSError:
            pass
        os.close(self.fd)


_SHA = re.compile(r"^[0-9a-f]{40}$")


def work_tree(project):
    """The directory (a Path) holding .git at or above the project directory (None when there is none): a project
    may be one folder of a larger repository, or the repository itself."""
    here = Path(os.path.abspath(project))      # abspath: ".." folded without following links (resolve() would)
    for d in (here, *here.parents):
        if (d / ".git").exists():
            return d
    return None


def code_paths():
    """The project paths (git pathspecs) whose changes make the running code dirty: the framework, config/ (the
    settings) and the selected app's folder (another app's change is not this ledger's code). With no app selected
    (the core tests' toy apps), the whole project directory."""
    from .. import registry  # noqa: PLC0415
    app = registry.selected()
    if app is None:
        return ["."]
    return ["manage.py", "CORE.md", "rawlayer", "tests/__init__.py", "tests/core", "config", app]


def repo_info():
    """(git_head, dirty) of the code that runs; dirty looks at code_paths() of the project directory only (another
    folder of the same repository, or another app, is not this code). Under SYSTEM git refuses a repo owned by
    another user unless safe.directory names its work tree; if git still fails, HEAD is read from .git directly
    and dirty is reported as true
    (unknown is never reported as clean). --no-optional-locks: `status` must not refresh the index (a write
    outside the data root, and as SYSTEM a SYSTEM-owned index.lock in the user's repo)."""
    repo = paths.REPO
    top = work_tree(repo) or repo
    git = ["git", "--no-optional-locks", "-c", "safe.directory=" + str(top).replace("\\", "/"), "-C", str(repo)]
    try:
        h = subprocess.run(git + ["rev-parse", "HEAD"], capture_output=True, text=True, timeout=60)
        st = subprocess.run(git + ["status", "--porcelain", "--", *code_paths()], capture_output=True, text=True,
                            timeout=60)
        head = h.stdout.strip()
        if h.returncode == 0 and st.returncode == 0 and _SHA.fullmatch(head):
            return head, bool(st.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        pass
    return _head_from_files(repo), True


def _head_from_files(repo):
    """HEAD sha from the .git files of the work tree at or above `repo` (worktree-aware: .git may be a 'gitdir:'
    file; refs may be packed)."""
    try:
        top = work_tree(repo)
        if top is None:
            return None
        g = top / ".git"
        # normpath, not resolve(): the ".." of a relative gitdir / commondir is folded without following links
        if g.is_file():
            g = Path(os.path.normpath(top / g.read_text(encoding="utf-8").strip()[len("gitdir:"):].strip()))
        common = g
        if (g / "commondir").exists():
            common = Path(os.path.normpath(g / (g / "commondir").read_text(encoding="utf-8").strip()))
        head = (g / "HEAD").read_text(encoding="utf-8").strip()
        if head.startswith("ref: "):
            ref = head[len("ref: "):]
            for d in (g, common):
                p = d.joinpath(*ref.split("/"))
                if p.exists():
                    head = p.read_text(encoding="utf-8").strip()
                    break
            else:
                head = None
                with (common / "packed-refs").open(encoding="utf-8") as fh:
                    for ln in fh:
                        if ln.rstrip("\n").endswith(" " + ref):
                            head = ln.split(" ", 1)[0]
        return head if head and _SHA.fullmatch(head) else None
    except (OSError, ValueError):
        return None


def pycryptodome_version():
    try:
        import Crypto  # noqa: PLC0415
        return Crypto.__version__
    except Exception:  # noqa: BLE001
        return None


class Ctx:
    pass


def open_session(cfg):
    """Lock, sweep .work, recover, catch state up. Returns (ledger, lock, state_con). A ledger defect met while
    recovering or catching up aborts the command (LedgerCorrupt and friends, exit 1)."""
    ledger = Ledger(cfg.raw)
    ledger.ensure_dirs()
    lock = RunLock(ledger)
    con = None
    try:
        fsio.clear_dir(ledger.work_root)
        ledger.recover()
        con = _state_session(ledger)
    except BaseException:
        if con is not None:
            con.close()
        lock.release()
        raise
    return ledger, lock, con


class StateDamaged(Exception):
    """state.sqlite is not a database or is malformed (SQLITE_NOTADB / SQLITE_CORRUPT, or quick_check)."""


def _damage(e):
    code = getattr(e, "sqlite_errorcode", None)
    return isinstance(code, int) and (code & 0xFF) in (11, 26)     # SQLITE_CORRUPT, SQLITE_NOTADB


def _state_session(ledger):
    """state.sqlite opened, checked and caught up. A damaged file is only a cache of the fold (SPEC 3), so it is set
    aside as state.sqlite.bad-<run> and rebuilt from the ledger, once. Damage means SQLite says 'not a database'
    or 'malformed', or quick_check is not ok (damage below the header opens fine). Busy, I/O and constraint errors
    are not damage; a ledger record SQLite cannot store is LedgerCorrupt (state.apply_manifest)."""
    path = Path(ledger.state_dir, "state.sqlite")
    for attempt in (0, 1):
        con = None
        try:
            try:
                con = state.open_state(path)
                if con.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                    raise StateDamaged()
                state.catch_up(con, ledger)
            except sqlite3.DatabaseError as e:
                if not _damage(e):
                    raise
                raise StateDamaged() from e
            return con
        except StateDamaged:
            if con is not None:
                con.close()
            if attempt:
                raise
            state.set_aside(path, new_run_id())
            publicout.say(lit("state damaged: set aside and rebuilt from the ledger"))
        except BaseException:
            if con is not None:
                con.close()
            raise
    raise AssertionError("unreachable")


def is_system():
    """True when this process runs as LocalSystem (token user SID S-1-5-18)."""
    return fsio.token_user_sid() == "S-1-5-18"


def selected(origins):
    """The installed Origins named in `origins`, in ledger order."""
    return [o for o in registry.origins() if o.name in origins]


def estimate_peak(inputs, origins):
    """Bytes a run may add to the data volume: what each selected origin says it may need (Origin.estimate_peak:
    a decrypted image and its temp file, an upper bound for a spill of new records ...) and 1 GiB for segments
    and the remaining spill."""
    return sum(o.estimate_peak(inputs) for o in selected(origins)) + 1024 ** 3


def capture(cfg, input_set, origins, skip_precheck=False):
    from . import precheck  # noqa: PLC0415
    cfg.check_input_set(input_set)                     # refuse before open_session writes anything (SPEC 10.1)
    try:
        ledger, lock, con = open_session(cfg)          # SPEC 9.1 steps 1-3: lock, sweep, recover
    except Busy:
        publicout.say(lit("capture busy"))
        return EXIT_BUSY
    try:
        if not skip_precheck:
            precheck.check(cfg.data_root)              # step 4
        if any(o.requires_system(cfg, input_set) for o in selected(origins)) and not is_system():
            raise ConfigError("an origin is read as SYSTEM only")   # SPEC 10.2 / 16
        run = new_run_id()
        run_start = time.time()
        work = Path(ledger.work_root, run)
        fsio.setup_process(work)
        inputs = InputSet(cfg, input_set)
        free = shutil.disk_usage(cfg.data_root).free
        if free - estimate_peak(inputs, origins) < HARD_FLOOR:
            publicout.say(lit("capture skipped"), disk_floor=True)
            return EXIT_BUSY
        ctx = Ctx()
        ctx.cfg, ctx.inputs, ctx.ledger, ctx.state = cfg, inputs, ledger, con
        ctx.run, ctx.run_start_s, ctx.work_dir = run, int(run_start), work
        mans = ledger.manifests()
        ctx.seq = len(mans) + 1
        if state.head_seq(con) != len(mans):
            raise RuntimeError("state behind ledger")
        ctx.denylist = ledger.denylist()
        ctx.origin_notes = defaultdict(Counter)
        ctx.scratch = {}                               # run-scoped caches of the apps

        oruns = {}
        for spec in selected(origins):
            oruns[spec.name] = spec.run(ctx)

        prev_sha = None
        if mans:
            prev_sha = hashlib.sha256(Path(mans[-1][2]).read_bytes()).hexdigest()
        head, dirty = repo_info()
        manifest = {
            "v": 1, "type": "capture", "seq": ctx.seq, "run": run, "prev_sha256": prev_sha,
            "started_at": iso(run_start), "finished_at": None, "input_set": input_set,
            "impl": {"name": cfg.impl, "git_head": head, "dirty": dirty,
                     "python": sys.version.split()[0], "pycryptodome": pycryptodome_version()},
            "segments": [],
            "origins": {o: engine.origin_manifest(r) for o, r in oruns.items()},
        }
        commit(ctx, oruns, manifest)
        total_put = sum(r.counts.get("put", 0) for r in oruns.values())
        total_abs = sum(r.counts.get("absent", 0) for r in oruns.values())
        suspect = [o for o, r in oruns.items() if r.status == "suspect"]
        publicout.say(lit("capture done"), run=run, seq=ctx.seq, put=total_put, absent=total_abs,
                      suspect=len(suspect))
        for o, r in oruns.items():
            publicout.say(lit(" "), o, status=r.status, put=r.counts.get("put", 0),
                          absent=r.counts.get("absent", 0), containers=len(r.containers))
        if suspect:
            return EXIT_SUSPECT
        # an origin that could not be read was skipped this run (SPEC 13: 75 = skipped)
        return EXIT_BUSY if any(engine.not_read(r) for r in oruns.values()) else EXIT_OK
    finally:
        fsio.clear_dir(ledger.work_root)
        con.close()
        lock.release()


CRASH_HOOK = None   # tests only: called with a step name between the commit steps of SPEC 9.1


def _step(name):
    if CRASH_HOOK is not None:
        CRASH_HOOK(name)


def commit(ctx, oruns, manifest):
    ledger = ctx.ledger
    man_name = f"{ctx.seq:08d}-{ctx.run}.json"
    man_rel = f"manifests/{man_name}"
    man_path = Path(ledger.man_dir, man_name)
    man_part = man_path.with_name(man_name + ".part")
    inflight = {"run": ctx.run, "seq": ctx.seq, "parts": [], "manifest_part": man_rel + ".part"}

    def note_part(rel):
        inflight["parts"].append(rel)
        fsio.atomic_write(ledger.inflight, json.dumps(inflight).encode("ascii"))

    fsio.atomic_write(ledger.inflight, json.dumps(inflight).encode("ascii"))
    _step("inflight")
    year = _dt.datetime.fromtimestamp(ctx.run_start_s, _dt.timezone.utc).year
    parts = []
    for origin in registry.origin_order():
        r = oruns.get(origin)
        if r is None:
            continue
        infos = write_segments(ledger, r.spill, ctx.seq, year, on_part=note_part)
        r.spill.discard()
        parts.extend(infos)
        if infos:
            _step("segment")
    manifest["segments"] = [{"path": i["path"], "origin": i["origin"], "sha256": i["sha256"],
                             "bytes": i["bytes"], "records": i["records"]} for i in parts]
    manifest["finished_at"] = iso()
    data = publicout.render_json(manifest)
    fsio.fsync_write(man_part, data)
    _step("manifest_part")
    fsio.replace_retry(man_part, man_path)                    # COMMIT
    _step("commit")
    for i in parts:
        part = Path(i["part_path"])
        final = part.with_name(part.name[:-len(".part")])
        fsio.replace_retry(part, final)
        _step("renamed")                # renamed but still writable: recovery must chmod it too
        fsio.make_readonly(final)
        _step("finalize_one")
    state.apply_manifest(ctx.state, ledger, json.loads(data))
    _step("state")
    update_srcmap(ledger, oruns)
    log_capture(ledger, manifest)
    _step("log")
    fsio.remove_file(ledger.inflight)


def update_srcmap(ledger, oruns):
    """Flat {source_id: relpath} (SPEC 3), private to .state (relpaths of MM files carry display names).
    A source_id is sha256(relpath)[:12], so one relpath has one id whichever origin saw it. The nested
    {origin: {...}} form written before 2026-10-04 is flattened on read."""
    path = Path(ledger.state_dir, "srcmap.json")
    try:
        with path.open(encoding="utf-8") as fh:
            old = json.load(fh)
    except (OSError, ValueError):
        old = {}
    m = {}
    for k, v in (old.items() if isinstance(old, dict) else ()):
        if isinstance(v, dict):
            m.update((sk, sv) for sk, sv in v.items() if isinstance(sv, str))
        elif isinstance(v, str):
            m[k] = v
    for _o, r in sorted(oruns.items()):
        m.update(r.srcmap)
    fsio.atomic_write(path, json.dumps(m, ensure_ascii=False, sort_keys=True).encode("utf-8"))
