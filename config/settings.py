# -*- coding: utf-8 -*-
"""Project settings (Django-style): the values every app's ledger records and the place every data root sits.
The apps are not listed here: every folder next to manage.py that holds __init__.py and apps.py is one
(rawlayer/registry.py). Machine-specific values (data_root, source paths, app options) live in each app's
config.json (<DATA_PARENT>/raw/<app>/config.json by default). A change here is a change of every app's code: the next
manifest of every ledger records the work tree as dirty until it is committed."""
from pathlib import Path

from rawlayer.conf import user_home

# The project directory: the folder that holds manage.py, config/, rawlayer/ and the apps.
BASE_DIR = Path(__file__).resolve().parent.parent

# The home of the user this checkout belongs to: the user profile folder that holds BASE_DIR. Not Path.home():
# the scheduled task runs as SYSTEM, whose home is the systemprofile.
HOME_DIR = user_home(BASE_DIR)

# Recorded in every manifest ("impl": {"name": ...}); each config.json must carry the same value.
IMPL_NAME = "claude"

# ACL'd, Search-excluded parent of every data root (SPEC 10.1). Config refuses a data_root outside it. The data
# roots sit in its raw/ folder, <DATA_PARENT>/raw/<app> (rawlayer/capture/paths.py data_root).
DATA_PARENT = HOME_DIR / "llm-wiki-data"

# sqlite-vec's loadable extension for search --hybrid (llm.md 12.2): <SQLITE_VEC_DIR>/<version>/vec0.dll. Outside the
# data parent on purpose: every process of the owner can write there, and only an administrator can change this
# folder, so the DLL that dense.py checks against its pinned SHA-256 cannot be swapped before it is loaded.
SQLITE_VEC_DIR = Path(r"C:\Program Files\sqlite-vec")

# App folders to leave out of this project (an app being moved in or out, say). Never leave out an app whose
# scheduled task is still registered: its runs would fail.
EXCLUDE_APPS = ()
