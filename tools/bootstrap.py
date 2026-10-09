# -*- coding: utf-8 -*-
"""python -s -B <PROJECT>/tools/bootstrap.py <command> ... -- the setup steps of llm.md as commands, for the agent
that brings this project's apps into live operation. A developer and operator tool, not a manage.py command (the
core stays as it is); standard library only; it runs from any folder.

  doctor [--app APP ...] [--json] [--verify]
      The machine (Python, git on the machine PATH, pwsh, the checkout, the core, the data parent) and every
      in-scope app: its config (placeholders left, refused or not, the sync option, what `schedule install` would
      refuse), its credential and source files (whether they are there; nothing is opened), its index (total,
      stale), its scheduled task (registered, last result; "unknown" from a prompt that is not elevated), the
      newest task.log run and its checkpoints. Each app gets `next`: the llm.md step to do next. --verify also
      runs `manage.py check <app>`, which takes the app's ledger lock: never while a capture may run.
  config get <app> [--json]
  config set <app> KEY VALUE [KEY VALUE ...] [--string] [--force]      config set <app> -   (a JSON object on stdin)
  config defaults <app>
      Read or change <DATA_PARENT>\\<app>\\config.json with a JSON-safe edit (llm.md 5.3): KEY is sources.<key> or
      one of the app's options (v, impl, data_root and inputs_root need --force). A VALUE of true, false, null, a
      plain integer, or one starting with [ or { is JSON, anything else a string (--string: always a string). The
      file is rewritten as setup writes it, the one before kept as config.json.prev. `defaults` fills what the
      app's setup defaults would (only keys still unset). `get` prints each key's state, never its value.
  checkpoint show <app> | mark <app> GATE [--field NAME=VALUE ...] | clear <app> [GATE]
      The llm.md gates an app has passed, kept in <DATA_ROOT>\\reports\\bootstrap.json (gate ids and integer or
      boolean fields only), so a later session resumes where the last one stopped.
  agents status | install | uninstall | sync-repo ...
      The agent integration (tools/agentkit.py; `agents -h` for its options): the global CLAUDE.md block, the
      skills, the raw-retriever subagent and the MCP server for Claude Code, and the checkout's own copies.

Every path printed is masked (<DATA_PARENT>, <PROJECT>, <HOME>, <user>), and no config value, token or archive
content is printed. kakaotalk is off-limits here (llm.md rule 2): doctor leaves it out, the others refuse it.

Exit codes: 0 ok / ready | 3 needs attention | 2 refused or usage | 1 error."""
import argparse
import concurrent.futures
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
if str(PROJECT) not in sys.path:
    sys.path.insert(0, str(PROJECT))

OFF_LIMITS = ("kakaotalk",)
CORE_KEYS = ("v", "impl", "data_root", "inputs_root")
SHOWN = ("_source", "_token_type")      # options whose values are a fixed vocabulary, which `config get` prints
CHECKPOINT = "bootstrap.json"
PROBE_TIMEOUT = 300
# gmail's card has its own steps for the genesis (6.1.7) and turning sync on (6.1.8)
CARD_STEPS = {"gmail": {"5.5": "6.1.7", "5.6": "6.1.8"}}
# The step ids `checkpoint mark` takes, as llm.md 5.0 lists them: the steps of sections 5 and 8, gmail's numbered
# steps, the other cards (6.2-6.4) and their numbered steps (6.<card>.<step>), and genesis_complete (5.5)
GATES = ("5.1", "5.2", "5.3", "5.4", "5.5", "genesis_complete", "5.6", "5.7", "5.8", "5.9", "5.10",
         "6.1.1", "6.1.2", "6.1.3", "6.1.4", "6.1.5", "6.1.6", "6.1.7", "6.1.8", "6.1.9",
         "6.2", "6.2.1", "6.2.2", "6.2.3", "6.3", "6.3.1", "6.3.2", "6.3.3", "6.4", "6.4.1", "6.4.2", "6.4.3", "6.4.4",
         "8")
FIELDS = ("rc", "head_seq", "total", "errors", "limited", "messages_written", "posts_written", "channels", "pages",
          "excluded", "unavailable", "differing_rows")
# What each app needs before its first collection, by its source option (llm.md section 6): (key, kind, step, why).
# kind: file/dir (the config path must be there), value (the option set), checkpoint (that step recorded, or rows
# already captured)
NEEDS = {
    "gmail": {None: [("gm_client_file", "file", "6.1.2", "the Desktop client JSON is not in place yet: start at 6.1.2 "
                      "(ask whether the Google account already has the project before 6.1.3 creates one)"),
                     ("gm_token_file", "file", "6.1.5", "no token yet: the owner's consent (7.3)")]},
    "mattermost": {None: [("mm_url", "value", "6.4", "mm_url is empty: the owner gives the server's https:// address"),
                          ("mm_token_file", "file", "6.4", "no session token yet: the owner signs in (--login)")]},
    "discord": {"desktop": [("dc_desktop_path", "dir", "6.2", "the Discord desktop cache folder is not set or not "
                             "there (the desktop app installed and signed in)")],
                "api": [("dc_token_file", "file", "6.2", "no token file yet: the bot setup and the token file "
                         "handshake")]},
    "notion": {"desktop": [("nt_desktop_path", "file", "6.3", "Notion's desktop database is not set or not there "
                            "(the desktop app installed, signed in and synced)")],
               "api": [("5.4", "checkpoint", "6.3", "api source: [human] `ntn login`, then 5.4 (--auth-check); record "
                        "it with `checkpoint mark notion 5.4`")]},
}
KINDS = {key: kind for by_source in NEEDS.values() for needs in by_source.values() for key, kind, _s, _w in needs
         if kind in ("file", "dir")}
_WORD = re.compile(r"[a-z]{1,16}")
# sources whose sync never runs in the SYSTEM task (llm.md 6.3: ntn's keychain is the owner's; the task stays
# archive-only and the owner runs the collector)
NO_TASK_SYNC = {"notion": ("api",)}
_INT = re.compile(r"-?(0|[1-9][0-9]*)")
_KEY = re.compile(r"^[a-z][a-z0-9_]{0,39}(\.[A-Za-z_][A-Za-z0-9_]{0,39})?$")


class Refused(Exception):
    """A usage or precondition refusal: exit 2."""


def _out(text=""):
    print(mask(text))


def _err(text):
    print(mask(f"bootstrap: {text}"), file=sys.stderr)


# ---------------------------------------------------------------------------------------------------- masking

_MASKS = None


def _roots():
    """[(path, label)] to mask, from what can be known: the core's settings when they load, else the process's own
    profile (a broken settings.py or core must not leave a path unmasked)."""
    homes = {Path.home()}
    for var in ("USERPROFILE", "HOME"):
        if os.environ.get(var):
            homes.add(Path(os.environ[var]))
    out = [(PROJECT, "<PROJECT>")]
    try:
        from rawlayer import conf  # noqa: PLC0415
        homes.add(Path(getattr(conf.settings(), "HOME_DIR", None) or conf.user_home()))
        from rawlayer.capture import paths  # noqa: PLC0415
        out.append((Path(paths.DATA_PARENT), "<DATA_PARENT>"))
    except BaseException:  # noqa: BLE001 -- whatever the core raises, mask with what is known
        pass
    return out + [(h, "<HOME>") for h in homes]


def _mask_table():
    global _MASKS
    if _MASKS is None:
        roots = _roots()
        pairs = []
        for p, label in roots:
            forms = {str(p), p.as_posix()}
            try:
                forms |= {str(p.resolve()), p.resolve().as_posix()}
            except OSError:
                pass
            for s in forms:
                if len(s) > 3:
                    pairs += [(s, label), (s.replace("\\", "\\\\"), label)]
        pairs.sort(key=lambda x: -len(x[0]))
        users = {p.name for p, label in roots if label == "<HOME>" and p.name}
        users |= {os.environ[v] for v in ("USERNAME", "USER") if os.environ.get(v)}
        _MASKS = (pairs, sorted(users, key=len, reverse=True))
    return _MASKS


def mask(text):
    """`text` with the data parent, the checkout, the profile folder and the user name masked. It never raises: when
    masking itself fails, the text is replaced as a whole."""
    if not isinstance(text, str):
        return text
    try:
        pairs, users = _mask_table()
        for s, label in pairs:
            text = re.sub(re.escape(s), lambda _m, label=label: label, text, flags=re.IGNORECASE)
        for user in users:
            text = re.sub(r"(?<=[\\/])" + re.escape(user) + r"(?![\w.-])", "<user>", text, flags=re.IGNORECASE)
        return text
    except BaseException:  # noqa: BLE001
        return "(output withheld: it could not be masked)"


def mask_obj(obj):
    if isinstance(obj, str):
        return mask(obj)
    if isinstance(obj, dict):
        return {mask_obj(k): mask_obj(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [mask_obj(v) for v in obj]
    return obj


# ---------------------------------------------------------------------------------------------------- helpers

def _run(argv, timeout=120, cwd=None):
    """(rc, stdout, stderr) of a child process; rc None when it could not start or timed out."""
    try:
        p = subprocess.run(argv, capture_output=True, timeout=timeout, cwd=cwd or PROJECT, stdin=subprocess.DEVNULL,
                           env=dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1"))
    except (OSError, subprocess.SubprocessError):
        return None, "", ""
    return p.returncode, p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace")


def _python(*args):
    return [sys.executable, "-s", "-B", *args]


def _manage(*args):
    return _python(str(PROJECT / "manage.py"), *args)


def is_placeholder(v):
    return isinstance(v, str) and "<" in v and ">" in v


def path_state(v, kind="any"):
    """unset | placeholder | invalid | relative | there | missing | wrong_kind -- of a path value (stat only)."""
    if v is None or v == "":
        return "unset"
    if not isinstance(v, str):
        return "invalid"
    if is_placeholder(v):
        return "placeholder"
    if not os.path.isabs(v):
        return "relative"
    if not os.path.exists(v):
        return "missing"
    if (kind == "file" and not os.path.isfile(v)) or (kind == "dir" and not os.path.isdir(v)):
        return "wrong_kind"
    return "there"


def in_scope_apps():
    from rawlayer import registry  # noqa: PLC0415
    found = registry.discover()
    return [a for a in found if a not in OFF_LIMITS], [a for a in found if a in OFF_LIMITS]


def _check_app(app):
    apps, off = in_scope_apps()
    if app in OFF_LIMITS or app in off:
        raise Refused(f"{app} is off-limits to this tool (llm.md rule 2)")
    if app not in apps:
        raise Refused(f"no app {app!r} in this project (apps: {', '.join(apps)})")


def _now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------------------------------- machine

def _machine_path_has(name):
    """True when a folder of the machine PATH (what SYSTEM sees) holds `name`; None off Windows or unreadable."""
    if os.name != "nt":
        return None
    try:
        import winreg  # noqa: PLC0415
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment") as k:
            value = winreg.QueryValueEx(k, "Path")[0]
    except OSError:
        return None
    for d in os.path.expandvars(value).split(";"):
        if d.strip() and Path(d.strip(), name).is_file():
            return True
    return False


def _version(text):
    m = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", text or "")
    return tuple(int(x) for x in m.groups() if x is not None) if m else None


def machine():
    """The machine facts llm.md Phase 0 gates on, and the problems among them: [(step, text)]."""
    from rawlayer import conf, coreset, schedule  # noqa: PLC0415
    from rawlayer.capture import paths, precheck  # noqa: PLC0415
    problems = []
    m = {}
    exe = sys.executable
    py = {"version": ".".join(map(str, sys.version_info[:3])), "executable": exe,
          "flags_ok": bool(sys.flags.no_user_site and sys.flags.dont_write_bytecode)}
    py["problem"] = schedule.python_problem(exe) if os.name == "nt" else None
    if sys.version_info < (3, 12):
        problems.append(("0.2", "Python 3.12 or newer is needed"))
    if py["problem"]:
        problems.append(("0.2", py["problem"]))
    m["python"] = py
    git = shutil.which("git")
    rc, out, _ = _run([git, "--version"], timeout=30) if git else (None, "", "")
    m["git"] = {"found": rc == 0, "version": ".".join(map(str, _version(out) or ())) or None,
                "machine_path": _machine_path_has("git.exe")}
    if rc != 0:
        problems.append(("0.1", "git does not run: Git for Windows is needed"))
    elif m["git"]["machine_path"] is False:
        problems.append(("0.1", "git is not on the machine PATH, which the SYSTEM task uses"))
    pwsh = shutil.which("pwsh")
    rc, out, _ = _run([pwsh, "-NoProfile", "-NonInteractive", "-Command", "$PSVersionTable.PSVersion.ToString()"],
                      timeout=60) if pwsh else (None, "", "")
    v = _version(out) if rc == 0 else None
    # what is installed, not the agent's own shell: 0.1's two shell gates stay the agent's to run in it
    m["pwsh"] = {"found": v is not None, "version": ".".join(map(str, v)) if v else None,
                 "ok": v is not None and v[:2] >= (7, 3)}
    if not m["pwsh"]["ok"]:
        problems.append(("0.1", "PowerShell 7.3 or newer (pwsh) is not installed; the runbook's commands need it"))
    co = {}
    try:
        home = conf.user_home(PROJECT)
        co["under_profile"] = PROJECT.resolve().is_relative_to(Path(home).resolve()) and \
            (os.name != "nt" or conf.profile_sid(home) is not None)
    except Exception:  # noqa: BLE001
        co["under_profile"] = False
    rc, out, _ = _run([git, "-C", str(PROJECT), "rev-parse", "--is-inside-work-tree"], timeout=30) if git \
        else (None, "", "")
    co["work_tree"] = rc == 0 and out.strip() == "true"
    if co["work_tree"]:
        # no optional locks: status must not rewrite .git/index (doctor changes nothing); kakaotalk/ left out
        rc, out, _ = _run([git, "--no-optional-locks", "-C", str(PROJECT), "status", "--porcelain", "--", ".",
                           ":(exclude)kakaotalk"], timeout=60)
        co["changed_files"] = len([ln for ln in out.splitlines() if ln.strip()]) if rc == 0 else None
    bad = coreset.check(PROJECT)
    co["core"] = "ok" if not bad else {k: sum(1 for kind, _r in bad if kind == k) for k in ("missing", "extra",
                                                                                         "changed")}
    if not co["under_profile"]:
        problems.append(("0.1", "the checkout is not under the owner's profile: every SYSTEM run would fail"))
    if not co["work_tree"]:
        problems.append(("4", "the checkout is not a git work tree (getting the code: clone it with git)"))
    if bad:
        problems.append(("2", "core files differ from CORE_SHA256SUMS (rule 7: report it)"))
    m["checkout"] = co
    rc, out, err = _run(_manage("check"), timeout=300)
    lines = [ln for ln in (out + err).splitlines() if ln.startswith(("apps:", "core:", "check:"))]
    m["check"] = {"rc": rc, "lines": lines}
    if rc != 0:
        problems.append(("0.3", "manage.py check did not pass"))
    dp = Path(paths.DATA_PARENT)
    d = {"exists": dp.is_dir()}
    if d["exists"]:
        from rawlayer.management.commands.setup import acl_problem, search_problem  # noqa: PLC0415
        try:
            d["acl"] = acl_problem(dp) or "ok"
        except Exception as e:  # noqa: BLE001
            d["acl"] = f"not checked (error {type(e).__name__})"
        try:
            d["search"] = search_problem(dp) or "ok"
        except Exception as e:  # noqa: BLE001
            d["search"] = f"not checked (error {type(e).__name__})"
        d["in_work_tree"] = precheck.in_work_tree(dp)
        if d["acl"] != "ok":
            problems.append(("9", f"data parent ACL: {d['acl']}"))
        if d["search"] != "ok":
            problems.append(("0.5", f"data parent: {d['search']}"))
        if d["in_work_tree"]:
            problems.append(("9", "the data parent is inside a git work tree"))
    m["data_parent"] = d
    m["elevated"] = schedule.elevated()
    return m, problems


# ---------------------------------------------------------------------------------------------------- one app

def read_checkpoints(data_root):
    try:
        d = json.loads((Path(data_root) / "reports" / CHECKPOINT).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    gates = d.get("gates") if isinstance(d, dict) else None
    return gates if isinstance(gates, dict) else {}


def definition_state(schedule, s, app, back):
    """{enabled, definition[, differs]} of a registered task's definition (XML `back`), compared as
    `schedule status` compares it: "current", "differs" (the field names that do), or "foreign" (its action is not
    this checkout's `manage.py collect <app> --config <config>`)."""
    f = schedule.fields(back)
    out = {"enabled": f["enabled"] == "true"}
    ns = schedule.NS
    exe = schedule._root(back).find(f"{{{ns}}}Actions/{{{ns}}}Exec")
    python = (exe.findtext(f"{{{ns}}}Command") if exe is not None else None) or ""
    args = (exe.findtext(f"{{{ns}}}Arguments") if exe is not None else None) or ""
    project = schedule._project()
    if schedule.registered_config(project, app, args) is None:
        out["definition"] = "foreign"
        return out
    diff = schedule.differences(back, schedule.task_xml(s, python, project, app, schedule._argv(args)[6]))
    out["definition"] = "differs" if diff else "current"
    if diff:
        out["differs"] = diff
    return out


def probe(app):
    """What doctor reports of one app, from a process that selects only it (one process, one app)."""
    from rawlayer import registry, schedule  # noqa: PLC0415
    from rawlayer.capture import paths  # noqa: PLC0415
    from rawlayer.capture.config import Config, ConfigError  # noqa: PLC0415
    from rawlayer.management.base import default_config  # noqa: PLC0415
    from rawlayer.management.commands.setup import placeholders  # noqa: PLC0415
    reg = registry.select(app)
    a = reg.apps[0]
    out = {"app": app}
    task = {}
    try:
        s = schedule.task_settings(app)
    except Exception as e:  # noqa: BLE001
        s, task["error"] = None, type(e).__name__
    if s is not None:
        task["name"] = s.task
        if os.name == "nt":
            back, _e = schedule._definition(s.task)
            if back is not None:
                task["registered"] = True
                try:
                    task.update(definition_state(schedule, s, app, back))
                except Exception as e:  # noqa: BLE001
                    task["definition"] = f"unreadable ({type(e).__name__})"
                info = schedule._run_info(s.task)
                if info:
                    shown, ok = schedule.result_text(info["last_result"])
                    try:
                        code = int(info["last_result"]) & 0xFFFFFFFF
                    except (TypeError, ValueError):
                        code = None
                    task.update(state=info["state"], last_run=info["last_run"], last_result=shown,
                                result_ok=ok, next_run=info["next_run"], never_ran=code == 0x41303,
                                running=code == 0x41301)
            else:
                task["registered"] = False if schedule.elevated() else "unknown"
    out["task"] = task
    path = default_config(app)
    c = out["config"] = {"exists": path.is_file()}
    if not c["exists"]:
        return out
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError
    except (OSError, ValueError):
        c["readable"] = False
        return out
    c["readable"] = True
    c["placeholders"] = placeholders(raw)
    defaults = {op.key: op.default for op in a.options}
    sync_key = next((k for k in defaults if k.endswith("_sync")), None)
    source_key = next((k for k in defaults if k.endswith("_source")), None)
    c["sync_key"] = sync_key
    c["sync"] = raw.get(sync_key, defaults.get(sync_key)) is True if sync_key else None
    source = raw.get(source_key, defaults.get(source_key)) if source_key else None
    # only a value of the option's fixed vocabulary is shown (a host or path typed into the key is not)
    c["source"] = source if source is None or (isinstance(source, str) and _WORD.fullmatch(source)) else "invalid"
    files = {}
    sources = raw.get("sources") if isinstance(raw.get("sources"), dict) else {}
    for k in a.source_keys:
        files[f"sources.{k}"] = path_state(sources.get(k), "dir")
    for k, default in defaults.items():
        if k.endswith(("_file", "_path")):
            files[k] = path_state(raw.get(k, default), KINDS.get(k, "any"))
    c["files"] = files
    c["values_set"] = sorted(k for k, default in defaults.items()
                             if isinstance(raw.get(k, default), str) and raw.get(k, default) != "")
    data_root = raw.get("data_root")
    try:
        cfg = Config(path)
        c["valid"] = True
        c["task_problems"] = schedule.task_problems(cfg, reg.apps)
        data_root = str(cfg.data_root)
    except ConfigError as e:
        c.update(valid=False, refused=str(e))
    except Exception as e:  # noqa: BLE001
        c.update(valid=False, refused=f"error {type(e).__name__}")
    if isinstance(data_root, str) and os.path.isdir(data_root) and paths.inside(data_root, paths.DATA_PARENT):
        info = schedule.last_run(data_root)
        if info is None:
            out["log"] = None
        elif "error" in info:
            out["log"] = {"error": info["error"]}
        else:
            out["log"] = {"steps": [{"at": ln.split()[0], "step": step, "rc": rc} for ln, step, rc in info["steps"]],
                          "complete": info["complete"], "newer_started": info["newer_started"]}
        out["checkpoints"] = read_checkpoints(data_root)
    return out


def index_state(app):
    """Total and freshness of the app's index, through the search command (it takes no lock)."""
    rc, out, err = _run(_manage("search", app, "--since", "1970-01-01", "--mode", "timeline", "--limit", "1",
                                "--json"), timeout=300)
    if rc == 0:
        try:
            env = json.loads(out)
            ix = env.get("index") or {}
            return {"state": "stale" if ix.get("stale") else "ok", "total": env.get("total"),
                    "seq": ix.get("seq"), "ledger_seq": ix.get("ledger_seq"), "updated_at": env.get("updated_at")}
        except (ValueError, AttributeError):
            return {"state": "error", "why": "unreadable search output"}
    text = err.lower()
    if rc == 75:
        return {"state": "busy"}
    for needle, state in (("no " + app + " index yet", "missing"), ("built by another version", "outdated"),
                          ("cannot be read", "unreadable"), ("config file unreadable", "not_configured"),
                          ("data_root must be", "not_configured")):
        if needle in text:
            return {"state": state}
    return {"state": "error", "rc": rc, "why": (err.strip().splitlines() or ["no message"])[-1][:200]}


def _needs(app, c):
    by_source = NEEDS.get(app, {})
    return by_source.get(c.get("source"), by_source.get(None, []))


def next_step(app, p, idx):
    """(llm.md step, why) for one app's state: the first gate it has not passed, by the app card's own step where
    the card runs a section 5 step as one of its numbered steps (CARD_STEPS)."""
    step, why = _next_step(app, p, idx)
    return CARD_STEPS.get(app, {}).get(step, step), why


def _next_step(app, p, idx):
    c = p.get("config") or {}
    if p.get("timeout"):
        return "9", (f"the probe did not finish in {PROBE_TIMEOUT} s (a schtasks or ACL query may hang): run "
                     f"doctor --app {app} again, then report it (rule 7)")
    if p.get("error"):
        return "9", f"the app does not load ({p['error']}): report it (rule 7)"
    if not c.get("exists"):
        return "5.1", f"no config yet: python -s -B manage.py setup {app}"
    if not c.get("readable"):
        return "9", "config.json is not a readable JSON object"
    if c.get("placeholders"):
        return "5.3", "fill in config.json: " + ", ".join(c["placeholders"])
    if not c.get("valid"):
        return "5.3", f"config refused ({c.get('refused')})"
    files = c.get("files") or {}
    cps = p.get("checkpoints") or {}
    idx = idx or {}
    for key, kind, step, why in _needs(app, c):
        if kind == "value":
            missing = key not in c.get("values_set", ())
        elif kind == "checkpoint":
            missing = key not in cps and not idx.get("total")
        else:
            missing = files.get(key) != "there"
        if missing:
            return step, why
    state = idx.get("state")
    if state in ("missing", "not_configured") or (state in ("ok", "stale") and not idx.get("total")):
        return "5.5", "nothing captured yet: the bounded first collection"
    if state == "outdated":
        return "9", f"the index was built by another version: collect {app} --rebuild-index"
    if state == "unreadable":
        return "9", (f"the index cannot be read: if a run is in progress (the section 3 reader), wait and run doctor "
                     f"again; otherwise collect {app} (it rolls back an index left mid-update), and --rebuild-index "
                     "only if that does not help")
    if state == "error":
        return "9", f"search failed: {idx.get('why')}"
    if not c.get("sync"):
        # 5.5 finishes genesis before 5.8, so a recorded 5.10 implies it (an app live before this tool)
        if "genesis_complete" not in cps and "5.10" not in cps:
            return "5.5", ('finish genesis with larger bounds until "limited": false, then '
                           f"`bootstrap.py checkpoint mark {app} genesis_complete`")
        if c.get("source") not in NO_TASK_SYNC.get(app, ()):
            return "5.6", f"turn on {c.get('sync_key')}"
    if c.get("task_problems"):
        return "5.6", "schedule install would refuse: " + "; ".join(c["task_problems"])
    task = p.get("task") or {}
    registered = task.get("registered")
    log = p.get("log") or {}
    steps = log.get("steps") or []
    if registered is False:
        return "5.7", "no scheduled task yet: preview it (5.7), then install, check and start it (5.8)"
    # task.log cannot tell the task's runs from manual ones (collect and --capture write the same blocks): without
    # elevation only a recorded 5.10 says the task was installed and ran
    if registered == "unknown" and "5.10" not in cps:
        return "5.7", ("the task cannot be read without elevation and no 5.10 checkpoint is recorded: check it "
                       "elevated (8 step 2, 7.2), then record 5.10; if it is missing, install it (5.7, 5.8)")
    if registered is True:
        if task.get("enabled") is False:
            return "5.7", ("the task is disabled (paused by hand in Task Scheduler): ask the owner; to resume it, "
                           "install it again (5.7, 5.8), which registers it enabled")
        if task.get("definition") != "current":
            differs = f": {', '.join(task['differs'])}" if task.get("differs") else ""
            return "5.7", (f"the registered task is not this app's current definition ({task.get('definition')}"
                           f"{differs}): install it again (5.7, 5.8)")
        if "result_ok" not in task:
            return "9", "the task is registered but its run info cannot be read: an elevated status (8 step 2)"
        if task.get("never_ran"):
            return "5.8", "the task is installed but has not run yet: start it (5.8 `run`), then 5.9 and 5.10"
        if task.get("running"):
            return "5.9", "a run is in progress: wait for its step=index line (5.9), then 5.10"
        if task.get("result_ok") is False:
            return "9", f"the task's last result is not ok: {task.get('last_result')}"
    if log.get("error"):
        return "9", f"task.log cannot be read ({log['error']})"
    if steps and (not log.get("complete") or log.get("newer_started")):
        return "9", ("the newest run has no step=index line: it is still running, or it was stopped (the time limit, "
                     "or by hand); read it with the section 3 reader (section 9: 'stopped (time limit, or by hand)')")
    bad = [s for s in steps if s["rc"] != 0]
    if bad:
        return "9", "the newest run's " + ", ".join(f"{s['step']} rc={s['rc']}" for s in bad)
    if registered == "unknown":
        return "8", "live by its 5.10 checkpoint; the task was not read (not elevated): 8 step 2 checks it"
    if state == "stale":
        return "8", "live; the index is behind the ledger (the next run's index step catches up)"
    return "8", "live: the health routine (section 8) when asked"


def _probe_child(app):
    rc, out, err = _run(_python(str(Path(__file__).resolve()), "_probe", app), timeout=PROBE_TIMEOUT)
    if rc is None:
        return {"app": app, "error": "the probe did not finish", "timeout": True}
    if rc != 0:
        return {"app": app, "error": (err.strip().splitlines() or [f"probe rc={rc}"])[-1][:200]}
    try:
        return json.loads(out)
    except ValueError:
        return {"app": app, "error": "unreadable probe output"}


def _verify(app):
    rc, out, err = _run(_manage("check", app), timeout=600)
    text = out + err
    m = re.search(r"differing_rows=(\d+) head_seq=(\d+)", text)
    return {"rc": rc, "busy": "ledger busy" in text, "differing_rows": int(m.group(1)) if m else None,
            "head_seq": int(m.group(2)) if m else None}


def doctor(a):
    apps, off = in_scope_apps()
    named = a.app or []
    for app in named:
        _check_app(app)
    todo = named or apps
    report = {"v": 1}
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        fm = ex.submit(machine)
        probes = {app: ex.submit(_probe_child, app) for app in todo}
        idxs = {app: ex.submit(index_state, app) for app in todo}
        m, problems = fm.result()
        report["machine"] = m
        results = {}
        for app in todo:
            p = probes[app].result()
            idx = idxs[app].result()
            step, why = next_step(app, p, idx)
            entry = dict(p, index=idx, next={"step": step, "why": why})
            entry.pop("app", None)
            results[app] = entry
    if a.verify:
        for app in todo:
            if (results[app].get("config") or {}).get("valid"):
                results[app]["verify"] = _verify(app)
    report["apps"] = results
    report["skipped"] = [f"{x} (off-limits)" for x in off]
    attention = [f"machine {step}: {text}" for step, text in problems]
    for app, r in results.items():
        # an app that could not be probed counts whether or not it has a config
        configured = r.get("error") or (r.get("config") or {}).get("exists")
        if r["next"]["step"] != "8" and (configured or app in named):
            attention.append(f"{app} {r['next']['step']}: {r['next']['why']}")
        v = r.get("verify")
        if v and (v["rc"] != 0 or v["busy"] or v["differing_rows"]):
            attention.append(f"{app}: check {'busy (not verified)' if v['busy'] else 'did not pass'}")
    report["attention"] = attention
    rc = 3 if attention else 0
    report["rc"] = rc
    if a.json:
        _out(json.dumps(mask_obj(report), ensure_ascii=False, indent=1))
    else:
        _print_doctor(report)
    return rc


def _print_doctor(r):
    m = r["machine"]
    py, git, pw, co, dp = m["python"], m["git"], m["pwsh"], m["checkout"], m["data_parent"]
    _out("doctor: machine")
    _out(f"  python {py['version']}{'' if not py['problem'] else ' -- ' + py['problem']}; "
         f"<PY> candidate: {py['executable']}{'' if py['flags_ok'] else ' (run this tool with -s -B)'}")
    _out(f"  git: {'ok ' + (git['version'] or '') if git['found'] else 'NOT FOUND'}; machine PATH: "
         f"{ {True: 'yes', False: 'NO', None: 'unknown'}[git['machine_path']] }")
    _out(f"  pwsh: {pw['version'] or 'NOT FOUND'}{'' if pw['ok'] else ' (7.3 or newer needed)'}")
    core = co["core"] if co["core"] == "ok" else "DIFFERS " + json.dumps(co["core"])
    _out(f"  checkout: {'under the profile' if co['under_profile'] else 'NOT under the profile'}, "
         f"{'git work tree' if co['work_tree'] else 'NOT a git work tree'}"
         f"{'' if co.get('changed_files') is None else ', ' + str(co['changed_files']) + ' changed file(s)'}"
         f", core {core}")
    _out(f"  manage.py check: rc={m['check']['rc']}  " + " | ".join(m["check"]["lines"]))
    if dp["exists"]:
        _out(f"  data parent: there; ACL {dp['acl']}; Windows Search {dp['search']}"
             f"{'; INSIDE a git work tree' if dp['in_work_tree'] else ''}")
    else:
        _out("  data parent: not created yet (manage.py setup <app> creates it)")
    _out(f"  elevated: {'yes' if m['elevated'] else 'no'}")
    for app, e in r["apps"].items():
        _out(f"doctor: {app}  next {e['next']['step']}: {e['next']['why']}")
        if e.get("error"):
            continue
        c = e.get("config") or {}
        if not c.get("exists"):
            continue
        if c.get("readable"):
            sync = "" if c.get("sync_key") is None else f"; {c['sync_key']} {'on' if c.get('sync') else 'off'}"
            src = f"; source {c['source']}" if c.get("source") else ""
            state = "ok" if c.get("valid") else f"refused ({c.get('refused')})"
            _out(f"  config: {state}{src}{sync}")
            missing = [k for k, v in (c.get("files") or {}).items() if v not in ("there", "unset")]
            if missing:
                _out("  paths not there: " + ", ".join(f"{k} ({(c['files'][k])})" for k in missing))
            for p in c.get("task_problems") or []:
                _out(f"  schedule install would refuse: {p}")
        ix = e.get("index") or {}
        extra = "" if ix.get("total") is None else f" total={ix['total']} seq={ix.get('seq')}/{ix.get('ledger_seq')}"
        _out(f"  index: {ix.get('state')}{extra}")
        t = e.get("task") or {}
        if t:
            reg = {True: "registered", False: "NOT registered", "unknown": "registration unknown (not elevated)"}.get(
                t.get("registered"), "unknown")
            if t.get("registered") is True:
                reg += f" ({'enabled' if t.get('enabled') else 'DISABLED'}; definition {t.get('definition')})"
            res = f"; last result {t['last_result']}; next run {t.get('next_run')}" if t.get("last_result") else ""
            _out(f"  task {t.get('name')}: {reg}{res}")
        log = e.get("log")
        if log and log.get("steps"):
            steps = ", ".join(f"{s['step']} rc={s['rc']}" for s in log["steps"])
            _out(f"  task.log newest run with steps ({log['steps'][-1]['at']}): {steps}"
                 f"{'' if log['complete'] else ' (no step=index: running or stopped)'}"
                 f"{'; a newer run has started and finished no step yet' if log.get('newer_started') else ''}")
        if e.get("checkpoints"):
            _out("  checkpoints: " + ", ".join(sorted(e["checkpoints"])))
        v = e.get("verify")
        if v:
            _out(f"  check: rc={v['rc']} differing_rows={v['differing_rows']} head_seq={v['head_seq']}"
                 f"{' (ledger busy: not verified)' if v['busy'] else ''}")
    for s in r["skipped"]:
        _out(f"doctor: {s}: not checked")
    if r["attention"]:
        _out(f"doctor: {len(r['attention'])} item(s) need attention")
        for t in r["attention"]:
            _out(f"  - {t}")
    else:
        _out("doctor: ok")


# ---------------------------------------------------------------------------------------------------- config

def parse_value(raw, string=False):
    """llm.md 5.3's rule: true/false/null, a plain integer, or a value starting with [ or { is JSON."""
    if string:
        return raw
    if raw in ("true", "false", "null") or raw[:1] in ("[", "{") or _INT.fullmatch(raw):
        try:
            return json.loads(raw)
        except ValueError:
            raise Refused("a value starting with [ or { must be valid JSON (--string keeps it as text)") from None
    return raw


def _app_config(app):
    """(the selected app's AppConfig, config path, config dict)."""
    from rawlayer import registry  # noqa: PLC0415
    from rawlayer.management.base import default_config  # noqa: PLC0415
    _check_app(app)
    a = registry.select(app).apps[0]
    path = default_config(app)
    if not path.is_file():
        raise Refused(f"{app} has no config.json yet: python -s -B manage.py setup {app}")
    try:
        cfg = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise Refused("config.json is not readable JSON: set the keys again with `config set` after fixing "
                      "it, or ask the owner") from None
    if not isinstance(cfg, dict):
        raise Refused("config.json is not a JSON object")
    return a, path, cfg


def write_config(path, cfg, accept=None):
    """setup's format (json.dumps(indent=2) + newline through write_text, .part then replace); the file it
    replaces is kept as config.json.prev. `accept(part)` sees the new file before it replaces the old one and
    raises Refused to keep the old one. False when nothing changed."""
    text = json.dumps(cfg, ensure_ascii=False, indent=2) + "\n"
    old = path.read_bytes() if path.is_file() else None
    # compared as JSON text, not with ==, which takes true for 1 and 1 for 1.0 (a refused type could not be fixed)
    if old is not None and json.dumps(json.loads(old.decode("utf-8")), sort_keys=True) == json.dumps(cfg,
                                                                                                     sort_keys=True):
        return False
    tmp = path.with_name("config.json.part")
    tmp.write_text(text, encoding="utf-8")
    try:
        if accept is not None:
            accept(tmp)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    if old is not None:
        path.with_name("config.json.prev").write_bytes(old)
    tmp.replace(path)
    return True


def _keeps_accepted(path):
    """An `accept` for write_config: a config Config accepts is never replaced by one it refuses."""
    from rawlayer.capture.config import Config, ConfigError  # noqa: PLC0415
    try:
        Config(path)
    except ConfigError:
        return None                         # not accepted yet (being filled in): any step forward is written

    def accept(part):
        try:
            Config(part)
        except ConfigError as e:
            raise Refused(f"the change would make the config refused ({e}); config.json is unchanged") from None
    return accept


def _validate(a, path, cfg):
    """(rc, lines): placeholders left, Config refused or not, and what schedule install would refuse."""
    from rawlayer import schedule  # noqa: PLC0415
    from rawlayer.capture.config import Config, ConfigError  # noqa: PLC0415
    from rawlayer.management.commands.setup import placeholders  # noqa: PLC0415
    lines, rc = [], 0
    left = placeholders(cfg)
    if left:
        rc = 3
        lines.append("fill in: " + ", ".join(left))
    try:
        c = Config(path)
    except ConfigError as e:
        return 3, lines + [f"config refused ({e})"]
    lines.append("config ok")
    for p in schedule.task_problems(c, [a]):
        lines.append(f"schedule install would refuse: {p}")
    return rc, lines


def _allowed(a, key, force):
    if not _KEY.fullmatch(key):
        raise Refused(f"{key!r} is not a config key")
    if key.startswith("sources."):
        if key[len("sources."):] not in a.source_keys:
            raise Refused(f"{key}: {a.label} reads sources." + ", sources.".join(a.source_keys))
        return None
    if key in CORE_KEYS:
        if not force:
            raise Refused(f"{key} is set by setup; changing it needs --force (llm.md rule 10)")
        return None
    for op in a.options:
        if op.key == key:
            return op
    raise Refused(f"{key} is not one of {a.label}'s options ({', '.join(op.key for op in a.options)})")


def _set(cfg, key, value):
    if key.startswith("sources."):
        if not isinstance(cfg.get("sources"), dict):
            cfg["sources"] = {}
        cfg["sources"][key[len("sources."):]] = value
    else:
        cfg[key] = value


def _kind(v):
    return {bool: "boolean", int: "integer", str: "string", list: "list", dict: "object", type(None): "null"}.get(
        type(v), type(v).__name__)


def config_set(a_):
    a, path, cfg = _app_config(a_.app)
    if a_.pairs == ["-"]:
        # bytes decoded as UTF-8: a pipe would otherwise be read in the ANSI code page and garble Korean values
        raw = sys.stdin.buffer.read() if hasattr(sys.stdin, "buffer") else sys.stdin.read().encode("utf-8")
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise Refused("stdin is not UTF-8: pipe it from pwsh 7, or write non-ASCII characters as \\uXXXX "
                          "escapes") from None
        try:
            given = json.loads(text)
        except ValueError:
            raise Refused("stdin is not a JSON object") from None
        if not isinstance(given, dict) or not given:
            raise Refused("stdin is not a JSON object of {key: value}")
        pairs = list(given.items())
    else:
        if not a_.pairs or len(a_.pairs) % 2:
            raise Refused("give KEY VALUE pairs (or - and a JSON object on stdin)")
        pairs = [(a_.pairs[i], parse_value(a_.pairs[i + 1], a_.string)) for i in range(0, len(a_.pairs), 2)]
    for key, value in pairs:
        op = _allowed(a, key, a_.force)
        if is_placeholder(value) or (isinstance(value, list) and any(is_placeholder(x) for x in value)):
            raise Refused(f"{key}: the value still holds a <...> placeholder: substitute it")
        if key.startswith("sources.") and not (isinstance(value, str) and os.path.isabs(value)
                                               and os.path.isdir(value)):
            raise Refused(f"{key}: a source is the absolute path of an existing folder")
        if op is not None:
            try:
                op.check(value)
            except (ValueError, TypeError):
                raise Refused(f"{key}: {a.label} refuses this value ({_kind(value)})") from None
    for key, value in pairs:
        _set(cfg, key, value)
    changed = write_config(path, cfg, _keeps_accepted(path))
    for key, value in pairs:
        _out(f"config: {a.label} {key} {'set' if changed else 'unchanged'} ({_kind(value)})")
    rc, lines = _validate(a, path, cfg)
    for ln in lines:
        _out(f"config: {a.label}: {ln}")
    return rc


def config_defaults(a_):
    a, path, cfg = _app_config(a_.app)
    before = json.dumps(cfg, sort_keys=True)
    a.setup_defaults(cfg, lambda msg: _out(f"config: {a.label} {msg}"))
    changed = json.dumps(cfg, sort_keys=True) != before and write_config(path, cfg, _keeps_accepted(path))
    if not changed:
        _out(f"config: {a.label}: nothing to fill")
    rc, lines = _validate(a, path, cfg)
    for ln in lines:
        _out(f"config: {a.label}: {ln}")
    return rc


MISSING = object()          # a key the config does not hold (a key holding null is not missing)


def describe_value(v, data_root, key="", option=False):
    """What `config get` prints for a value: its state, never the value itself (a path, an organisation's value or
    a credential), except booleans, integers and the fixed vocabulary of the SHOWN options. `option`: an app option,
    whose default applies only when the key is missing."""
    if v is MISSING:
        return "missing (the app's default applies)" if option else "missing"
    if v is None:
        return "null (checked as given, not as the default: remove the key or set a value)" if option else "null"
    if isinstance(v, bool) or isinstance(v, int):
        return json.dumps(v)
    if key.endswith(SHOWN) and isinstance(v, str) and re.fullmatch(r"[a-z]{1,16}", v):
        return v
    if isinstance(v, list):
        return f"list of {len(v)}" + (" (placeholder)" if any(is_placeholder(x) for x in v) else "")
    if isinstance(v, dict):
        return f"object of {len(v)} key(s)"
    if not isinstance(v, str):
        return _kind(v)
    if v == "":
        return "empty"
    if is_placeholder(v):
        return "placeholder"
    if os.path.isabs(v):
        if key == "data_root":
            where = "the data root"
        elif isinstance(data_root, str) and _inside(v, data_root):
            where = "path in the data root"
        else:
            where = "path outside the data root"
        return f"{where}, {'there' if os.path.exists(v) else 'missing'}"
    return "set"


def _inside(path, parent):
    p = os.path.normcase(os.path.abspath(path)).rstrip("\\/")
    q = os.path.normcase(os.path.abspath(parent)).rstrip("\\/")
    return p == q or p.startswith(q + os.sep)


def config_get(a_):
    a, path, cfg = _app_config(a_.app)
    data_root = cfg.get("data_root")
    rows = {k: describe_value(cfg.get(k, MISSING), data_root, k) for k in CORE_KEYS}
    sources = cfg.get("sources") if isinstance(cfg.get("sources"), dict) else {}
    rows.update({f"sources.{k}": describe_value(sources.get(k, MISSING), data_root) for k in a.source_keys})
    rows.update({op.key: describe_value(cfg.get(op.key, MISSING), data_root, op.key, option=True)
                 for op in a.options})
    rc, lines = _validate(a, path, cfg)
    if a_.json:
        _out(json.dumps({"app": a.label, "keys": rows, "status": lines, "rc": rc}, ensure_ascii=False, indent=1))
    else:
        _out(f"config: {a.label}")
        width = max(len(k) for k in rows)
        for k, v in rows.items():
            _out(f"  {k:<{width}}  {v}")
        for ln in lines:
            _out(f"config: {a.label}: {ln}")
    return rc


# ---------------------------------------------------------------------------------------------------- checkpoint

def _data_root(app):
    from rawlayer.capture import paths  # noqa: PLC0415
    from rawlayer.management.base import default_config  # noqa: PLC0415
    _check_app(app)
    try:
        d = json.loads(default_config(app).read_text(encoding="utf-8")).get("data_root")
    except (OSError, ValueError, AttributeError):
        raise Refused(f"{app} has no readable config.json yet: python -s -B manage.py setup {app}") from None
    if not isinstance(d, str) or not os.path.isabs(d) or not os.path.isdir(d) or \
            not paths.inside(d, paths.DATA_PARENT):
        raise Refused(f"{app}'s data_root is not an existing folder inside the data parent")
    return Path(d)


def _field(text):
    name, sep, raw = text.partition("=")
    if not sep or name not in FIELDS:
        raise Refused(f"--field NAME=VALUE with NAME one of: {', '.join(FIELDS)}")
    if raw in ("true", "false"):
        return name, raw == "true"
    if _INT.fullmatch(raw):
        return name, int(raw)
    raise Refused(f"--field {name}: an integer, true or false")


def checkpoint(a_):
    root = _data_root(a_.app)
    f = root / "reports" / CHECKPOINT
    gates = read_checkpoints(root)
    if a_.action == "show":
        if not gates:
            _out(f"checkpoint: {a_.app}: none")
        for g in sorted(gates, key=lambda g: GATES.index(g) if g in GATES else len(GATES)):
            fields = " ".join(f"{k}={json.dumps(v)}" for k, v in sorted(gates[g].items()) if k != "at")
            _out(f"checkpoint: {a_.app} {g} at {gates[g].get('at')} {fields}".rstrip())
        return 0
    if a_.action == "mark":
        if a_.gate not in GATES:
            raise Refused(f"gate {a_.gate!r}: one of {', '.join(GATES)}")
        entry = {"at": _now()}
        entry.update(dict(_field(x) for x in a_.field or []))
        gates[a_.gate] = entry
    else:
        if a_.gate is None:
            gates = {}
        elif a_.gate in gates:
            del gates[a_.gate]
        else:
            raise Refused(f"no checkpoint {a_.gate!r} for {a_.app}")
    f.parent.mkdir(exist_ok=True)
    tmp = f.with_name(CHECKPOINT + ".part")
    tmp.write_text(json.dumps({"v": 1, "app": a_.app, "gates": gates}, indent=1) + "\n", encoding="utf-8")
    tmp.replace(f)
    _out(f"checkpoint: {a_.app} {'marked ' + a_.gate if a_.action == 'mark' else 'cleared ' + (a_.gate or 'all')}")
    return 0


# ---------------------------------------------------------------------------------------------------- main

def parser():
    ap = argparse.ArgumentParser(prog="tools/bootstrap.py", description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="command", required=True)
    p = sub.add_parser("doctor", help="the machine's and the apps' state, and each app's next llm.md step")
    p.add_argument("--app", action="append", help="check only this app (repeatable)")
    p.add_argument("--json", action="store_true")
    p.add_argument("--verify", action="store_true", help="also run manage.py check <app> (takes the ledger lock)")
    p = sub.add_parser("config", help="read or change an app's config.json (never printing its values)")
    cs = p.add_subparsers(dest="action", required=True)
    g = cs.add_parser("get")
    g.add_argument("app")
    g.add_argument("--json", action="store_true")
    s = cs.add_parser("set")
    s.add_argument("app")
    s.add_argument("pairs", nargs="*", metavar="KEY VALUE")
    s.add_argument("--string", action="store_true", help="every VALUE is a string")
    s.add_argument("--force", action="store_true", help="allow v, impl, data_root and inputs_root")
    d = cs.add_parser("defaults")
    d.add_argument("app")
    p = sub.add_parser("checkpoint", help="the llm.md gates an app has passed")
    p.add_argument("action", choices=("show", "mark", "clear"))
    p.add_argument("app")
    p.add_argument("gate", nargs="?")
    p.add_argument("--field", action="append", metavar="NAME=VALUE")
    p = sub.add_parser("_probe")
    p.add_argument("app")
    sub.add_parser("agents", help="the agent integration (tools/agentkit.py)", add_help=False)
    return ap


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass
    if argv[:1] == ["agents"]:
        from tools import agentkit  # noqa: PLC0415
        return agentkit.main(argv[1:])
    a = parser().parse_args(argv)
    try:
        if a.command == "doctor":
            return doctor(a)
        if a.command == "_probe":
            _check_app(a.app)
            print(json.dumps(probe(a.app), ensure_ascii=False))
            return 0
        if a.command == "config":
            return {"get": config_get, "set": config_set, "defaults": config_defaults}[a.action](a)
        if a.command == "checkpoint":
            if a.action == "mark" and not a.gate:
                raise Refused("mark needs a GATE")
            return checkpoint(a)
    except Refused as e:
        _err(f"refused: {e}")
        return 2
    except KeyboardInterrupt:
        _err("interrupted")
        return 1
    except Exception as e:  # noqa: BLE001 -- never a traceback: it would print unmasked paths
        _err(f"error {type(e).__name__}: {e}")
        return 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
