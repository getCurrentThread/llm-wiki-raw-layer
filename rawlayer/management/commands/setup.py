# -*- coding: utf-8 -*-
"""manage.py setup <app> [--from CFG|-] [--force] [--restart-client] -- a machine ready for an app's capture (SPEC 10.1).

  1. the data parent (settings.DATA_PARENT): created when missing, with a protected ACL that gives full control,
     inherited (OI)(CI), to SYSTEM, Administrators and the user whose home holds the project (Windows; elsewhere
     mode 0700); an existing one is only checked
  2. Windows Search: the data parent must be an excluded location while the indexer runs (checked, never changed:
     Indexing Options > Modify > untick it)
  3. the app's data root, <DATA_PARENT>/<app> (inherits the parent's ACL)
  4. its config.json, unless one is there (--force replaces it): the app's config.example.json, or the config read
     from CFG (- reads it from stdin), with v, impl, data_root and inputs_root set for this machine; nothing of it
     is printed. The app's own defaults (AppConfig.setup_defaults) fill the placeholders it can resolve itself
     before the write, and after it the app's provisioning (AppConfig.setup_provision; --restart-client lets it
     restart the app's client to rescan it, --scan adds its memory-scan fallback, --root installs a key file)
     derives what only the app can. Placeholders still left in it (<...>) are named by key, and the file is
     loaded as capture would load it.

  --provision skips 1-4 and reruns only that provisioning against the config.json that is already there: the
  recovery path when a credential has rotated (nothing is written but what the app derives).

Exit codes: 0 ready | 3 needs attention (a placeholder, the config refused, the ACL or Search exclusion not as
capture wants it, the app's provisioning) | 2 refused (usage, a config already there, no config to provision) |
1 error."""
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from ... import conf, registry
from ..base import BaseCommand

SYSTEM, ADMINISTRATORS = "S-1-5-18", "S-1-5-32-544"


def _say(msg):
    print(f"setup: {msg}")


def protect(folder):
    """Windows: the ACL capture's precheck wants on the data parent (protected; SYSTEM, Administrators and the
    home's user with full control, inherited by every file and folder below); elsewhere mode 0700."""
    if os.name != "nt":
        Path(folder).chmod(0o700)
        return
    from ... import schedule  # noqa: PLC0415
    sid = conf.profile_sid(getattr(conf.settings(), "HOME_DIR", None) or conf.user_home())
    if sid is None:
        raise OSError("the home's user profile was not found")
    icacls = schedule.system_dir() / "icacls.exe"
    p = subprocess.run([str(icacls), str(folder), "/inheritance:r", "/grant:r", f"*{SYSTEM}:(OI)(CI)F",
                        f"*{ADMINISTRATORS}:(OI)(CI)F", f"*{sid}:(OI)(CI)F"], capture_output=True, timeout=60)
    if p.returncode != 0:
        raise OSError(f"icacls rc={p.returncode}")


def acl_problem(folder):
    """What capture's precheck would say about the folder's ACL, or None (Windows only)."""
    if os.name != "nt":
        return None if (Path(folder).stat().st_mode & 0o077) == 0 else "readable by other users (mode is not 0700)"
    from ...capture import precheck  # noqa: PLC0415
    try:
        info = precheck.acl_info([str(folder)]).get(str(folder)) or {}
    except Exception as e:  # noqa: BLE001
        return f"ACL unreadable (error {type(e).__name__})"
    rules = info.get("rules") or []
    if not rules:
        return "ACL unreadable"
    if any("ContainerInherit" not in r["inh"] or "ObjectInherit" not in r["inh"] for r in rules):
        return "an ACL entry without (OI)(CI)"
    if not info.get("protected"):
        return "the ACL inherits from the folder above (not protected)"
    return None


def search_problem(folder):
    """None when Windows Search does not index the folder (excluded, or the indexer not running)."""
    if os.name != "nt":
        return None
    from ...capture import precheck  # noqa: PLC0415
    from ... import schedule  # noqa: PLC0415
    sc = schedule.system_dir() / "sc.exe"
    if "RUNNING" not in subprocess.run([str(sc), "query", "WSearch"], capture_output=True, text=True).stdout:
        return None
    t = str(folder).lower().rstrip("\\")
    best = None
    for _sub, p, inc in precheck.search_rules():
        if t == p or t.startswith(p + "\\"):
            if best is None or len(p) > len(best[0]):
                best = (p, inc)
    return None if best is not None and best[1] == 0 else \
        "not excluded from Windows Search (Indexing Options > Modify: untick it)"


def placeholders(value, key=""):
    """The keys whose value still holds a <...> placeholder of config.example.json."""
    if isinstance(value, dict):
        return [k for kk, v in value.items() for k in placeholders(v, f"{key}.{kk}" if key else kk)]
    if isinstance(value, list):
        return [k for v in value for k in placeholders(v, key)]
    return [key] if isinstance(value, str) and "<" in value and ">" in value else []


def config_for(app, base, data_parent):
    cfg = dict(base)
    from ...capture import paths  # noqa: PLC0415
    cfg.update(v=1, impl=paths.IMPL_NAME, data_root=str(data_parent / app), inputs_root=str(data_parent / "inputs"))
    cfg.setdefault("sources", {})
    return cfg


class Command(BaseCommand):
    help = ("make the machine ready for the app's capture: data parent (ACL, Search exclusion), data root, "
            "config.json, and the app's own defaults and provisioning (--provision reruns only the provisioning)")
    args = "[--from CFG|-] [--force] [--provision] [--scan] [--root HEXFILE] [--restart-client]"
    app = "required"

    def run(self, app, argv):
        ap = argparse.ArgumentParser(prog=f"manage.py setup {app}")
        ap.add_argument("--from", dest="source", help="take the config from this file (- : stdin)")
        ap.add_argument("--force", action="store_true", help="replace a config.json that is already there")
        ap.add_argument("--provision", action="store_true",
                        help="rerun only the app's provisioning on the config.json that is already there")
        ap.add_argument("--scan", action="store_true",
                        help="provisioning also scans the running client's memory (the app's fallback)")
        ap.add_argument("--root", default=None, metavar="HEXFILE", help="install this key file (the app verifies it)")
        ap.add_argument("--restart-client", action="store_true",
                        help="let the app's provisioning restart its client to rescan it")
        a = ap.parse_args(argv)
        from ...capture import paths  # noqa: PLC0415
        parent = Path(paths.DATA_PARENT)
        if not parent.is_absolute():
            print("setup: refused: settings.DATA_PARENT is not an absolute path", file=sys.stderr)
            return 2
        root, cfg_path = parent / app, parent / app / "config.json"
        if a.provision:
            if not cfg_path.is_file():
                print(f"setup: refused: no config.json to provision at {cfg_path} "
                      "(manage.py setup " + app + " [--from ...] writes one)", file=sys.stderr)
                return 2
            try:
                cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                print("setup: refused: the config.json that is there is unreadable (not JSON)", file=sys.stderr)
                return 2
            if not isinstance(cfg, dict):
                print("setup: refused: the config.json that is there is not a JSON object", file=sys.stderr)
                return 2
            attention = 0
            _say(f"{app}: provisioning the config.json that is there")
            for problem in registry.get().apps[0].setup_provision(cfg, _say, a.restart_client, a.scan, a.root):
                attention += 1
                _say(problem)
            left = placeholders(cfg)
            if left:
                _say(f"{app}: fill in config.json: {', '.join(left)}; then manage.py check {app}")
                return 3
            from ...capture.config import Config, ConfigError  # noqa: PLC0415
            try:
                Config(cfg_path)
            except ConfigError as e:
                _say(f"{app}: config refused ({e}); manage.py check {app} after fixing it")
                return 3
            _say(f"{app}: config ok")
            return 3 if attention else 0
        if cfg_path.exists() and not a.force:
            print(f"setup: refused: {app}'s config.json is already there (--force replaces it, "
                  "--provision reruns its provisioning)", file=sys.stderr)
            return 2
        try:
            if a.source == "-":
                base = json.loads(sys.stdin.read())
            elif a.source:
                base = json.loads(Path(a.source).read_text(encoding="utf-8"))
            else:
                base = json.loads((Path(conf.PROJECT_DIR) / app / "config.example.json").read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            print(f"setup: refused: the config to start from is unreadable (error {type(e).__name__})",
                  file=sys.stderr)
            return 2
        if not isinstance(base, dict):
            print("setup: refused: the config to start from is not a JSON object", file=sys.stderr)
            return 2
        attention = 0
        try:
            if not parent.exists():
                parent.mkdir(parents=True)
                protect(parent)
                _say("data parent created (protected ACL)")
            else:
                _say("data parent there")
            for problem in (acl_problem(parent), search_problem(parent)):
                if problem:
                    attention += 1
                    _say(f"data parent: {problem}")
            root.mkdir(exist_ok=True)
            cfg = config_for(app, base, parent)
            registry.get().apps[0].setup_defaults(cfg, _say)
            tmp = cfg_path.with_name("config.json.part")
            tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            tmp.replace(cfg_path)
        except OSError as e:
            print(f"setup: error {type(e).__name__}", file=sys.stderr)
            return 1
        _say(f"{app}: data root and config.json ready (sources: {len(cfg['sources'])})")
        for problem in registry.get().apps[0].setup_provision(cfg, _say, a.restart_client, a.scan, a.root):
            attention += 1
            _say(problem)
        left = placeholders(cfg)
        if left:
            _say(f"{app}: fill in config.json: {', '.join(left)}; then manage.py check {app}")
            return 3
        from ...capture.config import Config, ConfigError  # noqa: PLC0415
        try:
            Config(cfg_path)
        except ConfigError as e:
            _say(f"{app}: config refused ({e}); manage.py check {app} after fixing it")
            return 3
        _say(f"{app}: config ok" + ("" if not attention else "; capture will refuse until the data parent is fixed"))
        return 3 if attention else 0
