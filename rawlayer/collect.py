# -*- coding: utf-8 -*-
"""python -s -B manage.py collect <app> --config <config> -- the pipeline behind both the app's SYSTEM scheduled
task and a manual run: capture, then index.

1. capture (--input-set, live by default; --origins limits the run to some of the app's origins).
2. index, after EVERY capture exit code, which brings
   <data_root>/index/<app>.sqlite up to the committed ledger (--rebuild-index rebuilds it from scratch instead
   of catching up). Capture's exit code does not tell whether the ledger moved: 75 is also returned AFTER a
   commit when an origin or a container could not be read this run, and such a condition can last for many
   runs. The index reads only committed manifests, stops before one that is still mid-commit and reports
   defects itself, so running it after a refused, busy or failed capture is safe (a no-op when nothing new was
   committed), and the indexes never fall behind a ledger that keeps growing.

Both steps run in this process (the dispatcher has already selected the app). Every step is logged as
"<UTC> step=<name> rc=<rc> seconds=<n>" in <data_root>/reports/task.log. Capture's stdout is publicout-validated
(SPEC 11) and appended as is; its stderr is kept only when it is cli's own "error <Type> [errno=N]" line. The
index prints counts; its stdout and the last lines of its stderr are appended, each line cut at 300 characters.
The log rolls over at 4 MiB. Exit code = capture's (Task Scheduler's "last run result"; the task's own time
limit bounds a step that hangs)."""
import argparse
import contextlib
import datetime
import io
import json
import re
import time
import traceback
from pathlib import Path

from . import conf
from .capture.config import INPUT_SETS

LOG_MAX = 4 * 1024 * 1024
_ERR = re.compile(r"^error [A-Za-z][A-Za-z0-9_]{0,60}( errno=-?[0-9]{1,10})?$")
_LINE_MAX = 300
_STDERR_KEEP = 20


def _lines(name, out, err):
    if name == "capture":
        return out.splitlines() + [ln if _ERR.fullmatch(ln) else "stderr line withheld" for ln in err.splitlines()]
    return [ln[:_LINE_MAX] for ln in out.splitlines()] + \
        ["stderr: " + ln[:_LINE_MAX] for ln in err.splitlines()[-_STDERR_KEEP:]]


def one(name, argv):
    """A single step: the capture or index cli as the wrapper commands once presented them."""
    if name == "capture":
        from .capture import cli  # noqa: PLC0415
        return cli.main(["capture"] + argv)
    from .search import cli  # noqa: PLC0415
    return cli.main(["index"] + argv)


def step(log, name, argv):
    """One step in this process, logged with a header line plus its output (a step that dies logs rc=1 and its
    traceback as withheld stderr, as a child process crash would)."""
    t0 = time.time()
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            rc = one(name, argv)
        except SystemExit as e:                       # argparse's usage errors
            rc = e.code if isinstance(e.code, int) else 2
        except BaseException:  # noqa: BLE001
            traceback.print_exc()
            rc = 1
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    log.write(f"{stamp} step={name} rc={rc} seconds={int(time.time() - t0)}\n")
    for ln in _lines(name, out.getvalue(), err.getvalue()):
        log.write("  " + ln + "\n")
    log.flush()
    return rc


def log_path(config):
    """<data_root>/reports/task.log when data_root is an existing directory inside the data parent; otherwise
    <data parent>/task-refused.log. collect never creates data_root: a typo must reach capture, which refuses
    it (exit 2), instead of becoming a new empty ledger."""
    from .capture import paths  # noqa: PLC0415
    try:                        # the path as given, as Config opens it: Path() would drop a trailing separator
        with open(config, encoding="utf-8") as fh:
            data_root = json.load(fh).get("data_root")
    except (OSError, ValueError, AttributeError):
        data_root = None
    parent = Path(paths.DATA_PARENT).resolve()
    root = Path(data_root) if isinstance(data_root, str) else None
    # is_absolute() before is_dir(): Path("") is ".", an existing directory
    if root is not None and root.is_absolute() and root.is_dir() and paths.inside(root.resolve(), parent):
        reports = root / "reports"
        reports.mkdir(parents=True, exist_ok=True)
        return reports / "task.log"
    return parent / "task-refused.log"


def main(argv=None):
    """argv: <app> --config <config> [--input-set live|T1|T2] [--origins a,b] [--rebuild-index]
    (manage.py collect has selected the app)."""
    ap = argparse.ArgumentParser(prog="manage.py collect")
    ap.add_argument("app")
    ap.add_argument("--config", required=True)
    ap.add_argument("--input-set", default="live", choices=INPUT_SETS)
    ap.add_argument("--origins", default=None, help="capture only these origins (comma-separated)")
    ap.add_argument("--rebuild-index", action="store_true", help="rebuild the index instead of catching up")
    a = ap.parse_args(argv)
    capture_args = ["--config", a.config, "--input-set", a.input_set] + \
        (["--origins", a.origins] if a.origins else [])
    index_args = ["--config", a.config] + (["--rebuild"] if a.rebuild_index else [])
    path = Path(log_path(a.config))
    if path.exists() and path.stat().st_size > LOG_MAX:
        try:
            path.replace(path.with_name(path.name + ".1"))
        except OSError:                 # another collect has it open (Windows): that run's successor rolls it
            pass
    with path.open("a", encoding="utf-8", newline="\n") as log:
        log.write("run\n")
        log.flush()                     # on disk before capture starts: `schedule status` sees a run in progress
        rc = step(log, "capture", capture_args)
        step(log, "index", index_args)  # whatever rc is
    return rc
