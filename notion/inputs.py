from pathlib import Path


def resolve(cfg, input_set, frozen_root):
    return {"nt_archive": Path(cfg.sources["nt_archive"]) if frozen_root is None
            else Path(frozen_root, "nt", "archive")}


def static_roots(inputs):
    return {}
