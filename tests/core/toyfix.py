# -*- coding: utf-8 -*-
"""The toy app's inputs (tests/core/toyapp) and the helpers every core test uses. Everything is synthesised in
code under the temp folder (tempfile.gettempdir()); nothing here knows a real app.

Importing this module points capture's data parent at the temp folder (Config refuses a data root outside it, SPEC
10.1); a child process must do the same and install the toy app itself (CHILD_SETUP)."""
import contextlib
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from rawlayer import registry
from rawlayer.capture import cli, paths, run, state
from rawlayer.capture.config import Config
from rawlayer.capture.ledger import Ledger
from tests.core import ledgercheck

REPO = Path(__file__).resolve().parent.parent.parent       # the project directory, found as conf.PROJECT_DIR is
TOY = "tests.core.toyapp"
TOY2 = "tests.core.toyapp2"
ORIGINS = ("toy_a", "toy_b", "toy_m")
T0 = 1_600_000_000_000          # ms

TEST_PARENT = Path(tempfile.gettempdir())
paths.DATA_PARENT = TEST_PARENT

CHILD_SETUP = ("import os, sys\n"
               "from pathlib import Path\n"
               f"sys.path.insert(0, {str(REPO)!r})\n"
               "from rawlayer import registry\n"
               "from rawlayer.capture import paths\n"
               f"paths.DATA_PARENT = Path({str(TEST_PARENT)!r})\n"
               f"_apps = registry.use([{TOY!r}])\n"     # held: a collected use() would put the old apps back
               "_apps.__enter__()\n")


def _force(func, path, _exc):
    try:
        Path(path).chmod(stat.S_IWRITE | stat.S_IREAD)
        func(path)
    except OSError:
        pass


def force_rmtree(path):
    """rmtree that also removes the read-only segment files capture leaves."""
    if Path(path).exists():
        shutil.rmtree(path, onexc=_force)


def names(folder):
    """The names in `folder`, sorted (os.listdir's view of it)."""
    return sorted(p.name for p in Path(folder).iterdir())


def suffixed(path, tail):
    """`path` with `tail` added to its name: x.sqlite -> x.sqlite.new, seg.jsonl.gz -> seg.jsonl.gz.part."""
    path = Path(path)
    return path.with_name(path.name + tail)


def py_files(top, pattern="**/*.py"):
    """The files below `top` that glob.glob(<top>/<pattern>, recursive=True) finds, sorted: Path.glob also lists
    names starting with '.' and what lies below such a folder, glob.glob does not."""
    top = Path(top)
    return sorted(p for p in top.glob(pattern) if not any(n.startswith(".") for n in p.relative_to(top).parts))


def write_json(path, value):
    """json.dump(value) into `path`; a path inside it is written as its text (str(path))."""
    with Path(path).open("w", encoding="utf-8") as fh:
        json.dump(value, fh, default=os.fspath)


def item(ident, v, t=None):
    return {"id": str(ident), "v": v, "t": T0 + int(ident) * 3_600_000 if t is None and str(ident).isdigit() else t}


class ToyFixture:
    """<base>/in/toy/{a,b,m} and a data root, with a config.json that points at them."""

    def __init__(self):
        self.base = Path(tempfile.mkdtemp(prefix="llmw-toy-", dir=TEST_PARENT))
        self.toy = self.base / "in" / "toy"
        self.a, self.b, self.m = (self.toy / d for d in ("a", "b", "m"))
        self.data_root = self.base / "data"
        self.frozen = self.base / "frozen"
        for d in (self.a, self.b, self.m, self.data_root, self.frozen):
            d.mkdir(parents=True, exist_ok=True)
        self.m_list([])
        self.config_path = self.base / "config.json"
        self.write_config()

    # ---- config
    def write_config(self, data_root=None, **extra):
        dr = Path(data_root or self.data_root)
        dr.mkdir(parents=True, exist_ok=True)
        cfg = {"v": 1, "impl": paths.IMPL_NAME, "data_root": dr, "inputs_root": self.frozen,
               "sources": {"toy_dir": self.toy}}
        cfg.update(extra)
        write_json(self.config_path, cfg)
        return self.config_path

    def set_cfg(self, **kv):
        """Set top-level config keys; None removes one."""
        with self.config_path.open(encoding="utf-8") as fh:
            cfg = json.load(fh)
        for k, v in kv.items():
            if v is None:
                cfg.pop(k, None)
            else:
                cfg[k] = v
        write_json(self.config_path, cfg)

    def cfg(self):
        return Config(self.config_path)

    # ---- inputs
    @staticmethod
    def write(root, name, rows):
        """<root>/<name>.jsonl from dicts (item(), {"fail": ...}) and raw str lines."""
        lines = [r if isinstance(r, str) else json.dumps(r, ensure_ascii=False) for r in rows]
        (Path(root) / (name + ".jsonl")).write_text("".join(ln + "\n" for ln in lines), encoding="utf-8",
                                                    newline="\n")

    def a_put(self, name, rows):
        self.write(self.a, name, rows)

    def b_put(self, name, rows, partial=False):
        self.write(self.b, name, rows)
        self.b_partial(name, partial)

    def b_partial(self, name, on=True):
        p = self.b / (name + ".partial")
        if on:
            p.write_bytes(b"")
        else:
            p.unlink(missing_ok=True)

    def m_put(self, name, rows):
        self.write(self.m, name, rows)

    def m_list(self, names):
        write_json(self.m / "list.json", list(names))

    def m_stamp(self, value):
        (self.m / "STAMP").write_text(value, encoding="utf-8")

    def remove(self, root, name):
        (Path(root) / (name + ".jsonl")).unlink()

    def standard(self):
        """a: c1 (1, 2), c2 (3); b: r1 (1..20), r2 (21..30); m: c1 (1, 2), c2 (3)."""
        self.a_put("c1", [item(1, "회의는 10월에 합니다"), item(2, "가나다라 테스트")])
        self.a_put("c2", [item(3, "API 키 발급")])
        self.b_put("r1", [item(i, f"b{i}") for i in range(1, 21)])
        self.b_put("r2", [item(i, f"b{i}") for i in range(21, 31)])
        self.m_put("c1", [item(1, "rapid 배포 준비"), item(2, "답글 하나")])
        self.m_put("c2", [item(3, "잡담 메시지")])
        self.m_list(["c1", "c2"])
        return self


@contextlib.contextmanager
def preserved_env():
    """capture.run points TMP/TEMP and tempfile.tempdir at raw/.work/<run>; put them back afterwards."""
    env = dict(os.environ)
    td = tempfile.tempdir
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(env)
        tempfile.tempdir = td


def capture(fx, origins=ORIGINS, input_set="live"):
    """One capture run (precheck skipped). Returns its exit code."""
    with preserved_env(), contextlib.redirect_stdout(io.StringIO()):
        return run.capture(fx.cfg(), input_set, list(origins), skip_precheck=True)


def cli_main(fx, *argv):
    """The internal capture CLI in-process (its replay/export-state actions included), with --config; returns the
    exit code. A path in argv is passed as its text, as a command line passes it."""
    with preserved_env(), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        return cli.main([os.fspath(a) for a in argv] + ["--config", os.fspath(fx.config_path)])


def ledger(fx):
    return Ledger(fx.cfg().raw)


def manifests(fx):
    lg = ledger(fx)
    return [lg.read_manifest(p) for _s, _r, p in lg.manifests()]


def records(fx):
    lg = ledger(fx)
    out = []
    for _s, _r, p in lg.manifests():
        out.extend(lg.iter_records(lg.read_manifest(p)))
    return out


def by_key(recs):
    out = {}
    for r in recs:
        out.setdefault(r["key"], []).append(r)
    return out


def export(fx):
    return list(cli.export_lines(ledger(fx)))


def invariants(fx):
    """({problem category: count}, {containers_checked, containers_skipped}) (tests/core/ledgercheck.py)."""
    return ledgercheck.check(fx.cfg().raw)


def replay_diff(fx):
    lg = ledger(fx)
    tmp = fx.base / "replay.sqlite"
    state.rebuild(lg, tmp).close()
    con = state.open_state(lg.state_dir / "state.sqlite")
    try:
        return sum(state.compare(con, tmp).values())
    finally:
        con.close()
        tmp.unlink()


def log_runs(fx):
    with ledger(fx).log_md.open(encoding="ascii") as fh:
        return [w[len("run="):] for ln in fh for w in ln.split() if w.startswith("run=")]


class Crash(Exception):
    pass


def crash_at(fx, step):
    """A capture that raises Crash at a commit step (run.CRASH_HOOK)."""
    def hook(name):
        if name == step:
            raise Crash(step)
    run.CRASH_HOOK = hook
    try:
        try:
            capture(fx)
        except Crash:
            return
        raise AssertionError("no crash at " + step)
    finally:
        run.CRASH_HOOK = None


def child(body, timeout=300):
    """Run `body` in a fresh interpreter with the toy app installed -> CompletedProcess."""
    return subprocess.run([sys.executable, "-s", "-B", "-c", CHILD_SETUP + body], cwd=REPO, capture_output=True,
                          text=True, timeout=timeout)


def child_capture(fx, before=""):
    """A capture in a child process (`before` runs first, e.g. to install a crash hook) -> exit code."""
    return child("from rawlayer.capture import fsio, run\n"
                 "from rawlayer.capture.config import Config\n"
                 + before +
                 f"sys.exit(run.capture(Config({str(fx.config_path)!r}), 'live', {list(ORIGINS)!r}, "
                 "skip_precheck=True))\n").returncode


def run_search(*argv):
    """The internal index/search CLI in-process -> (exit code, stdout, stderr). A path in argv is its text."""
    from rawlayer.search import cli as search_cli  # noqa: PLC0415
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = search_cli.main([os.fspath(a) for a in argv])
    return rc, out.getvalue(), err.getvalue()


class ToyCase(unittest.TestCase):
    """The toy app installed for the class; a fresh standard fixture per test (self.fx)."""
    APPS = (TOY,)
    STANDARD = True

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.enterClassContext(registry.use(list(cls.APPS)))

    def setUp(self):
        self.fx = ToyFixture()
        if self.STANDARD:
            self.fx.standard()
        self.addCleanup(force_rmtree, self.fx.base)

    def origin_doc(self, origin, n=-1):
        return manifests(self.fx)[n]["origins"][origin]
