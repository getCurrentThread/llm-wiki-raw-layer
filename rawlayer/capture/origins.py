# -*- coding: utf-8 -*-
"""Origins: what an app tells the core about each source it reads (Django ModelAdmin-style).

An app subclasses Origin once per origin (<label>/origins.py) and implements read(). What differs between
origins is a hook with a core default; the rules the ledger depends on are core mixins, written and tested once,
that an app combines:

  Origin               a key that was there and is not now -> 'vanished': the origin is suspect and commits
                       nothing this run (SPEC 8.3)
  PendingAbsentMixin   two-phase absent behind a shrink guard (SPEC-CLARIFICATIONS Q10): a key missing from a
                       container that read completely becomes pending, and absent when it is still missing next
                       time; a run that sees too little, or too many new pendings, is suspect and changes nothing
  MultiVersionMixin    the origin is read in one stable pass and diffed as a whole; every version of a key seen in
                       the pass is recorded once (Q3); the app implements read_once() instead of read()

Writing the ledger (OriginRun.record / absent_record, the commit, the manifest) is never a hook, and run() is the
core's: the registry refuses an app that overrides it."""
import time
from collections import Counter
from typing import NamedTuple

from . import engine, multiver, publicout
from .publicout import lit

BLOCKING = ("error", "unstable", "skipped")         # origin statuses that mean "not read this run" (exit 75)
HIST_DDL = ("CREATE TABLE IF NOT EXISTS hist(origin TEXT NOT NULL, key TEXT NOT NULL, h TEXT NOT NULL,\n"
            "  PRIMARY KEY(origin, key, h)) WITHOUT ROWID;")


class Guard(NamedTuple):
    """Shrink guard of PendingAbsentMixin: the run is suspect when it observed fewer than min_obs_ratio of the
    keys the origin held, or found more new missing keys than max(max_new_abs, max_new_frac * held)."""
    min_obs_ratio: float = 0.9
    max_new_abs: int = 50
    max_new_frac: float = 0.01


class Origin:
    name = None                          # ^[a-z_]+$: the ledger directory and the manifest key
    kinds = frozenset()                  # record kinds this origin writes
    day_kinds = frozenset()              # kinds counted per KST day in the container facts ("days")
    ok_statuses = frozenset({"ok"})      # container statuses whose observations are diffed
    complete_status = "ok"               # a container in this status was read completely
    statuses = frozenset()               # container / origin statuses beyond the core's (registry.CORE_STATUSES)
    state_tables = ()                    # ((table, DDL), ...) state.sqlite tables apply_put maintains
    fail_statuses = frozenset()          # ContainerError statuses a failed container keeps
    fail_default = "error"               # the status of a failed container otherwise

    def __init__(self, app=None):
        self.app = app

    def __repr__(self):
        return f"<Origin {self.name}>"

    # ---- reading
    def read(self, ctx):
        """-> [model.Container]. An exception here makes the origin 'error' (read_error) for this run."""
        raise NotImplementedError

    def run(self, ctx):
        """Read, diff every container, decide (core; never overridden by an app)."""
        orun = engine.OriginRun(ctx, self)
        try:
            containers = self.read(ctx)
        except Exception as e:  # noqa: BLE001
            orun.status = "error"
            orun.notes["read_error"] += 1
            log_exc(self.name, e)
            return orun
        for c in containers:
            engine.process_container(orun, c)
        engine.finish_origin(orun)
        return orun

    # ---- decisions
    def fail_status(self, status):
        """The status of a container whose read failed with `status` (ContainerError) or 'error' (I/O)."""
        return status if status in self.fail_statuses else self.fail_default

    def on_missing(self, orun, c, live_heads, counted, missing, drops):
        """After a container was diffed: `missing` = [(key, Head)] put before under it and not observed now."""
        if missing:
            orun.vanished += len(missing)
            c.notes["vanished"] += len(missing)

    def finish(self, orun):
        """Origin-level decision after every container."""
        if orun.vanished:
            orun.status = "suspect"
            orun.spill.discard()
            orun.counts = Counter()
            orun.pending_drop = []

    def blocks_run(self, orun):
        """True when this run could not read the origin, or one of its containers (capture exit 75)."""
        return orun.status in BLOCKING or \
            any(isinstance(f, dict) and f.get("status") == "error" for f in orun.containers.values())

    # ---- state.sqlite
    def apply_put(self, cur, rec):
        """Fold one committed put record into this origin's state_tables (replay runs it too)."""

    # ---- the run as a whole
    def estimate_peak(self, inputs):
        """Bytes beyond the core's 1 GiB a run of this origin may add to the data volume."""
        return 0

    def requires_system(self, cfg, input_set):
        """True when this origin may only be read by the SYSTEM account."""
        return False


def log_exc(origin, e):
    publicout.say(lit("origin error"), origin, errno=getattr(e, "errno", None) if isinstance(getattr(e, "errno", None), int) else None)


class PendingAbsentMixin:
    ok_statuses = frozenset({"clean", "partial"})
    statuses = frozenset({"clean", "partial"})
    complete_status = "clean"
    guard = Guard()

    def on_missing(self, orun, c, live_heads, counted, missing, drops):
        # Q10: a container that did not read completely leaves its pending set untouched
        if c.status == self.complete_status:
            orun.pending_drop.extend(drops)
            orun.guard_prev += len(live_heads)
            orun.guard_obs += len(counted)
            for k, hd in missing:
                if hd.pending:
                    orun.absent_cands.append((k, hd, c.asof_s))
                else:
                    orun.pending_cands.append(k)

    def blocks_run(self, orun):
        # container statuses are routine here; the shrink guard judges them
        return orun.status in BLOCKING

    def finish(self, orun):
        g = self.guard
        n_new = len(orun.pending_cands) + len(orun.absent_cands)
        prev = orun.guard_prev
        # N_obs == 0 (no container read completely) trips even when N_prev == 0 - containers that never read
        # completely must not pass as ok - unless there is nothing to guard: no container this run and no put of
        # this origin in the ledger (SPEC-CLARIFICATIONS Q12).
        had_puts = orun.ctx.state.execute(
            "SELECT 1 FROM heads WHERE origin=? AND op='put' LIMIT 1", (orun.origin,)).fetchone() is not None
        tripped = (orun.guard_obs == 0 and (prev > 0 or had_puts or bool(orun.containers))) or \
            (prev > 0 and (orun.guard_obs < g.min_obs_ratio * prev or n_new > max(g.max_new_abs, g.max_new_frac * prev)))
        unprintable = sum(1 for k in orun.pending_cands if not publicout.ok_str(k))
        if tripped or unprintable:          # Q10 6-7: no add / absent this run; drops and puts still commit
            orun.status = "suspect"
            if tripped:
                orun.notes["shrink_guard"] += 1
            if unprintable:
                orun.notes["key_unprintable"] += unprintable
        else:
            for k, hd, asof_s in orun.absent_cands:
                orun.absent_record(k, hd, asof_s)
            orun.pending_add = sorted(orun.pending_cands)


class Unstable(Exception):
    """read_once(): the source changed while it was read; the pass is repeated."""


class MultiVersionMixin:
    attempts = 3
    retry_sleep = 0.5
    state_tables = (("hist", HIST_DDL),)

    def read_once(self, ctx):
        """-> multiver.OriginRead of one pass. Raise Unstable when the source moved under the pass; OSError or
        ValueError make the origin 'error' (read_error); anything else stops the capture (exit 1)."""
        raise NotImplementedError

    def run(self, ctx):
        orun = engine.OriginRun(ctx, self)
        rd = None
        try:
            for attempt in range(self.attempts):
                try:
                    rd = self.read_once(ctx)
                    break
                except Unstable:
                    if attempt + 1 < self.attempts:
                        time.sleep(self.retry_sleep)
            if rd is None:
                orun.status = "unstable"
                orun.notes["unstable"] += 1
                return orun
        except (OSError, ValueError):
            orun.status = "error"
            orun.notes["read_error"] += 1
            return orun
        for k, v in rd.notes.items():
            orun.notes[k] += v
        orun.srcmap.update(rd.srcmap)
        multiver.diff(orun, rd)
        return orun

    def apply_put(self, cur, rec):
        cur.execute("INSERT OR IGNORE INTO hist(origin,key,h) VALUES(?,?,?)", (rec["origin"], rec["key"], rec["h"]))
