# -*- coding: utf-8 -*-
"""python -s -B manage.py schedule <action> [<app>] -- the apps' scheduled tasks (each app's AppConfig.schedule),
registered with Windows' own schtasks.exe from a Task Scheduler XML definition built here: no script files, standard
library only.

  install [<app>] [--config CFG] [--python EXE] [--run-now | --dry-run]
          check the app's schedule, the config, the data root and the interpreter, then register the task (replacing
          one of the same name) and read it back; --run-now starts it once (the SYSTEM smoke run); --dry-run prints
          the definition and registers nothing. Without <app>: every app that has a schedule, one after the other,
          each with its default config (<DATA_PARENT>\\<app>\\config.json)
  status  [<app>] [--config CFG] [--python EXE]
          registered?, the definition against the app's schedule (and against CFG / EXE when given), state, last run
          and result, and the step lines of the newest run that finished a step in <data_root>\\reports\\task.log.
          Without <app>: every app that has a schedule
  run     [<app>]  start the task once now
  remove  [<app>]  unregister the task
  (<app> may be left out of run and remove in a project with a single app)

An app's task runs `<python> -s -B <project>\\manage.py collect <app> --config <CFG>` in the project folder as SYSTEM
at the highest run level: daily from `start` (local time) and every `every_hours` hours after it that day, at most
`limit_hours` hours per run. A start is ignored while a run is still going, a start missed while the machine was off
runs when it is back, and batteries neither block nor stop it. The project folder and the config are registered by
their real path (on-disk case, junctions and subst drives resolved), so SYSTEM sees the same files and a prompt that
spells the path differently still matches. A task registered from the earlier layout of one project per folder
(`<folder>\\manage.py collect --config <CFG>`) is reported as such by status: install replaces it.

install refuses before anything is registered when the task would fail: the schedule malformed, the config refused
(Config: paths, data parent), the data root unsafe or not verifiable (precheck: outside git, inherited ACL, Search
exclusion), the app's task_checks(cfg) reporting a problem, or an interpreter SYSTEM cannot start. install, run and
remove change the machine's task store and need an elevated prompt; --dry-run and status do not (reading a SYSTEM
task without elevation may fail, and status then says so).

Exit codes: 0 ok | 2 refused or usage | 1 error (schtasks failed) | 3 needs attention: the definition read back
differs (install, status), the task is not registered or not readable, its last result is not ok, or a step of the
newest run that finished one in task.log ended non-zero (status). For several apps, the worst of them."""
import argparse
import datetime
import json
import os
import re
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import NamedTuple
from xml.sax.saxutils import escape

from . import conf, registry

NS = "http://schemas.microsoft.com/windows/2004/02/mit/task"
SYSTEM_SID = "S-1-5-18"
KEYS = ("task", "start", "every_hours", "limit_hours", "description")
_TASK = re.compile(r"^(\\[A-Za-z0-9][A-Za-z0-9 ._-]{0,63})+$")
_START = re.compile(r"^([01][0-9]|2[0-3]):[0-5][0-9]$")
_DECL = re.compile(r"^\s*<\?xml[^>]*\?>")
# list2cmdline output for paths, which never hold '"': split where Windows' argv parser splits, on space and tab
# only (a path may hold other white space, U+3000 or U+00A0, unquoted)
_TOKEN = re.compile(r'"([^"]*)"|([^ \t]+)')
# collect's task.log (rawlayer/collect.py): "run" before every run, then one line per step it finished
_STEP = re.compile(r"^\S+ step=([a-z_-]+) rc=(-?[0-9]+) seconds=-?[0-9]+$")
_LAST_STEP = "index"
# schtasks /Query /FO CSV /V /NH: HostName, TaskName, Next Run Time, Status, Logon Mode, Last Run Time, Last Result, ...
# The later fields (the command line) are written without escaping their quotes, so only this prefix is parsed.
_CSV_HEAD = re.compile(r'^"([^"]*)","([^"]*)","([^"]*)","([^"]*)","([^"]*)","([^"]*)","([^"]*)"')
# Last Result: collect's exit code (capture's) or a Task Scheduler code
RESULTS = {0: "ok", 1: "capture error", 2: "capture refused", 3: "capture suspect",
           75: "capture busy, or an origin skipped", 124: "capture timed out",
           0x41301: "running now", 0x41303: "has not run yet", 0x41306: "stopped (time limit, or by hand)",
           0x80070002: "the interpreter or a file the action names was not found"}
RESULT_OK = (0, 0x41301, 0x41303)


class ScheduleError(Exception):
    """An app's schedule is malformed."""


class Schedule(NamedTuple):
    task: str           # Task Scheduler path, e.g. \folder\name
    start: str          # HH:MM, local time
    every_hours: int    # 1..24 (24: once a day)
    limit_hours: int    # 1..72
    description: str


def _int(v, lo, hi):
    return isinstance(v, int) and not isinstance(v, bool) and lo <= v <= hi


def parse(value, where="schedule"):
    """An AppConfig.schedule -> Schedule, or None when the app has none. Raises ScheduleError when malformed."""
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) - set(KEYS) or set(KEYS[:4]) - set(value):
        raise ScheduleError(f"{where}: a dict with task, start, every_hours, limit_hours and an optional description")
    task, start, every, limit = (value[k] for k in KEYS[:4])
    description = value.get("description", "")
    if not isinstance(task, str) or not _TASK.fullmatch(task):
        raise ScheduleError(f"{where} task: a Task Scheduler path such as \\folder\\name")
    if not isinstance(start, str) or not _START.fullmatch(start):
        raise ScheduleError(f"{where} start: HH:MM")
    if not _int(every, 1, 24):
        raise ScheduleError(f"{where} every_hours: an integer 1..24")
    if not _int(limit, 1, 72):
        raise ScheduleError(f"{where} limit_hours: an integer 1..72")
    if not isinstance(description, str) or len(description) > 500 or not description.isprintable():
        raise ScheduleError(f"{where} description: printable text, at most 500 characters")
    return Schedule(task, start, every, limit, description)


def _schedule_value(app):
    """The raw AppConfig.schedule of `app` (the selected app's registry, or the app loaded on its own)."""
    reg = registry.get() if registry.selected() == app else registry.app_registry(app)
    return reg.apps[0].schedule


def task_settings(app):
    return parse(_schedule_value(app), f"{app}: schedule")


def describe(s):
    when = f"daily at {s.start}" if s.every_hours == 24 else f"every {s.every_hours} h from {s.start}"
    return f"{when} (local time), at most {s.limit_hours} h per run"


def _project():
    """The project folder by its real path: on-disk case, junctions and subst drives resolved."""
    return Path(conf.PROJECT_DIR).resolve()


def arguments(project, app, config):
    """The registered Arguments text: str() of each path (a config given as text is kept as written)."""
    return subprocess.list2cmdline(["-s", "-B", str(Path(project) / "manage.py"), "collect", app, "--config",
                                    str(config)])


def task_xml(s, python, project, app, config, today=None):
    """The Task Scheduler definition (schema 1.2) install registers. The paths go in as str() of each."""
    today = today or datetime.date.today()
    repetition = "" if s.every_hours == 24 else (
        "      <Repetition>\n"
        f"        <Interval>PT{s.every_hours}H</Interval>\n"
        "        <Duration>P1D</Duration>\n"
        "        <StopAtDurationEnd>false</StopAtDurationEnd>\n"
        "      </Repetition>\n")
    return ('<?xml version="1.0" encoding="UTF-16"?>\n'
            f'<Task version="1.2" xmlns="{NS}">\n'
            "  <RegistrationInfo>\n"
            f"    <Description>{escape(s.description)}</Description>\n"
            "  </RegistrationInfo>\n"
            "  <Triggers>\n"
            "    <CalendarTrigger>\n"
            f"      <StartBoundary>{today.isoformat()}T{s.start}:00</StartBoundary>\n"
            "      <Enabled>true</Enabled>\n"
            f"{repetition}"
            "      <ScheduleByDay>\n"
            "        <DaysInterval>1</DaysInterval>\n"
            "      </ScheduleByDay>\n"
            "    </CalendarTrigger>\n"
            "  </Triggers>\n"
            "  <Principals>\n"
            '    <Principal id="Author">\n'
            f"      <UserId>{SYSTEM_SID}</UserId>\n"
            "      <RunLevel>HighestAvailable</RunLevel>\n"
            "    </Principal>\n"
            "  </Principals>\n"
            "  <Settings>\n"
            "    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>\n"
            "    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>\n"
            "    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>\n"
            "    <StartWhenAvailable>true</StartWhenAvailable>\n"
            "    <Enabled>true</Enabled>\n"
            f"    <ExecutionTimeLimit>PT{s.limit_hours}H</ExecutionTimeLimit>\n"
            "  </Settings>\n"
            '  <Actions Context="Author">\n'
            "    <Exec>\n"
            f"      <Command>{escape(str(python))}</Command>\n"
            f"      <Arguments>{escape(arguments(project, app, config))}</Arguments>\n"
            f"      <WorkingDirectory>{escape(str(project))}</WorkingDirectory>\n"
            "    </Exec>\n"
            "  </Actions>\n"
            "</Task>\n")


# ---- reading a definition back: Task Scheduler drops values equal to the schema defaults, so both sides are read
# through the same defaults before they are compared; paths compare as Windows compares them (no case)
_FIELDS = {
    "description": ("RegistrationInfo/Description", ""),
    "user": ("Principals/Principal/UserId", None),
    "run_level": ("Principals/Principal/RunLevel", "LeastPrivilege"),
    "start": ("Triggers/CalendarTrigger/StartBoundary", None),
    "trigger_enabled": ("Triggers/CalendarTrigger/Enabled", "true"),
    "interval": ("Triggers/CalendarTrigger/Repetition/Interval", None),
    "duration": ("Triggers/CalendarTrigger/Repetition/Duration", None),
    "stop_at_duration_end": ("Triggers/CalendarTrigger/Repetition/StopAtDurationEnd", "false"),
    "days_interval": ("Triggers/CalendarTrigger/ScheduleByDay/DaysInterval", "1"),
    "instances": ("Settings/MultipleInstancesPolicy", "IgnoreNew"),
    "disallow_on_batteries": ("Settings/DisallowStartIfOnBatteries", "true"),
    "stop_on_batteries": ("Settings/StopIfGoingOnBatteries", "true"),
    "start_when_available": ("Settings/StartWhenAvailable", "false"),
    "enabled": ("Settings/Enabled", "true"),
    "time_limit": ("Settings/ExecutionTimeLimit", "PT72H"),
    "command": ("Actions/Exec/Command", None),
    "arguments": ("Actions/Exec/Arguments", None),
    "working_directory": ("Actions/Exec/WorkingDirectory", None),
}
_CASELESS = ("user", "run_level", "instances", "disallow_on_batteries", "stop_on_batteries", "start_when_available",
             "enabled", "trigger_enabled", "stop_at_duration_end")
_PATHS = ("command", "working_directory")


def _root(xml_text):
    return ET.fromstring(_DECL.sub("", xml_text.lstrip("\ufeff"), count=1))


def _path_key(p):
    return os.path.normcase(os.path.normpath(p))


def _argv(args):
    return [q or b for q, b in _TOKEN.findall(args or "")]


def fields(xml_text):
    """The facts of a definition this module compares, read through the schema defaults."""
    root = _root(xml_text)
    out = {}
    for name, (path, default) in _FIELDS.items():
        node = root.find("/".join(f"{{{NS}}}{p}" for p in path.split("/")))
        v = node.text.strip() if node is not None and node.text is not None else None
        if v is None or v == "":
            v = default
        if v is not None and name in _CASELESS:
            v = v.casefold()
        elif v is not None and name in _PATHS:
            v = _path_key(v)
        elif v is not None and name == "arguments":       # the flags exactly (-s is not -S), the paths as paths
            v = tuple(_path_key(t) if Path(t).is_absolute() else t for t in _argv(v))
        out[name] = v
    if out["user"] in ("system", "nt authority\\system"):
        out["user"] = SYSTEM_SID.casefold()
    if out["start"]:
        out["start"] = out["start"].partition("T")[2][:5]        # the time of day; the date is the day it was made
    for group in ("Triggers", "Actions"):
        node = root.find(f"{{{NS}}}{group}")
        out[group.lower()] = tuple(c.tag.rpartition("}")[2] for c in node) if node is not None else ()
    return out


def differences(actual_xml, expected_xml):
    a, e = fields(actual_xml), fields(expected_xml)
    return [k for k in e if a.get(k) != e[k]]


def registered_config(project, app, args):
    """The --config path (a Path) of registered Arguments that run `project`'s `manage.py collect <app> --config
    <path>` (quoted or not, the project path in any spelling Windows treats as the same), else None."""
    t = _argv(args) if isinstance(args, str) else []
    if len(t) == 7 and t[:2] == ["-s", "-B"] and t[3:6] == ["collect", app, "--config"] and \
            _path_key(t[2]) == _path_key(Path(project) / "manage.py"):
        return Path(t[6])
    return None


def old_layout(args):
    """True for Arguments registered from the earlier layout of one project per folder: `-s -B <folder>\\manage.py
    collect --config <path>` (install replaces such a task)."""
    t = _argv(args) if isinstance(args, str) else []
    # the token as text: basename is "" after a trailing separator, which Path.name would drop
    return len(t) == 6 and t[:2] == ["-s", "-B"] and t[3:5] == ["collect", "--config"] and \
        os.path.basename(t[2]).casefold() == "manage.py"


# ---- the machine
def _windows():
    return os.name == "nt"


def elevated():
    try:
        import ctypes  # noqa: PLC0415
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except (ImportError, AttributeError, OSError):
        return False


def _console_cp():
    try:
        import ctypes  # noqa: PLC0415
        return ctypes.windll.kernel32.GetConsoleOutputCP()
    except (ImportError, AttributeError, OSError):
        return 0


def _text(b):
    """schtasks output: UTF-16 with a BOM, else UTF-8, else the console's output code page (what schtasks writes
    a pipe in), else the ANSI code page."""
    if b[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return b.decode("utf-16", "replace")
    cp = _console_cp()
    for enc in ["utf-8"] + ([f"cp{cp}"] if cp else []) + (["mbcs"] if _windows() else []):
        try:
            return b.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return b.decode("utf-8", "replace")


def system_dir():
    """Windows' System32 folder (a Path), asked of Windows itself (GetSystemDirectoryW): neither PATH nor the
    environment is trusted, and no drive is assumed. OSError elsewhere: there is none."""
    if os.name != "nt":
        raise OSError("no Windows system folder on this platform")
    import ctypes  # noqa: PLC0415
    buf = ctypes.create_unicode_buffer(32768)
    n = ctypes.windll.kernel32.GetSystemDirectoryW(buf, len(buf))
    if not 0 < n < len(buf):
        raise OSError("GetSystemDirectoryW failed")
    return Path(buf.value)


def schtasks(*args):
    """(rc, stdout, stderr) of Windows' schtasks.exe, by its full path (PATH is not trusted)."""
    try:
        exe = system_dir() / "schtasks.exe"
        p = subprocess.run([str(exe), *args], capture_output=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as e:
        return 1, "", f"schtasks could not run ({type(e).__name__})"
    return p.returncode, _text(p.stdout), _text(p.stderr)


def _first(*texts):
    for t in texts:
        for ln in (t or "").splitlines():
            if ln.strip():
                return ln.strip()[:300]
    return "no message"


def python_problem(exe):
    exe = Path(exe)
    if not exe.is_absolute() or not exe.is_file():
        return "the interpreter is not an existing file (pass --python <path of python.exe>)"
    if "\\windowsapps\\" in os.path.normcase(exe):
        return "the interpreter is a Microsoft Store alias, which SYSTEM cannot start (pass --python <python.exe>)"
    return None


def task_problems(cfg, apps):
    """['app <label>: <problem>'] from every app's task_checks(cfg). A hook that raises, or returns anything but a
    list of strings, is a problem too: an app's mistake never takes the command down."""
    out = []
    for app in apps:
        try:
            found = app.task_checks(cfg)
        except Exception as e:  # noqa: BLE001
            out.append(f"app {app.label}: task_checks failed (error {type(e).__name__})")
            continue
        if not isinstance(found, (list, tuple)) or not all(isinstance(p, str) for p in found):
            out.append(f"app {app.label}: task_checks must return a list of strings")
            continue
        out += [f"app {app.label}: {p}" for p in found]
    return out


def _refuse(msg):
    print(f"schedule: refused: {msg}", file=sys.stderr)
    return 2


def _settings_or_refuse(app):
    try:
        s = task_settings(app)
    except ScheduleError as e:
        return None, _refuse(str(e))
    except registry.RegistryError as e:
        return None, _refuse(f"app {app}: {e}")
    except Exception as e:  # noqa: BLE001 -- an app that does not import
        return None, _refuse(f"app {app} does not load (error {type(e).__name__})")
    if s is None:
        return None, _refuse(f"{app} has no schedule (AppConfig.schedule is not set)")
    if not _windows():
        return None, _refuse("the scheduled task needs Windows Task Scheduler")
    return s, None


def _ready(config):
    """Config, precheck and the selected app's task checks: the reasons collect would refuse, or None. A check that
    cannot be completed (say Get-Acl failing) is a reason too: install registers only what it could verify."""
    from .capture import ledger, precheck  # noqa: PLC0415
    from .capture.config import Config, ConfigError  # noqa: PLC0415
    try:
        cfg = Config(config)
        if ledger.foreign_origins(cfg.raw):
            return "the data root's ledger belongs to another app"
        precheck.check(cfg.data_root)
    except ConfigError as e:
        return f"config refused ({e})"
    except registry.RegistryError as e:
        return f"app contract broken ({e})"
    except Exception as e:  # noqa: BLE001
        return f"the config or the data root could not be checked (error {type(e).__name__})"
    return "; ".join(task_problems(cfg, registry.apps())) or None


def _default_config(app):
    from .management.base import default_config  # noqa: PLC0415
    return default_config(app)


# ---- actions
def install(a, app):
    s, rc = _settings_or_refuse(app)
    if s is None:
        return rc
    try:
        registry.select(app)            # Config and task_checks are the app's
    except registry.RegistryError as e:
        return _refuse(f"app {app}: {e}")
    # not resolve(): a venv's python must stay the venv's; abspath, not Path.absolute(), which leaves ".." in place
    python = Path(os.path.abspath(a.python or sys.executable))
    problem = python_problem(python)
    if problem:
        return _refuse(problem)
    project, config = _project(), Path(a.config or _default_config(app)).resolve()
    problem = _ready(config)
    if problem:
        return _refuse(problem)
    xml = task_xml(s, python, project, app, config)
    if a.dry_run:
        print(xml, end="")
        return 0
    if not elevated():
        return _refuse("install registers a SYSTEM task: run it from an elevated prompt")
    fd, tmp = tempfile.mkstemp(prefix="rawlayer-task-", suffix=".xml")
    tmp = Path(tmp)
    try:
        with os.fdopen(fd, "w", encoding="utf-16") as fh:           # BOM + UTF-16, as the declaration says
            fh.write(xml)
        rc, out, err = schtasks("/Create", "/TN", s.task, "/XML", str(tmp), "/F")
    finally:
        tmp.unlink()
    if rc != 0:
        print(f"schedule: registering {s.task} failed (schtasks rc={rc}: {_first(err, out)})", file=sys.stderr)
        return 1
    print(f"schedule: registered {s.task}: {describe(s)}, as SYSTEM")
    print(f"  runs: {python} {arguments(project, app, config)}")
    print(f"  in:   {project}")
    back, err = _definition(s.task)
    if back is None:
        print(f"schedule: {s.task} could not be read back ({err})", file=sys.stderr)
        return 3
    diff = differences(back, xml)
    if diff:
        print(f"schedule: the definition read back differs: {', '.join(diff)}", file=sys.stderr)
        return 3
    print("  definition read back: as registered")
    if a.run_now:
        return _start(s)
    return 0


def _start(s):
    rc, out, err = schtasks("/Run", "/TN", s.task)
    if rc != 0:
        print(f"schedule: starting {s.task} failed (schtasks rc={rc}: {_first(err, out)})", file=sys.stderr)
        return 1
    print(f"schedule: started {s.task} once; `manage.py schedule status` shows how it went")
    return 0


def _definition(task):
    rc, out, err = schtasks("/Query", "/TN", task, "/XML")
    if rc != 0:
        return None, _first(err, out)
    try:
        _root(out)
    except ET.ParseError:
        return None, "schtasks returned a definition that does not parse"
    return out, None


def _run_info(task):
    rc, out, _err = schtasks("/Query", "/TN", task, "/FO", "CSV", "/V", "/NH")
    if rc != 0:
        return None
    for ln in out.splitlines():
        m = _CSV_HEAD.match(ln.strip())
        if m and m.group(2).casefold() == task.casefold():
            return {"next_run": m.group(3), "state": m.group(4), "last_run": m.group(6), "last_result": m.group(7)}
    return None


def result_text(raw):
    """(display, ok) for a Last Result value as schtasks prints it (decimal, signed for HRESULTs)."""
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return str(raw), False
    u = v & 0xFFFFFFFF
    shown = str(u) if u < 0x10000 else f"0x{u:08X}"
    label = RESULTS.get(u)
    return (f"{shown} ({label})" if label else shown), u in RESULT_OK


def _runs(path):
    """The runs of one task log, oldest first: [[(line, step, rc)]], the step lines only. Raises OSError."""
    with Path(path).open("rb") as fh:
        fh.seek(0, os.SEEK_END)
        fh.seek(max(0, fh.tell() - 256 * 1024))
        tail = fh.read().decode("utf-8", "replace")
    runs = [[]]
    for ln in tail.splitlines():
        if ln == "run":
            runs.append([])
        else:
            m = _STEP.fullmatch(ln)
            if m:
                runs[-1].append((ln, m.group(1), int(m.group(2))))
    return runs


def last_run(data_root):
    """What task.log (after task.log.1, the log it rolled over from) says about the newest run that finished a step:
    {"steps": [(line, step, rc)], "complete": it reached collect's last step, "newer_started": a later run has
    started and finished no step yet}, or {"error": type} when a log cannot be read, or None when there is none.
    Only the step lines are kept, never the children's output under them."""
    reports = Path(data_root) / "reports"
    runs, found = [], False
    for p in (reports / "task.log.1", reports / "task.log"):
        try:
            runs += _runs(p)
            found = True
        except FileNotFoundError:
            continue
        except OSError as e:
            return {"error": type(e).__name__}
    if not found:
        return None
    with_steps = [r for r in runs if r]
    last = with_steps[-1] if with_steps else []
    return {"steps": last, "complete": bool(last) and last[-1][1] == _LAST_STEP,
            "newer_started": len(runs) > 1 and not runs[-1]}


def _data_root(config):
    """The config's data_root as a Path, when it is an absolute path, else None (the config, a path or its text, is
    only read)."""
    try:
        # open() of the config as given, not Path(config): Path drops a trailing separator open() refuses
        with open(config, encoding="utf-8") as fh:
            d = json.load(fh).get("data_root")
    except (OSError, ValueError, AttributeError):
        return None
    if not isinstance(d, str):
        return None
    d = Path(d)
    return d if d.is_absolute() else None


def _task_log(data_root):
    """Prints what task.log says; True when a step of the newest run that finished one ended non-zero."""
    info = last_run(data_root)
    if info is None:
        from .capture import paths  # noqa: PLC0415
        refused = Path(paths.DATA_PARENT).resolve() / "task-refused.log"
        note = f"; {refused} exists (a collect run refused its data root)" if refused.exists() else ""
        print(f"  task.log: none yet{note}")
        return False
    if "error" in info:
        print(f"  task.log: not readable from this prompt ({info['error']})")
        return False
    newer = info["newer_started"]
    if newer:
        print("  task.log: a newer run has started (running now, or stopped) and not finished a step yet")
    if not info["steps"]:
        print("  task.log: no step finished yet")
        return False
    if info["complete"]:
        label = "previous run" if newer else "newest run"
    elif newer:     # one run at a time (IgnoreNew, the run lock): it stopped before its last step
        label = f"previous run, incomplete (stopped before its {_LAST_STEP} step)"
    else:
        label = f"newest run, incomplete (no {_LAST_STEP} step yet: still running, or stopped)"
    print(f"  task.log, {label}:")
    for ln, _step, rc in info["steps"]:
        print(f"    {ln}{'   <- not ok' if rc != 0 else ''}")
    return any(rc != 0 for _ln, _step, rc in info["steps"])


def status(a, app):
    s, rc = _settings_or_refuse(app)
    if s is None:
        return rc
    back, err = _definition(s.task)
    if back is None:
        print(f"schedule: {s.task} is not registered, or not readable from this prompt ({err})")
        print(f"  register it with: python -s -B manage.py schedule install {app}   (elevated)")
        return 3
    attention = False
    project = _project()
    f = fields(back)
    exec_ = _root(back).find(f"{{{NS}}}Actions/{{{NS}}}Exec")
    registered_python = (exec_.findtext(f"{{{NS}}}Command") if exec_ is not None else None) or ""
    registered_args = (exec_.findtext(f"{{{NS}}}Arguments") if exec_ is not None else None) or ""
    registered = registered_config(project, app, registered_args)
    legacy = registered is None and old_layout(registered_args)
    # the registered --config as its text, not str() of a Path: a relative one written with "/" or a "." part
    # would no longer match the definition read back (only an absolute token is compared as a path)
    t = _argv(registered_args)
    config = Path(a.config).resolve() if a.config else (
        t[6] if registered is not None else (t[5] if legacy else None))
    python = Path(os.path.abspath(a.python)) if a.python else registered_python     # as install, not resolve()
    print(f"schedule: {s.task} registered ({'enabled' if f['enabled'] == 'true' else 'DISABLED'})")
    print(f"  runs: {registered_python} {registered_args}")
    if legacy:
        attention = True
        print(f"  definition: registered from the earlier layout (one project per folder): re-run "
              f"`manage.py schedule install {app}`")
    elif registered is None:
        attention = True
        print(f"  definition: its action is not this project's `manage.py collect {app} --config <config>` "
              f"(re-run install)")
    else:
        diff = differences(back, task_xml(s, python, project, app, config))
        if diff:
            attention = True
            given = " and the options given" if a.config or a.python else ""
            print(f"  definition: differs from {app}'s schedule{given}: {', '.join(diff)}  (re-run install)")
        else:
            print(f"  definition: current ({describe(s)}, as SYSTEM)")
    info = _run_info(s.task)
    if info is None:
        attention = True
        print("  run info: unavailable")
    else:
        shown, ok = result_text(info["last_result"])
        attention = attention or not ok
        print(f"  state: {info['state']}; last run: {info['last_run']}; last result: {shown}; "
              f"next run: {info['next_run']}")
    data_root = _data_root(config) if config else None
    if data_root and _task_log(data_root):
        attention = True
    return 3 if attention else 0


def run_now(_a, app):
    s, rc = _settings_or_refuse(app)
    if s is None:
        return rc
    if not elevated():
        return _refuse("starting a SYSTEM task needs an elevated prompt")
    return _start(s)


def remove(_a, app):
    s, rc = _settings_or_refuse(app)
    if s is None:
        return rc
    if not elevated():
        return _refuse("removing a SYSTEM task needs an elevated prompt")
    rc, out, err = schtasks("/Delete", "/TN", s.task, "/F")
    if rc != 0:
        print(f"schedule: removing {s.task} failed (schtasks rc={rc}: {_first(err, out)})", file=sys.stderr)
        return 1
    print(f"schedule: removed {s.task}")
    return 0


_SEVERITY = {0: 0, 3: 1, 2: 2, 1: 3}


def worst(*rcs):
    """The exit code of several apps: an error over a refusal over attention over ok."""
    return max(rcs, key=lambda rc: _SEVERITY.get(rc, 3), default=0)


def scheduled(apps):
    """[(app, Schedule or None, problem or None)] for the given apps (each loaded on its own)."""
    out = []
    for app in apps:
        try:
            out.append((app, task_settings(app), None))
        except ScheduleError as e:
            out.append((app, None, str(e)))
        except registry.RegistryError as e:
            out.append((app, None, f"app {app}: {e}"))
        except Exception as e:  # noqa: BLE001
            out.append((app, None, f"app {app} does not load (error {type(e).__name__})"))
    return out


def _every(a, apps):
    """install / status for every app that has a schedule. install runs each app in a process of its own (one
    process works on one app: rawlayer/registry.py)."""
    found, rc = [], 0
    for app, s, problem in scheduled(apps):
        if problem:
            rc = worst(rc, _refuse(problem))
        elif s is not None:
            found.append(app)
    if not found:
        return worst(rc, _refuse("no app has a schedule"))
    project = Path(conf.PROJECT_DIR)
    for i, app in enumerate(found):
        if a.action == "install":
            argv = [sys.executable, "-s", "-B", str(project / "manage.py"), "schedule", "install", app]
            argv += (["--python", a.python] if a.python else []) + (["--run-now"] if a.run_now else []) + \
                (["--dry-run"] if a.dry_run else [])
            print(f"schedule: == {app}", flush=True)
            rc = worst(rc, subprocess.run(argv, cwd=project).returncode)
        else:
            if i:
                print()
            rc = worst(rc, status(a, app))
    return rc


def main(argv=None):
    ap = argparse.ArgumentParser(prog="manage.py schedule", description="The apps' scheduled tasks "
                                 "(AppConfig.schedule): manage.py collect <app> as SYSTEM, through schtasks.exe.")
    sub = ap.add_subparsers(dest="action", required=True)
    p = sub.add_parser("install", help="register (or replace) the app's task, or every app's; elevated")
    p.add_argument("app", nargs="?", help="the app (default: every app that has a schedule)")
    p.add_argument("--config", help="the config.json collect runs with (default: <DATA_PARENT>\\<app>\\config.json)")
    p.add_argument("--python", help="the interpreter the task starts (default: this one)")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--run-now", action="store_true", help="start the task once after registering it")
    g.add_argument("--dry-run", action="store_true", help="print the definition; register nothing")
    p = sub.add_parser("status", help="the registered task, its last run, and task.log (of every app by default)")
    p.add_argument("app", nargs="?")
    p.add_argument("--config", help="also compare the registered config path with this one")
    p.add_argument("--python", help="also compare the registered interpreter with this one")
    for action, text in (("run", "start the app's task once now; elevated"), ("remove", "unregister it; elevated")):
        sub.add_parser(action, help=text).add_argument("app", nargs="?")
    a = ap.parse_args(argv)
    apps = registry.discover()
    if a.app is not None and a.app not in apps:
        return _refuse(f"no app {a.app!r} in this project (apps: {', '.join(apps) or 'none'})")
    if a.app is None and a.action in ("run", "remove"):
        if len(apps) != 1:
            return _refuse(f"name the app: manage.py schedule {a.action} <app>")
        a.app = apps[0]
    if a.app is None:
        if getattr(a, "config", None):
            return _refuse(f"--config belongs to one app: manage.py schedule {a.action} <app> --config CFG")
        return _every(a, apps)
    return {"install": install, "status": status, "run": run_now, "remove": remove}[a.action](a, a.app)
