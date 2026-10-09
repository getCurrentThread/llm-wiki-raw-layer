# -*- coding: utf-8 -*-
"""Ledger invariants for the tests, read from the ledger files alone (no state.sqlite).

The verification subsystem (verify / doctor) is gone from the product, but the properties of the ledger that
capture itself must keep are still asserted by the tests. This module re-derives them from the segments and
manifests with the rules the removed checks used:

  seal    (old V5)  manifest names and seq run 1..n, prev_sha256 chain, every segment present, read-only, with the
                    manifest's size / sha256 / record count, no .part or inflight.json left, no unreferenced segment
  policy  (old C10) 15 fields, rev contiguous from 1, record seq = manifest seq, one kind per key and one of the
                    origin's kinds, h right, consecutive puts differ (a
                    multi-version origin: (h, src)), no flap, absent only in a two-phase origin (PendingAbsentMixin)
                    from a completely read container of a non-suspect run for a key pending since an earlier run,
                    pending add/drop consistent (Q10)
  facts   (old C2)  each origin's newest manifest: container kinds (n, digest) and days (the origin's day_kinds)
                    equal the fold's heads (pending.add keys of that run excluded; suspect origins and containers not
                    read completely skipped, Q2/Q5)

The rules of each origin come from the installed Origin (rawlayer.registry), so the same checks serve every app.
check(raw_dir) returns ({category: count} for every non-zero category, {"containers_checked": n,
"containers_skipped": n}); a sound ledger gives ({}, ...). Everything is read into memory: test ledgers are small.
"""
import gzip
import hashlib
import json
import os
import re
import stat
import zlib
from collections import Counter, defaultdict
from pathlib import Path

from rawlayer import registry
from rawlayer.capture import canon
from rawlayer.capture.origins import MultiVersionMixin, Origin, PendingAbsentMixin

FIELDS = frozenset(("v", "kind", "origin", "key", "rev", "op", "run", "seq", "src", "observed_at_s",
                    "src_asof_s", "ts_ms", "native", "derived", "h"))
OPS = ("put", "absent", "purged")
_UNKNOWN = Origin()                     # an origin no installed app declares: no kinds, no absent


def _spec(origin):
    return registry.origin(origin) or _UNKNOWN
_MAN = re.compile(r"^([0-9]{8})-([0-9]{13}-[0-9a-f]{4})\.json$")


def _manifests(raw, bad):
    """[(seq, run, path, manifest)] of the readable manifests, in seq order; unreadable ones counted in bad."""
    out = []
    d = Path(raw) / "manifests"
    for p in sorted(d.iterdir(), key=lambda p: p.name) if d.is_dir() else ():
        m = _MAN.fullmatch(p.name)
        if not m:
            continue
        try:
            man = json.loads(p.read_bytes())
        except (OSError, ValueError):
            bad["manifest_unreadable"] += 1
            continue
        out.append((int(m.group(1)), m.group(2), p, man))
    seqs = [s for s, _r, _p, _m in out]
    bad["seq_gap"] += len(set(range(1, max(seqs, default=0) + 1)) - set(seqs)) + len(seqs) - len(set(seqs))
    return out


def _segment_records(path, bad):
    try:
        with gzip.open(path, "rb") as fh:
            return [json.loads(ln) for ln in fh]
    except (OSError, ValueError, EOFError, zlib.error):     # a flipped byte may break the deflate stream itself
        bad["unreadable_segment"] += 1
        return None


def seal(raw, mans, bad, records_by_seg):
    raw = Path(raw)
    prev = None
    referenced = set()          # case-folded absolute strings: a manifest path holding '..' still names its file
    for seq, run, path, m in mans:
        if m.get("seq") != seq or m.get("run") != run:
            bad["manifest_name"] += 1
        if m.get("prev_sha256") != (hashlib.sha256(prev).hexdigest() if prev is not None else None):
            bad["prev_chain"] += 1
        for s in m.get("segments", []):
            p = raw.joinpath(*s["path"].split("/"))
            referenced.add(os.path.normcase(os.path.abspath(p)))
            if not p.exists():
                bad["segment_missing"] += 1
                continue
            st = p.stat()
            if st.st_mode & stat.S_IWRITE:
                bad["writable"] += 1
            if st.st_size != s.get("bytes"):
                bad["size_mismatch"] += 1
            if hashlib.sha256(p.read_bytes()).hexdigest() != s.get("sha256"):
                bad["sha_mismatch"] += 1
            recs = records_by_seg.get(p)
            if recs is not None and len(recs) != s.get("records"):
                bad["records_mismatch"] += 1
        prev = Path(path).read_bytes()
    # os.walk, not Path.walk: Path.walk enters no junction and files a link to a directory as a file (as in
    # Ledger.part_files, whose leftovers this must agree with)
    for dp, _d, fs in os.walk(raw):
        dp = Path(dp)
        for f in fs:
            full = dp / f
            if f.endswith(".part") or (f == "inflight.json" and dp.name == ".state"):
                bad["leftover"] += 1
            elif f.endswith(".jsonl.gz") and os.path.normcase(os.path.abspath(full)) not in referenced:
                bad["unreferenced"] += 1


def pending_fold_violations(mans, absent_by_seq, origin):
    """Q10 item 9: P_s = (P_{s-1} | add_s) - drop_s - absent_s over the origin's pending lists of the manifests (seq
    order) and its absent keys of each seq. Each absent key must be in P_{s-1}; add_s must miss P_{s-1}, drop_s
    must lie in it, add/drop/absent are disjoint, add/drop sorted without duplicates. Returns [(category, key)]."""
    out = []
    pend = set()
    for seq, _r, _p, m in mans:
        p = ((m.get("origins") or {}).get(origin) or {}).get("pending") or {}
        add, drop = list(p.get("add") or []), list(p.get("drop") or [])
        absent = absent_by_seq.get(seq, set())
        for lst in (add, drop):
            if lst != sorted(set(lst)):
                out.append(("pending_bad", lst[0]))
        add_s, drop_s = set(add), set(drop)
        out.extend(("absent_not_allowed", k) for k in sorted(absent - pend))
        out.extend(("pending_bad", k) for k in sorted((add_s & pend) | (drop_s - pend) | (add_s & drop_s)
                                                       | (add_s & absent) | (drop_s & absent)))
        pend = (pend | add_s) - drop_s - absent
    return out


def _absent_allowed(by_seq, spec, seq, rec):
    if not isinstance(spec, PendingAbsentMixin):
        return False
    o = ((by_seq.get(seq) or {}).get("origins") or {}).get(spec.name) or {}
    if o.get("status") == "suspect":
        return False
    return ((o.get("containers") or {}).get(rec.get("src")) or {}).get("status") == spec.complete_status


def policy(chains, mans, bad):
    by_seq = {s: m for s, _r, _p, m in mans}
    absent = defaultdict(lambda: defaultdict(set))      # two-phase origin -> seq -> absent keys
    for (origin, key), items in chains.items():
        spec = _spec(origin)
        moves = isinstance(spec, MultiVersionMixin)     # the same h under another container is a new put (a move)
        if [r for r, _s, _x in items] != list(range(1, len(items) + 1)):
            bad["rev_gap"] += 1
        kind0 = items[0][2].get("kind")
        prev = None
        h_before_absent = None
        for _rev, seq, rec in items:
            op, kind = rec.get("op"), rec.get("kind")
            if set(rec) != FIELDS or rec["v"] != 1 or rec["origin"] != origin or rec["key"] != key:
                bad["shape"] += 1
                prev = rec
                continue
            if rec["seq"] != seq:
                bad["seq_mismatch"] += 1
            if kind not in spec.kinds or kind != kind0:
                bad["kind_bad"] += 1
            if op == "put":
                try:
                    h_ok = rec["h"] == canon.record_h(rec["native"], rec["derived"])
                except (ValueError, TypeError):
                    h_ok = False
                if not h_ok:
                    bad["h_bad"] += 1
                same = prev is not None and prev.get("op") == "put" and prev.get("h") == rec["h"]
                if same and (not moves or prev.get("src") == rec["src"]):
                    bad["put_same_h"] += 1
                if h_before_absent is not None and rec["h"] == h_before_absent:
                    bad["flap"] += 1
                h_before_absent = None
            elif op == "absent":
                if any(rec[f] is not None for f in ("native", "derived", "h")):
                    bad["shape"] += 1
                if isinstance(spec, PendingAbsentMixin):
                    absent[origin][seq].add(key)
                if not _absent_allowed(by_seq, spec, seq, rec):
                    bad["absent_not_allowed"] += 1
                if prev is not None and prev.get("op") == "put":
                    h_before_absent = prev.get("h")
            elif op == "purged":
                if any(rec[f] is not None for f in ("native", "derived", "h")):
                    bad["shape"] += 1
            else:
                bad["op_bad"] += 1
            prev = rec
    for spec in registry.origins():
        if isinstance(spec, PendingAbsentMixin):
            for cat, _key in pending_fold_violations(mans, absent[spec.name], spec.name):
                bad[cat] += 1


def facts(chains, mans, bad, nums):
    latest = {}
    for _s, _r, _p, m in mans:
        for o, doc in (m.get("origins") or {}).items():
            latest[o] = doc
    acc = {o: defaultdict(lambda: {"kinds": defaultdict(list), "days": Counter()}) for o in latest}
    for (origin, key), items in sorted(chains.items()):
        head = items[-1][2]
        if origin not in latest or head.get("op") != "put" or set(head) != FIELDS:
            continue
        if key in set((latest[origin].get("pending") or {}).get("add") or []):
            continue
        a = acc[origin][head["src"]]
        a["kinds"][head["kind"]].append(key + "\t" + head["h"])
        if head["kind"] in _spec(origin).day_kinds:
            a["days"][canon.kst_day(head["ts_ms"])] += 1
    for origin, doc in sorted(latest.items()):
        containers = doc.get("containers") or {}
        if doc.get("status") == "suspect":
            nums["containers_skipped"] += len(containers)
            continue
        for sid, f in sorted(containers.items()):
            if f.get("status") != _spec(origin).complete_status:
                nums["containers_skipped"] += 1
                continue
            nums["containers_checked"] += 1
            a = acc[origin].get(sid) or {"kinds": {}, "days": Counter()}
            got = {k: {"n": len(v), "digest": canon.facts_digest(v)} for k, v in a["kinds"].items() if v}
            if got != (f.get("kinds") or {}):
                bad["kinds_mismatch"] += 1
            if dict(a["days"]) != (f.get("days") or {}):
                bad["days_mismatch"] += 1


def check(raw):
    raw = Path(raw)
    bad, nums = Counter(), Counter()
    mans = _manifests(raw, bad)
    records_by_seg = {}
    chains = defaultdict(list)          # (origin, key) -> [(rev, seq, record)]
    for seq, _r, _p, m in mans:
        for s in m.get("segments", []):
            p = raw.joinpath(*s["path"].split("/"))
            if not p.exists():
                continue
            recs = _segment_records(p, bad)
            if recs is None:
                continue
            records_by_seg[p] = recs
            for rec in recs:
                if not isinstance(rec, dict) or not isinstance(rec.get("key"), str) \
                        or not isinstance(rec.get("rev"), int) or isinstance(rec.get("rev"), bool):
                    bad["shape"] += 1
                    continue
                chains[(s.get("origin"), rec["key"])].append((rec["rev"], seq, rec))
    for items in chains.values():
        items.sort(key=lambda t: (t[0], t[1]))
    seal(raw, mans, bad, records_by_seg)
    policy(chains, mans, bad)
    facts(chains, mans, bad, nums)
    nums.setdefault("containers_checked", 0)
    nums.setdefault("containers_skipped", 0)
    return {k: v for k, v in sorted(bad.items()) if v}, dict(nums)
