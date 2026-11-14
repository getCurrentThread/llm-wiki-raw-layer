# -*- coding: utf-8 -*-
from pathlib import Path


def resolve(cfg):
    root = Path(cfg.sources["toy_dir"])
    return {"toy_a": root / "a", "toy_b": root / "b", "toy_m": root / "m"}
