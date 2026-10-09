# -*- coding: utf-8 -*-
"""Where mattertok's archive is for an input set: `live` reads config.json "sources"; a frozen set reads its copy
under <inputs_root>/<set>/mm/archive."""
from pathlib import Path


def resolve(cfg, input_set, frozen_root):
    # the archive directory is only read from (archive.read_once), never written as text: a Path of the config's
    # string names the same files
    if frozen_root is None:
        return {"mm_archive": Path(cfg.sources["mm_archive"])}
    return {"mm_archive": Path(frozen_root, "mm", "archive")}


def static_roots(inputs):
    return {}
