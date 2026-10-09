# -*- coding: utf-8 -*-
from rawlayer.registry import AppConfig, Option


def _flag(v):
    if not isinstance(v, bool):
        raise ValueError("toy_system")
    return v


class ToyConfig(AppConfig):
    label = "toy"
    source_keys = ("toy_dir",)
    options = (Option("toy_system", False, _flag),)

    def task_checks(self, cfg):
        return [] if cfg.toy_system else ["toy_system must be true"]
