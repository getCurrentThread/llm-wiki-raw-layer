# -*- coding: utf-8 -*-
"""What a command is: a module rawlayer/management/commands/<name>.py that defines `Command`, a BaseCommand
subclass."""
from pathlib import Path


class BaseCommand:
    """help: one line for `manage.py help`. args: the synopsis after the command and its <app>.

    app: None -- the command works on no app; "required" -- `manage.py <name> <app> ...` (the app may be left out
    in a project that has one); "optional" -- the command works on every app when <app> is left out.

    config: True -- `--config` defaults to <DATA_PARENT>/<app>/config.json.

    run(app, argv) -> exit code. When `app` is given the dispatcher has selected it (rawlayer.registry.select)."""
    help = ""
    args = ""
    app = None
    config = False

    def __init__(self, name):
        self.name = name

    def run(self, app, argv):
        raise NotImplementedError


def default_config(app):
    """<DATA_PARENT>/<app>/config.json (a Path): where an app's machine config sits by convention (next to its
    ledger)."""
    from ..capture import paths  # noqa: PLC0415
    return Path(paths.DATA_PARENT) / app / "config.json"


def has_option(argv, option):
    return any(a == option or a.startswith(option + "=") for a in argv)
