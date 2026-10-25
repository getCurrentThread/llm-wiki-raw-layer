from pathlib import Path


def resolve(cfg, input_set, frozen_root):
    return {"cc_archive": Path(cfg.sources["cc_archive"]) if frozen_root is None
            else Path(frozen_root, "cc", "archive")}


def static_roots(inputs):
    return {}
