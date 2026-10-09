# -*- coding: utf-8 -*-
"""The multi-version rule (SPEC 8.2, SPEC-CLARIFICATIONS Q3) for an origin read as a whole
(origins.MultiVersionMixin).

One pass of the origin observes keys in containers, possibly several times and in several versions; the diff
judges each key's versions together: every version not yet in the key's history is recorded once (in the order
it was first seen), then the version observed last becomes the head. A key put before and observed nowhere now
is vanished when its container was read ok (the origin is suspect), and its container counted as gone when the
container is not there any more (room_gone)."""
from collections import Counter

from . import canon, engine, state


class _Ver:
    __slots__ = ("first", "last", "src", "ob")

    def __init__(self, first, src, ob):
        self.first, self.last, self.src, self.ob = first, first, src, ob


class OriginRead:
    """Everything one pass observed."""

    def __init__(self, origin=None, denylist=frozenset()):
        self.origin = origin
        self.denylist = denylist
        self.seq = 0
        self.keys = {}            # key -> {h: _Ver}, plus the h of the last observation in last_h
        self.last_h = {}
        self.containers = {}      # sid -> facts base {"status": ...}
        self.versions = Counter()  # sid -> distinct (key, h) observed there
        self.seen_pairs = set()
        self.asof = {}
        self.notes = Counter()
        self.srcmap = {}

    def add(self, ob, sid):
        """One observation, unless the key is denylisted for this origin."""
        if (self.origin, ob.key) in self.denylist:
            self.notes["denied"] += 1
            return
        self.observe(ob, sid)

    def observe(self, ob, sid):
        try:
            h = canon.record_h(ob.native, ob.derived)
        except (ValueError, TypeError):
            self.notes["value_bad"] += 1
            return
        self.seq += 1
        vs = self.keys.get(ob.key)
        if vs is None:
            vs = self.keys[ob.key] = {}
        v = vs.get(h)
        if v is None:
            vs[h] = _Ver(self.seq, sid, ob)
        else:
            v.last, v.src, v.ob = self.seq, sid, ob
        self.last_h[ob.key] = h
        if (sid, ob.key, h) not in self.seen_pairs:
            self.seen_pairs.add((sid, ob.key, h))
            self.versions[sid] += 1


def diff(orun, rd):
    origin = orun.origin
    day_kinds = orun.spec.day_kinds
    con = orun.ctx.state
    heads = state.heads_for_origin(con, origin)
    hist = state.hist_for_origin(con, origin)
    kinds = {}      # sid -> kind -> [key \t h]
    days = {}
    for key in sorted(rd.keys):
        vs = rd.keys[key]
        rep_h = rd.last_h[key]
        rep = vs[rep_h]
        hd = heads.get(key)
        rev = hd.rev if hd else 0
        cur_h, cur_src, cur_op = (hd.h, hd.src, hd.op) if hd else (None, None, None)
        seen_h = hist.get(key, set())
        for h, v in sorted(((h, v) for h, v in vs.items() if h != rep_h), key=lambda t: t[1].first):
            if h not in seen_h:
                rev += 1
                orun.record(v.ob, rev, h, v.src)
                cur_h, cur_src, cur_op = h, v.src, "put"
        if cur_op != "put" or cur_h != rep_h or cur_src != rep.src:
            rev += 1
            orun.record(rep.ob, rev, rep_h, rep.src)
        kd = kinds.setdefault(rep.src, {})
        kd.setdefault(rep.ob.kind, []).append(key + "\t" + rep_h)
        if rep.ob.kind in day_kinds:
            dd = days.setdefault(rep.src, Counter())
            dd[canon.kst_day(rep.ob.ts_ms)] += 1
    # keys that were there before and are now observed nowhere (Q3)
    gone_containers = set()
    denylist = orun.ctx.denylist
    for key, hd in heads.items():
        if hd.op != "put" or key in rd.keys or (origin, key) in denylist:
            continue
        f = rd.containers.get(hd.src)
        if f is None:
            gone_containers.add(hd.src)
        elif f["status"] == "ok":
            orun.vanished += 1
            orun.notes["vanished"] += 1
    if gone_containers:
        orun.notes["room_gone"] += len(gone_containers)
    for sid, base in sorted(rd.containers.items()):
        f = dict(base)
        kd = kinds.get(sid)
        if kd:
            f["kinds"] = {k: {"n": len(v), "digest": canon.facts_digest(v)} for k, v in sorted(kd.items())}
        if days.get(sid):
            f["days"] = dict(sorted(days[sid].items()))
        if rd.versions.get(sid):
            f["versions"] = rd.versions[sid]
        orun.containers[sid] = f
    engine.finish_origin(orun)
