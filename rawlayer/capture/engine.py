# -*- coding: utf-8 -*-
"""Change determination (SPEC 8) and container facts (SPEC 9.3), per container.

What differs between origins is a hook of the origin (rawlayer.capture.origins.Origin: ok_statuses, day_kinds,
fail_status, on_missing, finish); an origin judged as a whole (the multi-version rule) diffs in multiver.py.
Nothing here names an origin."""
import sqlite3
from collections import Counter, defaultdict

from . import canon, state
from .ledger import OriginSpill
from .model import ContainerError


class OriginRun:
    def __init__(self, ctx, spec):
        origin = spec.name
        self.ctx = ctx
        self.spec = spec            # the Origin whose hooks decide
        self.origin = origin
        self.spill = OriginSpill(ctx.work_dir, origin)
        self.counts = Counter()
        self.notes = ctx.origin_notes[origin]
        self.containers = {}
        self.srcmap = {}            # source_id -> relpath (.state/srcmap.json only)
        self.pending_add = []
        self.pending_drop = []
        self.status = "ok"
        self.absent_cands = []      # (key, Head, container as-of)
        self.pending_cands = []     # keys
        self.vanished = 0
        self.guard_prev = 0
        self.guard_obs = 0
        self.recorded = set()       # keys a settled container of this run recorded: one record per key per run
        # At genesis no key can sit under another container, so skip the per-key fallback lookup.
        self.origin_has_heads = ctx.state.execute(
            "SELECT 1 FROM heads WHERE origin=? LIMIT 1", (origin,)).fetchone() is not None

    def record(self, ob, rev, h, src):
        ctx = self.ctx
        rec = {"v": 1, "kind": ob.kind, "origin": self.origin, "key": ob.key, "rev": rev, "op": "put",
               "run": ctx.run, "seq": ctx.seq, "src": src, "observed_at_s": ctx.run_start_s,
               "src_asof_s": ob.src_asof_s, "ts_ms": ob.ts_ms, "native": ob.native, "derived": ob.derived,
               "h": h}
        self.spill.add(ob.key, rev, canon.canon(rec))
        self.counts["put"] += 1

    def absent_record(self, key, hd, asof_s):
        ctx = self.ctx
        rec = {"v": 1, "kind": hd.kind, "origin": self.origin, "key": key, "rev": hd.rev + 1, "op": "absent",
               "run": ctx.run, "seq": ctx.seq, "src": hd.src, "observed_at_s": ctx.run_start_s,
               "src_asof_s": asof_s, "ts_ms": None, "native": None, "derived": None, "h": None}
        self.spill.add(key, hd.rev + 1, canon.canon(rec))
        self.counts["absent"] += 1


def process_container(orun: OriginRun, c):
    """Diff one container against state; facts go to orun.containers[c.sid]."""
    ctx = orun.ctx
    spec = orun.spec
    origin = orun.origin
    orun.srcmap[c.sid] = c.relpath
    if c.status not in spec.ok_statuses or c.iter_obs is None:
        orun.containers[c.sid] = c.facts_base()
        _merge_notes(orun, c)
        return
    heads = state.heads_for_src(ctx.state, origin, c.sid)
    orun.spill.begin()
    put_mark = orun.counts["put"]   # a rolled-back container takes its puts out of the manifest count too
    seen = set()
    counted = set()             # SPEC 9.3 counted keys = "observed" for 8.3 (Q10)
    drops = []                  # pending keys seen again; kept only if the room read clean (Q10)
    kinds = defaultdict(list)
    days = Counter()
    recorded = []
    try:
        for ob in c.iter_obs():
            if ob.key in seen:
                raise ContainerError("error", "dup_key")
            seen.add(ob.key)
            if (origin, ob.key) in ctx.denylist:
                c.notes["denied"] += 1
                continue
            try:
                h = canon.record_h(ob.native, ob.derived)
            except (ValueError, TypeError):
                c.notes["value_bad"] += 1
                seen.discard(ob.key)
                continue
            kinds[ob.kind].append(ob.key + "\t" + h)
            counted.add(ob.key)
            if ob.kind in spec.day_kinds:
                days[canon.kst_day(ob.ts_ms)] += 1
            hd = heads.get(ob.key)
            if hd is None and orun.origin_has_heads:
                hd = state.head(ctx.state, origin, ob.key)   # key seen before under another container
            if hd is None or hd.op != "put" or hd.h != h:
                if ob.key in orun.recorded:     # an earlier container recorded it at the same rev this run
                    raise ContainerError("error", "dup_key")
                orun.record(ob, (hd.rev if hd else 0) + 1, h, c.sid)
                recorded.append(ob.key)
            if hd is not None and hd.pending:
                drops.append(ob.key)
    except ContainerError as e:
        orun.spill.rollback()
        orun.counts["put"] = put_mark
        c.status = spec.fail_status(e.status)
        c.notes[e.note] += 1
        orun.containers[c.sid] = c.facts_base()
        _merge_notes(orun, c)
        return
    except (sqlite3.Error, OSError):
        orun.spill.rollback()
        orun.counts["put"] = put_mark
        c.status = spec.fail_status("error")
        c.notes["read_error"] += 1
        orun.containers[c.sid] = c.facts_base()
        _merge_notes(orun, c)
        return
    finally:
        if c.cleanup:
            try:
                c.cleanup()
            except Exception:  # noqa: BLE001
                pass
    orun.spill.settle()
    orun.recorded.update(recorded)
    # keys that were there before and are not now
    # Q10: every kind (room, schema, rows) is a key of the room; denylisted keys are outside 8.3; "observed"
    # = counted keys (kinds); a non-clean room (partial included) leaves its pending set untouched.
    live_heads = {k: hd for k, hd in heads.items() if hd.op == "put" and (origin, k) not in ctx.denylist}
    missing = [(k, hd) for k, hd in live_heads.items() if k not in counted]
    spec.on_missing(orun, c, live_heads, counted, missing, drops)
    f = c.facts_base()
    f["kinds"] = {k: {"n": len(v), "digest": canon.facts_digest(v)} for k, v in sorted(kinds.items())}
    if days:
        f["days"] = dict(sorted(days.items()))
    orun.containers[c.sid] = f
    _merge_notes(orun, c)


def _merge_notes(orun, c):
    for k, v in c.notes.items():
        if v:
            orun.notes[k] += v


def not_read(orun: OriginRun):
    """SPEC 13 'skipped' (capture exit 75): the origin, or a container the origin cannot do without, could not be
    read this run (Origin.blocks_run)."""
    return orun.spec.blocks_run(orun)


def finish_origin(orun: OriginRun):
    """Origin-level decisions (Origin.finish: vanished -> suspect, or the shrink guard and absents)."""
    orun.spec.finish(orun)
    orun.pending_drop = sorted(set(orun.pending_drop))


def origin_manifest(orun: OriginRun):
    return {"status": orun.status,
            "counts": {"put": orun.counts.get("put", 0), "absent": orun.counts.get("absent", 0)},
            "notes": {k: v for k, v in sorted(orun.notes.items()) if v},
            "pending": {"add": orun.pending_add, "drop": orun.pending_drop},
            "containers": orun.containers}
