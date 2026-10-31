# -*- coding: utf-8 -*-
"""Change determination (SPEC 8, Q3, Q10) through the Origin hooks, on the toy app: a plain origin (vanished ->
suspect), a two-phase origin (PendingAbsentMixin: pending, absent, the shrink guard, partial containers, unprintable
keys) and a multi-version origin (MultiVersionMixin: versions, moves, missing and gone containers, unstable passes);
container failures and their statuses, read errors, denylist, value_bad / dup_key / bad_line, container facts,
state tables and replay, spill and part boundaries, determinism, the disk estimate and SYSTEM-only origins."""
import contextlib
import io
import shutil
import sqlite3
from collections import Counter
from unittest import mock

from rawlayer.capture import canon, ledger as ledger_mod, run
from rawlayer.capture.config import ConfigError, InputSet
from rawlayer.capture.origins import Guard
from tests.core import toyfix as T
from tests.core.toyapp import readers
from tests.core.toyapp.origins import ToyB
from tests.core.toyfix import by_key, capture, export, invariants, item, manifests, records, replay_diff


def key(origin, ident):
    return f"toy:{origin}:{ident}"


def sid(rel):
    return canon.source_id(rel)


class Case(T.ToyCase):
    def ops(self, k):
        return [(r["rev"], r["op"]) for r in by_key(records(self.fx)).get(k, [])]

    def sound(self):
        self.assertEqual(invariants(self.fx)[0], {})
        self.assertEqual(replay_diff(self.fx), 0)

    def state_rows(self, sql, *params):
        con = sqlite3.connect(T.ledger(self.fx).state_dir / "state.sqlite")
        try:
            return con.execute(sql, params).fetchall()
        finally:
            con.close()


class TestPlainOrigin(Case):
    def test_genesis_facts_rerun_and_state_table(self):
        self.assertEqual(capture(self.fx), 0)
        a = self.origin_doc("toy_a")
        self.assertEqual((a["status"], a["counts"], a["notes"], a["pending"]),
                         ("ok", {"put": 3, "absent": 0}, {}, {"add": [], "drop": []}))
        f1 = a["containers"][sid("a/c1.jsonl")]
        self.assertEqual((f1["status"], f1["kinds"]["item"]["n"], sum(f1["days"].values())), ("ok", 2, 2))
        recs = {r["key"]: r for r in records(self.fx)}
        r1 = recs[key("toy_a", 1)]
        self.assertEqual((r1["kind"], r1["rev"], r1["op"], r1["src"], r1["seq"], r1["native"]),
                         ("item", 1, "put", sid("a/c1.jsonl"), 1, {"id": "1", "v": "회의는 10월에 합니다"}))
        self.assertEqual(self.state_rows("SELECT count(*) FROM toy_last"), [(3,)])
        self.sound()
        self.assertEqual(capture(self.fx), 0)                       # nothing changed: a manifest without segments
        self.assertEqual(manifests(self.fx)[1]["segments"], [])
        self.sound()

    def test_change_is_the_next_rev(self):
        self.assertEqual(capture(self.fx), 0)
        self.fx.a_put("c1", [item(1, "회의는 10월에 합니다"), item(2, "changed")])
        self.assertEqual(capture(self.fx), 0)
        self.assertEqual(self.ops(key("toy_a", 2)), [(1, "put"), (2, "put")])
        h = by_key(records(self.fx))[key("toy_a", 2)][-1]["h"]
        self.assertEqual(self.state_rows("SELECT h, seq FROM toy_last WHERE key=?", key("toy_a", 2)), [(h, 2)])
        self.sound()

    def test_vanished_is_suspect_and_not_committed(self):
        self.assertEqual(capture(self.fx), 0)
        self.fx.a_put("c1", [item(2, "changed")])                   # 1 gone, 2 changed
        self.fx.m_put("c2", [item(3, "잡담 메시지 2")])
        self.assertEqual(capture(self.fx), 3)
        m = manifests(self.fx)[1]
        a = m["origins"]["toy_a"]
        self.assertEqual((a["status"], a["notes"]["vanished"], a["counts"]["put"]), ("suspect", 1, 0))
        self.assertEqual({s["origin"] for s in m["segments"]}, {"toy_m"})
        bad, nums = invariants(self.fx)
        self.assertEqual(bad, {})                                   # Q2: facts skip the suspect origin
        self.assertEqual((nums["containers_checked"], nums["containers_skipped"]), (4, 2))
        self.assertEqual(replay_diff(self.fx), 0)

    def test_container_failures(self):
        """A container that cannot be read keeps the status its origin gives it (fail_status), rolls its puts
        back (out of the manifest count too) and makes the run 'skipped' (exit 75) for a plain origin."""
        self.fx.a_put("c3", [item(7, "x"), {"fail": "io"}])
        self.fx.a_put("c4", [item(8, "y"), {"fail": "unreadable", "note": "bad_line"}])
        self.assertEqual(capture(self.fx), 75)
        a = self.origin_doc("toy_a")
        self.assertEqual((a["containers"][sid("a/c3.jsonl")], a["containers"][sid("a/c4.jsonl")]),
                         ({"status": "error"}, {"status": "error"}))
        self.assertEqual((a["status"], a["notes"]), ("ok", {"bad_line": 1, "read_error": 1}))
        self.assertEqual(a["counts"]["put"], sum(1 for r in records(self.fx) if r["origin"] == "toy_a"))
        self.assertNotIn(key("toy_a", 7), by_key(records(self.fx)))
        self.sound()

    def test_read_error_is_an_origin_error(self):
        shutil.rmtree(self.fx.a)
        out = io.StringIO()
        with T.preserved_env(), contextlib.redirect_stdout(out):
            self.assertEqual(run.capture(self.fx.cfg(), "live", list(T.ORIGINS), skip_precheck=True), 75)
        a = self.origin_doc("toy_a")
        self.assertEqual((a["status"], a["notes"], a["containers"]), ("error", {"read_error": 1}, {}))
        self.assertIn("origin error toy_a errno=2", out.getvalue())
        self.assertTrue(any(s["origin"] == "toy_b" for s in manifests(self.fx)[0]["segments"]))

    def test_value_bad_dup_key_and_bad_line(self):
        self.fx.a_put("c1", ['{"id": "1", "v": NaN}', item(2, "ok"), "not json", "[1]"])
        self.fx.a_put("c2", [item(3, "x"), item(3, "x")])
        self.assertEqual(capture(self.fx, ["toy_a"]), 75)
        a = self.origin_doc("toy_a")
        self.assertEqual(a["notes"], {"bad_line": 2, "dup_key": 1, "value_bad": 1})
        self.assertEqual(a["containers"][sid("a/c2.jsonl")], {"status": "error"})
        self.assertEqual(sorted(by_key(records(self.fx))), [key("toy_a", 2)])

    def test_dup_key_across_containers_of_one_run(self):
        """A key that two containers would record in one run is recorded once: the later container fails dup_key,
        and the ledger stays replayable (two records at one rev would wedge every later run)."""
        self.fx.a_put("c1", [item(1, "x"), item(2, "ok")])
        self.fx.a_put("c2", [item(1, "y")])
        self.assertEqual(capture(self.fx, ["toy_a"]), 75)
        a = self.origin_doc("toy_a")
        self.assertEqual((a["notes"], a["containers"][sid("a/c2.jsonl")]), ({"dup_key": 1}, {"status": "error"}))
        self.assertEqual([r["rev"] for r in by_key(records(self.fx))[key("toy_a", 1)]], [1])
        self.sound()
        self.assertEqual(capture(self.fx, ["toy_a"]), 0)           # the next run goes on: c1 is unchanged
        self.assertEqual([r["rev"] for r in by_key(records(self.fx))[key("toy_a", 1)]], [1, 2])
        self.assertEqual(replay_diff(self.fx), 0)

    def test_denylist(self):
        lg = T.ledger(self.fx)
        lg.ensure_dirs()
        deny = [{"origin": "toy_a", "key": key("toy_a", 2)}, {"origin": "toy_m", "key": key("toy_m", 2)}]
        T.write_json(lg.denylist_path, {"keys": deny})
        self.assertEqual(capture(self.fx), 0)
        keys = set(by_key(records(self.fx)))
        self.assertTrue({key("toy_a", 1), key("toy_m", 1)} <= keys)
        self.assertFalse({key("toy_a", 2), key("toy_m", 2)} & keys)
        self.assertEqual((self.origin_doc("toy_a")["notes"], self.origin_doc("toy_m")["notes"]),
                         ({"denied": 1}, {"denied": 1}))
        self.sound()

    def test_denylisted_key_that_disappears_is_not_vanished(self):
        self.assertEqual(capture(self.fx), 0)
        lg = T.ledger(self.fx)
        T.write_json(lg.denylist_path, {"keys": [{"origin": "toy_a", "key": key("toy_a", 2)},
                                                 {"origin": "toy_m", "key": key("toy_m", 2)}]})
        self.fx.a_put("c1", [item(1, "회의는 10월에 합니다")])
        self.fx.m_put("c1", [item(1, "rapid 배포 준비")])
        self.assertEqual(capture(self.fx), 0)
        self.assertEqual((self.origin_doc("toy_a")["status"], self.origin_doc("toy_m")["status"]), ("ok", "ok"))

    def test_spill_and_part_boundaries(self):
        self.assertEqual(capture(self.fx), 0)
        ref = export(self.fx)
        self.fx.write_config(self.fx.base / "data2")
        old = ledger_mod.SPILL_LIMIT, ledger_mod.PART_LIMIT
        ledger_mod.SPILL_LIMIT, ledger_mod.PART_LIMIT = 1, 600
        try:
            self.assertEqual(capture(self.fx), 0)
        finally:
            ledger_mod.SPILL_LIMIT, ledger_mod.PART_LIMIT = old
        self.assertEqual(export(self.fx), ref)
        segs = manifests(self.fx)[0]["segments"]
        self.assertGreater(len([s for s in segs if s["origin"] == "toy_b"]), 1)
        self.sound()

    def test_determinism_two_roots(self):
        self.assertEqual(capture(self.fx), 0)
        a = export(self.fx)
        self.fx.write_config(self.fx.base / "data2")
        self.assertEqual(capture(self.fx), 0)
        self.assertEqual(export(self.fx), a)
        self.assertEqual(len(a), len({(o, k) for o, k, *_ in a}))

    def test_estimate_peak(self):
        inputs = InputSet(self.fx.cfg(), "live")
        size = sum(p.stat().st_size for p in self.fx.a.iterdir())
        self.assertEqual(run.estimate_peak(inputs, ["toy_a", "toy_b"]), size + 1024 ** 3)
        self.assertEqual(run.estimate_peak(inputs, ["toy_m"]), 1024 ** 3)

    def test_disk_floor_skips_the_run(self):
        usage = shutil.disk_usage(self.fx.data_root)._replace(free=run.HARD_FLOOR)
        with mock.patch.object(run.shutil, "disk_usage", return_value=usage):
            self.assertEqual(capture(self.fx), 75)
        self.assertEqual(manifests(self.fx), [])

    def test_system_only_origin(self):
        self.fx.set_cfg(toy_system=True)
        with self.assertRaises(ConfigError):
            capture(self.fx, ["toy_a"])
        self.assertEqual(capture(self.fx, ["toy_b"]), 0)
        with mock.patch.object(run, "is_system", return_value=True):
            self.assertEqual(capture(self.fx, ["toy_a"]), 0)
        self.assertEqual(len(manifests(self.fx)), 2)

    def test_partial_run_leaves_the_other_origins_out_of_its_manifest(self):
        """Q5: a run of some origins writes no entry for the others; each origin's newest manifest is its own."""
        self.assertEqual(capture(self.fx, ["toy_a"]), 0)
        self.assertEqual(capture(self.fx, ["toy_m", "toy_b"]), 0)
        mans = manifests(self.fx)
        self.assertEqual([sorted(m["origins"]) for m in mans], [["toy_a"], ["toy_b", "toy_m"]])
        self.assertEqual([s["origin"] for s in mans[1]["segments"]], ["toy_b", "toy_m"])       # ledger order
        bad, nums = invariants(self.fx)
        self.assertEqual(bad, {})
        per_origin = {o: len(d["containers"]) for m in mans for o, d in m["origins"].items()}   # each its newest
        self.assertTrue(all(per_origin.values()))
        self.assertEqual(nums["containers_checked"], sum(per_origin.values()))
        self.assertEqual(replay_diff(self.fx), 0)


class TestPendingAbsent(Case):
    def b_doc(self, n=-1):
        return self.origin_doc("toy_b", n)

    def test_absent_is_two_phase(self):
        self.assertEqual(capture(self.fx), 0)
        k5 = key("toy_b", 5)
        self.fx.b_put("r1", [item(i, f"b{i}") for i in range(1, 21) if i != 5])
        self.assertEqual(capture(self.fx), 0)
        self.assertEqual((self.b_doc()["pending"], self.b_doc()["counts"]["absent"]), ({"add": [k5], "drop": []}, 0))
        self.assertEqual(self.state_rows("SELECT pending FROM heads WHERE origin='toy_b' AND key=?", k5), [(2,)])
        self.assertEqual(capture(self.fx), 0)
        recs = by_key(records(self.fx))[k5]
        self.assertEqual([(r["rev"], r["op"], r["seq"]) for r in recs], [(1, "put", 1), (2, "absent", 3)])
        self.assertEqual((recs[1]["src"], recs[1]["ts_ms"], recs[1]["kind"]), (sid("b/r1.jsonl"), None, "item"))
        self.assertEqual(recs[1]["src_asof_s"], int((self.fx.b / "r1.jsonl").stat().st_mtime))
        self.assertEqual(self.b_doc()["pending"], {"add": [], "drop": []})
        self.sound()
        self.assertEqual(capture(self.fx), 0)                       # absent stays absent: nothing new
        self.assertEqual(manifests(self.fx)[-1]["segments"], [])

    def test_seen_again_drops_the_pending_key(self):
        self.assertEqual(capture(self.fx), 0)
        self.fx.b_put("r1", [item(i, f"b{i}") for i in range(1, 21) if i != 5])
        self.assertEqual(capture(self.fx), 0)
        self.fx.b_put("r1", [item(i, f"b{i}") for i in range(1, 21)])
        self.assertEqual(capture(self.fx), 0)
        self.assertEqual(self.b_doc()["pending"], {"add": [], "drop": [key("toy_b", 5)]})
        self.assertEqual(self.ops(key("toy_b", 5)), [(1, "put")])
        self.sound()

    def test_same_h_after_absent_is_a_flap(self):
        self.assertEqual(capture(self.fx), 0)
        self.fx.b_put("r1", [item(i, f"b{i}") for i in range(1, 21) if i != 5])
        self.assertEqual(capture(self.fx), 0)
        self.assertEqual(capture(self.fx), 0)
        self.fx.b_put("r1", [item(i, f"b{i}") for i in range(1, 21)])
        self.assertEqual(capture(self.fx), 0)
        self.assertEqual(self.ops(key("toy_b", 5)), [(1, "put"), (2, "absent"), (3, "put")])
        self.assertEqual(invariants(self.fx)[0], {"flap": 1})      # the ledger policy sees the regeneration

    def test_partial_container_keeps_pending(self):
        self.assertEqual(capture(self.fx), 0)
        k5 = key("toy_b", 5)
        self.fx.b_put("r1", [item(i, f"b{i}") for i in range(1, 21) if i != 5])
        self.assertEqual(capture(self.fx), 0)
        self.assertEqual(self.b_doc()["pending"]["add"], [k5])
        self.fx.b_partial("r1")                                     # still missing, but not read completely
        self.assertEqual(capture(self.fx), 0)
        d = self.b_doc()
        self.assertEqual((d["containers"][sid("b/r1.jsonl")]["status"], d["pending"], d["counts"]["absent"]),
                         ("partial", {"add": [], "drop": []}, 0))
        self.fx.b_put("r1", [item(i, f"b{i}") for i in range(1, 21)], partial=True)     # seen again, partial
        self.assertEqual(capture(self.fx), 0)
        self.assertEqual(self.b_doc()["pending"], {"add": [], "drop": []})
        self.fx.b_partial("r1", False)
        self.assertEqual(capture(self.fx), 0)
        self.assertEqual(self.b_doc()["pending"], {"add": [], "drop": [k5]})
        self.assertEqual(self.ops(k5), [(1, "put")])
        self.sound()

    def test_partial_container_puts_but_adds_no_pending(self):
        self.assertEqual(capture(self.fx), 0)
        self.fx.b_put("r1", [item(i, f"b{i}") for i in range(1, 19)] + [item(40, "new")], partial=True)
        self.assertEqual(capture(self.fx), 0)
        d = self.b_doc()
        self.assertEqual((d["status"], d["pending"], d["counts"]), ("ok", {"add": [], "drop": []},
                                                                   {"put": 1, "absent": 0}))
        self.sound()

    def test_shrink_guard_on_new_missing_keys(self):
        """max(max_new_abs, max_new_frac * held) new missing keys pass; one more trips the guard."""
        self.assertEqual(capture(self.fx), 0)
        with mock.patch.object(ToyB, "guard", Guard(0.0, 3, 0.01)):
            self.fx.b_put("r1", [item(i, f"b{i}") for i in range(4, 21)])       # 3 new missing keys: pass
            self.assertEqual(capture(self.fx), 0)
            self.assertEqual(len(self.b_doc()["pending"]["add"]), 3)
            self.fx.b_put("r1", [item(i, f"b{i}") for i in range(4, 21)])
            self.fx.b_put("r2", [item(21, "changed")] + [item(i, f"b{i}") for i in range(22, 27)])  # 4 more
            self.assertEqual(capture(self.fx), 3)
        d = self.b_doc()
        self.assertEqual((d["status"], d["notes"], d["pending"], d["counts"]),
                         ("suspect", {"shrink_guard": 1}, {"add": [], "drop": []}, {"put": 1, "absent": 0}))
        self.assertEqual(self.ops(key("toy_b", 21)), [(1, "put"), (2, "put")])  # puts still commit (Q10 6-7)
        self.assertEqual(self.ops(key("toy_b", 1)), [(1, "put")])                # no absent in a tripped run
        self.sound()

    def test_shrink_guard_on_too_few_observed(self):
        self.assertEqual(capture(self.fx), 0)
        with mock.patch.object(ToyB, "guard", Guard(0.9, 100, 0.01)):
            self.fx.b_put("r1", [item(i, f"b{i}") for i in range(4, 21)])       # 27 of 30 observed: pass
            self.assertEqual(capture(self.fx), 0)
            self.fx.b_put("r1", [item(i, f"b{i}") for i in range(1, 21)])
            self.fx.b_put("r2", [item(i, f"b{i}") for i in range(21, 27)])      # 26 of 30
            self.assertEqual(capture(self.fx), 3)
        self.assertEqual((self.b_doc()["status"], self.b_doc()["notes"]), ("suspect", {"shrink_guard": 1}))
        self.sound()

    def test_guard_still_drops(self):
        self.assertEqual(capture(self.fx), 0)
        self.fx.b_put("r1", [item(i, f"b{i}") for i in range(1, 20)])
        self.assertEqual(capture(self.fx), 0)
        self.assertEqual(self.b_doc()["pending"]["add"], [key("toy_b", 20)])
        self.fx.b_put("r1", [item(i, f"b{i}") for i in range(1, 21)])           # back ...
        self.fx.b_put("r2", [item(i, f"b{i}") for i in range(21, 25)])          # ... while r2 trips the guard
        self.assertEqual(capture(self.fx), 3)
        d = self.b_doc()
        self.assertEqual((d["status"], d["notes"].get("shrink_guard"), d["counts"]["absent"]), ("suspect", 1, 0))
        self.assertEqual(d["pending"], {"add": [], "drop": [key("toy_b", 20)]})
        self.sound()

    def test_guard_at_genesis(self):
        self.fx.b_partial("r1")
        self.fx.b_partial("r2")
        self.assertEqual(capture(self.fx, ["toy_b"]), 3)            # containers, none read completely
        self.assertEqual((self.b_doc()["status"], self.b_doc()["notes"]), ("suspect", {"shrink_guard": 1}))

    def test_no_container_at_genesis_is_ok(self):
        for n in ("r1", "r2"):
            self.fx.remove(self.fx.b, n)
            self.fx.b_partial(n, False)
        self.assertEqual(capture(self.fx, ["toy_b"]), 0)
        self.assertEqual(self.b_doc()["status"], "ok")

    def test_no_complete_container_after_puts_is_suspect(self):
        self.assertEqual(capture(self.fx), 0)
        for n in ("r1", "r2"):
            self.fx.remove(self.fx.b, n)
        self.assertEqual(capture(self.fx, ["toy_b"]), 3)
        self.assertEqual((self.b_doc()["status"], self.b_doc()["containers"]), ("suspect", {}))

    def test_failed_containers_are_routine(self):
        """A container that cannot be read is routine here (no exit 75): it keeps its own failure status, or the
        origin's default for an I/O error, and the shrink guard judges the run."""
        self.assertEqual(capture(self.fx), 0)
        self.fx.b_put("r2", [item(21, "b21"), {"fail": "unreadable", "note": "bad_line"}])
        self.fx.b_put("r3", [{"fail": "io"}])
        self.assertEqual(capture(self.fx), 0)
        d = self.b_doc()
        self.assertEqual((d["status"], d["containers"][sid("b/r2.jsonl")], d["containers"][sid("b/r3.jsonl")]),
                         ("ok", {"status": "unreadable"}, {"status": "unreadable"}))
        self.assertEqual(d["notes"], {"bad_line": 1, "read_error": 1})
        self.assertEqual(d["pending"], {"add": [], "drop": []})     # r2 did not read completely
        self.sound()

    def test_key_unprintable(self):
        self.fx.b_put("r1", [item(i, f"b{i}") for i in range(1, 21)] + [item("Odd", "x")])
        self.assertEqual(capture(self.fx), 0)
        self.fx.b_put("r1", [item(i, f"b{i}") for i in range(1, 21)])
        self.assertEqual(capture(self.fx), 3)                       # no exception: manifest written, suspect
        d = self.b_doc()
        self.assertEqual((d["status"], d["notes"], d["pending"]["add"], d["counts"]["absent"]),
                         ("suspect", {"key_unprintable": 1}, [], 0))
        self.assertEqual(len(manifests(self.fx)), 2)


class TestMultiVersion(Case):
    def m_doc(self, n=-1):
        return self.origin_doc("toy_m", n)

    def setUp(self):
        super().setUp()
        self.addCleanup(setattr, readers, "between", None)

    def test_versions_in_order_of_first_sight(self):
        self.fx.m_put("c1", [item(1, "a"), item(1, "b"), item(2, "답글 하나")])
        self.assertEqual(capture(self.fx), 0)
        k1 = key("toy_m", 1)
        self.assertEqual([r["native"]["v"] for r in by_key(records(self.fx))[k1]], ["a", "b"])
        self.assertEqual(self.m_doc()["containers"][sid("m/c1.jsonl")]["versions"], 3)
        self.assertEqual(capture(self.fx), 0)                       # the same pass again: nothing
        self.assertEqual(manifests(self.fx)[-1]["segments"], [])
        self.fx.m_put("c1", [item(1, "a"), item(2, "답글 하나")])   # an old version becomes the head again
        self.assertEqual(capture(self.fx), 0)
        self.fx.m_put("c1", [item(1, "c"), item(1, "a"), item(2, "답글 하나")])
        self.assertEqual(capture(self.fx), 0)
        self.assertEqual([(r["rev"], r["native"]["v"]) for r in by_key(records(self.fx))[k1]],
                         [(1, "a"), (2, "b"), (3, "a"), (4, "c"), (5, "a")])
        self.assertEqual(len(self.state_rows("SELECT h FROM hist WHERE origin='toy_m' AND key=?", k1)), 3)
        self.sound()

    def test_move_is_one_put_not_vanished(self):
        self.assertEqual(capture(self.fx), 0)
        self.fx.m_put("c1", [item(1, "rapid 배포 준비"), item(2, "답글 하나"), item(3, "잡담 메시지")])
        self.fx.m_put("c2", [])
        self.assertEqual(capture(self.fx), 0)
        recs = by_key(records(self.fx))[key("toy_m", 3)]
        self.assertEqual([(r["rev"], r["src"]) for r in recs], [(1, sid("m/c2.jsonl")), (2, sid("m/c1.jsonl"))])
        self.assertEqual(recs[0]["h"], recs[1]["h"])
        self.assertEqual(self.m_doc()["notes"], {})
        self.sound()

    def test_vanished_is_suspect(self):
        self.assertEqual(capture(self.fx), 0)
        self.fx.m_put("c1", [item(1, "rapid 배포 준비")])
        self.fx.m_put("c2", [item(3, "잡담 메시지"), item(4, "new")])
        self.assertEqual(capture(self.fx), 3)
        d = self.m_doc()
        self.assertEqual((d["status"], d["notes"], d["counts"]), ("suspect", {"vanished": 1}, {"put": 0, "absent": 0}))
        self.assertEqual(manifests(self.fx)[-1]["segments"], [])

    def test_missing_and_gone_containers_are_not_vanished(self):
        self.assertEqual(capture(self.fx), 0)
        self.fx.remove(self.fx.m, "c2")                             # listed, no file: missing
        self.assertEqual(capture(self.fx), 0)
        d = self.m_doc()
        self.assertEqual((d["status"], d["containers"][sid("m/c2.jsonl")], d["notes"]),
                         ("ok", {"status": "missing"}, {}))
        self.fx.m_list(["c1"])                                      # not listed any more: gone
        self.assertEqual(capture(self.fx), 0)
        self.assertEqual((self.m_doc()["status"], self.m_doc()["notes"]), ("ok", {"room_gone": 1}))
        self.sound()

    def test_unstable_pass_is_repeated(self):
        flips = {"n": 0, "total": 0}

        def flip(root, limit):
            if flips["n"] < limit:
                flips["n"] += 1
                flips["total"] += 1                 # a new STAMP every time: the source moved under the pass
                self.fx.m_stamp(str(flips["total"]))
        readers.between = lambda root: flip(root, 1)
        self.assertEqual(capture(self.fx), 0)                       # the second pass is stable
        self.assertEqual((self.m_doc()["status"], self.m_doc()["counts"]["put"]), ("ok", 3))
        flips["n"] = 0
        readers.between = lambda root: flip(root, 10)
        self.fx.m_put("c2", [item(3, "잡담 메시지 2")])
        self.assertEqual(capture(self.fx), 75)                      # three moving passes: unstable, skipped
        d = self.m_doc()
        self.assertEqual((d["status"], d["notes"], d["counts"]["put"], flips["n"]),
                         ("unstable", {"unstable": 1}, 0, 3))
        self.assertTrue(all(s["origin"] != "toy_m" for s in manifests(self.fx)[-1]["segments"]))
        self.sound()

    def test_read_errors(self):
        (self.fx.m / "list.json").write_text("{not json", encoding="utf-8")
        self.assertEqual(capture(self.fx), 75)
        self.assertEqual((self.m_doc()["status"], self.m_doc()["notes"]), ("error", {"read_error": 1}))
        self.fx.m_list(["c1", "c2"])
        self.fx.m_put("c2", [{"fail": "io"}])
        self.assertEqual(capture(self.fx), 75)
        self.assertEqual((self.m_doc()["status"], self.m_doc()["notes"]), ("error", {"read_error": 1}))
        self.assertEqual(sum(1 for r in records(self.fx) if r["origin"] == "toy_m"), 0)

    def test_value_bad_and_bad_line(self):
        self.fx.m_put("c1", ['{"id": "1", "v": NaN}', "not json", item(2, "답글 하나")])
        self.assertEqual(capture(self.fx), 0)
        self.assertEqual(self.m_doc()["notes"], {"bad_line": 1, "value_bad": 1})
        self.assertEqual(sorted(k for k in by_key(records(self.fx)) if k.startswith("toy:toy_m")),
                         [key("toy_m", 2), key("toy_m", 3)])

    def test_days_facts(self):
        self.assertEqual(capture(self.fx), 0)
        f = self.m_doc()["containers"][sid("m/c1.jsonl")]
        self.assertEqual(f["days"], dict(sorted(Counter(canon.kst_day(T.T0 + i * 3_600_000) for i in (1, 2)).items())))
        self.assertEqual(f["kinds"]["rec"]["n"], 2)
        self.sound()
