from pathlib import Path


def resolve(cfg, input_set, frozen_root):
    return {"gm_archive": Path(cfg.sources["gm_archive"]) if frozen_root is None
            else Path(frozen_root, "gm", "archive")}


def static_roots(inputs):
    return {}
