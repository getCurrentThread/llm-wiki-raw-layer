# -*- coding: utf-8 -*-
"""The shared preamble of an app's test helpers: one capture run, the ledger read back and checked, the
replay comparison, the raw-search runner and the clone/damage utilities of the search tests. An app's
tests/helpers.py and tests/ledgerfix.py re-export these next to their origins list and scenario builders
(the ledger invariants themselves live in ledgercheck.py)."""
import contextlib
import datetime as _dt
import io
import json
import os
import shutil
import stat
import tempfile
from pathlib import Path

from rawlayer.capture import cli, run, state
from rawlayer.capture.config import Config
from rawlayer.capture.ledger import Ledger
from tests.core import ledgercheck

KST = _dt.timezone(_dt.timedelta(hours=9))


def kst(y, mo, d, h=12, mi=0):
    return int(_dt.datetime(y, mo, d, h, mi, tzinfo=KST).timestamp())


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


def capture(fx, origins):
    """One capture run (input set live, precheck skipped as in the capture tests). Returns its exit code."""
    with preserved_env(), contextlib.redirect_stdout(io.StringIO()):
        return run.capture(Config(fx.config_path), "live", list(origins), skip_precheck=True)


def records(fx):
    lg = Ledger(Config(fx.config_path).raw)
    out = []
    for _s, _r, p in lg.manifests():
        out.extend(lg.iter_records(lg.read_manifest(p)))
    return out


def by_key(recs):
    out = {}
    for r in recs:
        out.setdefault(r["key"], []).append(r)
    return out


def manifests(fx):
    lg = Ledger(Config(fx.config_path).raw)
    return [lg.read_manifest(p) for _s, _r, p in lg.manifests()]


def export(fx):
    return list(cli.export_lines(Ledger(Config(fx.config_path).raw)))


def invariants(fx):
    """({problem category: count}, {containers_checked, containers_skipped}) of the ledger (ledgercheck.py)."""
    return ledgercheck.check(Config(fx.config_path).raw)


def replay_diff(fx):
    cfg = Config(fx.config_path)
    lg = Ledger(cfg.raw)
    tmp = Path(fx.base) / "replay.sqlite"
    state.rebuild(lg, tmp).close()
    con = state.open_state(Path(lg.state_dir) / "state.sqlite")
    try:
        return sum(state.compare(con, tmp).values())
    finally:
        con.close()
        tmp.unlink()


def run_rawsearch(argv):
    """rawlayer.search.cli.main in-process -> (exit code, stdout, stderr). argv is `index ...` or `<label> ...`
    (the search subcommand of manage.py: `search <label> ...`); a path argument may be a Path (a command line is
    text: it is passed as str)."""
    from rawlayer.search import cli  # noqa: PLC0415
    argv = [os.fspath(a) for a in argv]
    if argv and argv[0] not in ("index", "search") and not argv[0].startswith("-"):
        argv = ["search"] + argv
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = cli.main(argv)
    return rc, out.getvalue(), err.getvalue()


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


def clone_data_root(src_data_root, parent):
    """Copy a data root (ledger + index) to a fresh temp folder under `parent` (the app fixtures' TEST_PARENT)
    and write a config for it. Returns (base folder, config path, data root), each a Path."""
    base = Path(tempfile.mkdtemp(prefix="rsx-", dir=parent))
    dr = base / "data"
    shutil.copytree(src_data_root, dr)
    cfg = base / "config.json"
    with cfg.open("w", encoding="utf-8") as fh:
        json.dump({"v": 1, "data_root": str(dr)}, fh)
    return base, cfg, dr


class LedgerSeq:
    """Paths of one data root's ledger by seq (tests that damage or hide ledger files)."""

    def __init__(self, data_root):
        self.raw = Path(data_root) / "raw"
        self.man_dir = self.raw / "manifests"

    def manifest(self, seq):
        names = [p.name for p in self.man_dir.iterdir()
                 if p.name.startswith(f"{seq:08d}-") and p.name.endswith(".json")]
        assert len(names) == 1, names
        return self.man_dir / names[0]

    def segments(self, seq):
        with self.manifest(seq).open(encoding="ascii") as fh:
            man = json.load(fh)
        return [self.raw.joinpath(*s["path"].split("/")) for s in man["segments"]]
