# -*- coding: utf-8 -*-
"""python -s -B manage.py <command> [<app>] ...   (the project's single entry point, Django-style)

A command is a module rawlayer/management/commands/<name>.py defining `Command`
(rawlayer/management/base.py); "_" in a file name is "-" on the command line. `manage.py help` lists them,
`manage.py help <command>` shows one.

A command that works on an app's data root takes the app right after its name (`manage.py collect <app> ...`); a
project with a single app may leave it out. Such a command selects the app before it starts (one process, one app,
one ledger: rawlayer/registry.py), and its --config defaults to <DATA_PARENT>/<app>/config.json.

Exit codes: 0 ok | 3 suspect (capture), or needs attention (schedule, check) | 75 busy or skipped | 2 refused or
usage | 1 error."""
import importlib
import sys
from pathlib import Path

from .. import registry
from .base import BaseCommand, default_config, has_option

_HERE = Path(__file__).resolve().parent          # resolved, as conf.PROJECT_DIR is (the folder is only listed)


def core_commands():
    """{command: module} of the core."""
    try:
        names = sorted(p.name for p in (_HERE / "commands").iterdir())
    except OSError:
        return {}
    return {n[:-3].replace("_", "-"): f"{__name__}.commands.{n[:-3]}" for n in names
            if n.endswith(".py") and not n.startswith("_") and n[:-3].isidentifier()}


def load(name, module):
    cls = getattr(importlib.import_module(module), "Command", None)
    if not (isinstance(cls, type) and issubclass(cls, BaseCommand)):
        raise registry.RegistryError(f"{module} defines no Command")
    return cls(name)


def _err(msg, rc=2):
    print(f"manage.py: {msg}", file=sys.stderr)
    return rc


def usage(topic=None):
    """The command list, or one command's synopsis and help."""
    core = core_commands()
    if topic is not None:
        if topic not in core:
            return None
        c = load(topic, core[topic])
        app = {"required": " <app>", "optional": " [<app>]"}.get(c.app, "")
        return f"manage.py {topic}{app} {c.args}".rstrip() + f"\n  {c.help}"
    lines = [__doc__.split("\n\n")[0], "", "commands:"]
    for name, mod in core.items():
        try:
            c = load(name, mod)
            lines.append(f"  {name:<13} {c.help}")
        except Exception as e:  # noqa: BLE001 -- a help listing never fails on one command
            lines.append(f"  {name:<13} (does not load: error {type(e).__name__})")
    lines.append(f"apps: {', '.join(registry.discover()) or 'none'}   (manage.py help <command>)")
    return "\n".join(lines)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    if not argv or argv[0] in ("-h", "--help", "help"):
        if len(argv) > 1:
            text = usage(argv[1])
            if text is None:
                return _err(f"unknown command {argv[1]!r} (manage.py help)")
            print(text)
            return 0
        print(usage())
        return 0 if argv else 2
    name, rest = argv[0], argv[1:]
    apps = registry.discover()
    try:
        core = core_commands()
        if name not in core:
            return _err(f"unknown command {name!r} (manage.py help)")
        cmd = load(name, core[name])
    except registry.RegistryError as e:
        return _err(str(e), 1)
    except Exception as e:  # noqa: BLE001 -- a command module that does not import
        return _err(f"{name} does not load (error {type(e).__name__}; manage.py check --traceback)", 1)
    app = None
    if cmd.app:
        if rest and rest[0] in apps:
            app, rest = rest[0], rest[1:]
        elif len(apps) == 1 and cmd.app == "required":
            app = apps[0]
        elif cmd.app == "required":
            return _err(f"{name}: name the app: manage.py {name} <app> ...  (apps: {', '.join(apps) or 'none'})")
    if app is not None:
        try:
            registry.select(app)
        except registry.RegistryError as e:
            return _err(f"app {app}: {e}", 1)
        except Exception as e:  # noqa: BLE001 -- an app that does not import: check tells more
            return _err(f"app {app} does not load (error {type(e).__name__}; manage.py check {app} --traceback)", 1)
        if cmd.config and not has_option(rest, "--config"):
            rest = rest + ["--config", str(default_config(app))]        # argv is text
    return cmd.run(app, rest)


__all__ = ["BaseCommand", "main", "usage", "core_commands", "load", "default_config"]
