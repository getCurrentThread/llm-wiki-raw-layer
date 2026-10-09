from pathlib import Path


def resolve(cfg, input_set, frozen_root):
    return {"dc_archive": Path(cfg.sources["dc_archive"]) if frozen_root is None
            else Path(frozen_root, "dc", "archive")}


def static_roots(inputs):
    return {}
