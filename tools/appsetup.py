# -*- coding: utf-8 -*-
"""What the apps' AppConfig.setup_defaults share (`manage.py setup <app>`, and `tools/bootstrap.py config defaults
<app>` on a config.json that is already there): fill a config key only while it is unset (missing, empty, or still
a config.example.json placeholder <...>), and create the archive folder an app's fetch step writes into. A value
the owner set is never replaced. `defaults` states them as a dict; `existing_path` is the task check they share.
Only key names reach `note`, never the values: they are machine paths."""
import os
from pathlib import Path


def unset(value):
    """True for a value setup may fill: missing, empty, or a placeholder of config.example.json."""
    return value is None or value == "" or (isinstance(value, str) and "<" in value and ">" in value)


def get(cfg, key):
    """The value at a dotted key (sources.gm_archive), or None."""
    node = cfg
    for part in key.split("."):
        node = node.get(part) if isinstance(node, dict) else None
    return node


def put(cfg, key, value):
    *parents, last = key.split(".")
    node = cfg
    for part in parents:
        child = node.get(part)
        if not isinstance(child, dict):
            child = node[part] = {}
        node = child
    node[last] = value


def profile_path(*parts):
    """<HOME>\\<parts...> when it exists, else None. <HOME> is the profile that holds the checkout (settings.HOME_DIR),
    which is also right under SYSTEM, whose own %APPDATA% is the system profile's."""
    from rawlayer import conf  # noqa: PLC0415
    home = getattr(conf.settings(), "HOME_DIR", None) or conf.user_home()
    p = Path(home, *parts)
    return p if p.exists() else None


def fill(cfg, note, values, folders=()):
    """Set each dotted key of `values` whose value is unset (a value of None is skipped: nothing to offer), then
    create the folder each key of `folders` names, when it is missing and inside the data root, and the data root is
    an existing folder inside the protected data parent (where a new folder inherits its ACL). Returns the keys
    set."""
    from rawlayer.capture import paths  # noqa: PLC0415
    done = []
    for key, value in values.items():
        if value is not None and unset(get(cfg, key)):
            put(cfg, key, str(value))
            done.append(key)
            note(f"{key}: set to its default")
    root = cfg.get("data_root")
    if not (isinstance(root, str) and os.path.isabs(root) and os.path.isdir(root)
            and paths.inside(root, paths.DATA_PARENT)):
        return done
    for key in folders:
        p = get(cfg, key)
        if isinstance(p, str) and _inside(p, root) and not os.path.isdir(p):
            Path(p).mkdir(parents=True, exist_ok=True)
            note(f"{key}: folder created")
    return done


def _inside(path, parent):
    p = os.path.normcase(os.path.abspath(path)).rstrip("\\/")
    q = os.path.normcase(os.path.abspath(parent)).rstrip("\\/")
    return os.path.isabs(path) and p.startswith(q + os.sep)


def defaults(cfg, note, data=None, home=None):
    """The declarative form of an app's setup_defaults, once the config names a data root: `data` maps a dotted key to a
    name under the data root (`sources.*` keys are folders `fill` creates), `home` a key to path parts under the
    owner's profile (offered only when that path exists). `fill` sets what is still unset, data keys first."""
    root = cfg.get("data_root")
    if not root:
        return []
    values = {key: Path(root, name) for key, name in (data or {}).items()}
    values.update((key, profile_path(*parts)) for key, parts in (home or {}).items())
    return fill(cfg, note, values, folders=[k for k in values if k.startswith("sources.")])


def existing_path(cfg, sync, key, kind, other):
    """A task_checks result: while `cfg.<sync>` is on, `cfg.<key>` must be an absolute path to an existing folder or
    file (`kind`), as SYSTEM has another `other` than the owner's."""
    if not getattr(cfg, sync):
        return []
    value = getattr(cfg, key)
    if value and os.path.isabs(value) and (os.path.isdir if kind == "folder" else os.path.isfile)(value):
        return []
    return [f"{sync} requires an absolute {key} to an existing {kind} (SYSTEM has another {other})"]
