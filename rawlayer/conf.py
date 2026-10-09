# -*- coding: utf-8 -*-
"""The project's settings: config/settings.py in the project directory, the directory that holds manage.py,
rawlayer/ and the apps (Django's layout: settings.BASE_DIR is that directory too).

It is found by location, never through the environment or sys.path (under the SYSTEM task neither can be trusted
to point at this checkout). For the same reason user_home() finds the home of the user a checkout belongs to from
the machine's user profiles, not from whoever runs the process, and refuses (HomeError) in a service account's
process when no profile holds the checkout."""
import importlib.util
import os
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
_MODULE = "config.settings"
SETTINGS_FILE = PROJECT_DIR / "config" / "settings.py"
# the service accounts' profiles (SYSTEM, LOCAL SERVICE, NETWORK SERVICE): never the home of a project
_SERVICE_SIDS = ("S-1-5-18", "S-1-5-19", "S-1-5-20")


def settings():
    mod = sys.modules.get(_MODULE)
    # resolved like SETTINGS_FILE, so a module imported through a link to this checkout is still this one
    if mod is not None and os.path.normcase(Path(getattr(mod, "__file__", "") or "").resolve()) == \
            os.path.normcase(SETTINGS_FILE):
        return mod
    spec = importlib.util.spec_from_file_location(_MODULE, str(SETTINGS_FILE))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    sys.modules[_MODULE] = mod
    return mod


def profile_sid(home):
    """Windows: the SID of the user whose profile folder is `home` (None when no profile is)."""
    if os.name != "nt":
        return None
    want = Path(home).resolve()
    for sid, folder in _profiles(with_sid=True):
        try:
            if folder.resolve() == want:
                return sid
        except OSError:
            continue
    return None


def _profiles(with_sid=False):
    """The home folders of the machine's users: Windows' ProfileList (service accounts left out), elsewhere the
    passwd entries (a home of / left out). with_sid: (sid, folder) pairs (Windows only)."""
    if os.name == "nt":
        import winreg  # noqa: PLC0415
        base = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\ProfileList"
        try:
            k = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base)
        except OSError:
            return
        with k:
            i = 0
            while True:
                try:
                    sid = winreg.EnumKey(k, i)
                except OSError:
                    break
                i += 1
                if sid in _SERVICE_SIDS:
                    continue
                try:
                    with winreg.OpenKey(k, sid) as kk:
                        folder = winreg.QueryValueEx(kk, "ProfileImagePath")[0]
                except OSError:
                    continue
                if isinstance(folder, str) and folder:
                    path = Path(os.path.expandvars(folder))
                    yield (sid, path) if with_sid else path
    else:
        import pwd  # noqa: PLC0415
        for entry in pwd.getpwall():
            home = Path(entry.pw_dir or "/")
            if home.is_absolute() and home != Path(home.anchor):
                yield home


class HomeError(RuntimeError):
    """No user's home can be told for the project (user_home under a service account)."""


def user_home(path=None):
    """The home folder of the user `path` (default: the project directory) belongs to: the user profile folder
    that holds it, the innermost when several do. Path.home() is the home of whoever runs the process, and the
    scheduled task runs as SYSTEM (home: the systemprofile), so it is only the answer when no profile holds the
    path, and never in a service account's process: there it would be the service's profile, a DATA_PARENT other
    than the one `schedule install` checked the config under (HomeError instead)."""
    target = Path(path if path is not None else PROJECT_DIR).resolve()
    best = None
    for home in _profiles():
        try:
            home = home.resolve()
        except OSError:
            continue
        if target.is_relative_to(home) and (best is None or len(home.parts) > len(best.parts)):
            best = home
    if best is not None:
        return best
    if _service_account():
        raise HomeError("the project is not inside a user profile folder: a service account's home is not its "
                        "user's (SYSTEM would see another DATA_PARENT)")
    return Path.home()


def _service_account():
    """True when this process runs as SYSTEM, LOCAL SERVICE or NETWORK SERVICE (Windows; never elsewhere)."""
    if os.name != "nt":
        return False
    from .capture import fsio  # noqa: PLC0415 -- the token is read through ctypes, only when it is needed
    return fsio.token_user_sid() in _SERVICE_SIDS
