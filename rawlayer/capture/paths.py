# -*- coding: utf-8 -*-
"""Fixed machine paths (SPEC 2, 10.1), from the project's config/settings.py. Nothing here is derived from the
environment: under the SYSTEM task LOCALAPPDATA and USERPROFILE point at systemprofile.

IMPL_NAME is recorded in every manifest (config.json must say the same); DATA_PARENT (a Path) is the ACL'd,
Search-excluded parent every data root must sit in; REPO (a Path) is the project directory (git_head / dirty)."""
import os
from pathlib import Path

from .. import conf

IMPL_NAME = conf.settings().IMPL_NAME
DATA_PARENT = Path(conf.settings().DATA_PARENT)
REPO = conf.PROJECT_DIR


def inside(path, parent):
    """True when `path` is strictly below `parent` (str or os.PathLike; case-insensitive, after normalisation)."""
    # strings on purpose: abspath folds ".." without following links, normcase folds case the way the OS does
    p = os.path.normcase(os.path.abspath(path)).rstrip("\\/")
    q = os.path.normcase(os.path.abspath(parent)).rstrip("\\/")
    return p.startswith(q + os.sep)
