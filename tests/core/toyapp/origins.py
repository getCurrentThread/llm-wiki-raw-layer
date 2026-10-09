# -*- coding: utf-8 -*-
from rawlayer.capture import fsio
from rawlayer.capture.origins import Guard, MultiVersionMixin, Origin, PendingAbsentMixin

from . import readers

TOY_LAST = ("toy_last", "CREATE TABLE IF NOT EXISTS toy_last(key TEXT PRIMARY KEY, h TEXT NOT NULL,\n"
                        "  seq INTEGER NOT NULL) WITHOUT ROWID;")


class ToyA(Origin):
    """Plain: a key that disappears is vanished (the origin is suspect). Keeps the last h per key in toy_last."""
    name = "toy_a"
    kinds = frozenset({"item"})
    day_kinds = frozenset({"item"})
    state_tables = (TOY_LAST,)

    def read(self, ctx):
        return readers.containers(self.name, ctx.inputs.toy_a, "item")

    def apply_put(self, cur, rec):
        cur.execute("INSERT OR REPLACE INTO toy_last(key, h, seq) VALUES(?,?,?)", (rec["key"], rec["h"], rec["seq"]))

    def estimate_peak(self, inputs):
        root = inputs.toy_a
        return sum(fsio.size(p) for p in root.iterdir()) if root.is_dir() else 0

    def requires_system(self, cfg, input_set):
        return input_set == "live" and cfg.toy_system


class ToyB(PendingAbsentMixin, Origin):
    """Two-phase absent behind a shrink guard that trips on more than 3 new missing keys."""
    name = "toy_b"
    kinds = frozenset({"item"})
    statuses = PendingAbsentMixin.statuses | {"unreadable"}
    fail_statuses = frozenset({"unreadable"})
    fail_default = "unreadable"
    guard = Guard(min_obs_ratio=0.9, max_new_abs=3, max_new_frac=0.01)

    def read(self, ctx):
        return readers.containers(self.name, ctx.inputs.toy_b, "item", status="clean", partial=True)


class ToyM(MultiVersionMixin, Origin):
    """Read in one stable pass; every version of a key is recorded once."""
    name = "toy_m"
    kinds = frozenset({"rec"})
    day_kinds = frozenset({"rec"})
    retry_sleep = 0

    def read_once(self, ctx):
        return readers.read_versions(self.name, ctx.inputs.toy_m, ctx.denylist)


ORIGINS = (ToyA, ToyB, ToyM)
