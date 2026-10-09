# -*- coding: utf-8 -*-
from pathlib import Path


def resolve(cfg, input_set, frozen_root):
    root = Path(cfg.sources["toy_dir"]) if frozen_root is None else Path(frozen_root) / "toy"
    return {"toy_a": root / "a", "toy_b": root / "b", "toy_m": root / "m"}


def static_roots(inputs):
    return {"toy_a": (inputs.toy_a, True)}
