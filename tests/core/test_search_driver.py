# -*- coding: utf-8 -*-
"""The search index driver (rawlayer.search.common) on the toy index, built from a real three-round toy ledger:
reports, incremental catch-up == full rebuild, ledger reset / genesis redone / a non-capture manifest / a large
catch-up (rebuilds), mid-commit (stops, then resumes), defects (stop with exit 1, roll back), crashes and a killed
catch-up (hot journal), the version gate, the stale warning, status, the writer lock, several indexes in one project
(index --source all, one lock each, one failing does not stop the other), and the CLI's usage errors."""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import mock

from rawlayer.capture import fsio
from tests.core import toyfix as T
from tests.core.toyapp import search as toy
from tests.core.toyapp2 import search as toy2
from tests.core.toyfix import item, run_search

COLS = "key, origin, rev, seq, held, v, ng"


def ipath(data_root, name="toy"):
    return Path(data_root) / "index" / (name + ".sqlite")


def dump(data_root, name="toy"):
    """Everything the index derives from the ledger (row ids and build timestamps excluded)."""
    con = sqlite3.connect(fsio.ro_uri(ipath(data_root, name), immutable=False), uri=True)
    try:
        out = {"item": sorted(con.execute(f"SELECT {COLS} FROM item").fetchall())}
        for t in ("facet", "counts"):
            out[t] = sorted(con.execute(f"SELECT * FROM {t}").fetchall())
        meta = dict(con.execute("SELECT k, v FROM meta").fetchall())
        out["head"] = (meta["head_seq"], meta["head_sha256"], meta["head_run"])
        return out
    finally:
        con.close()


def fts_ok(data_root):
    con = sqlite3.connect(ipath(data_root))
    try:
        con.execute("INSERT INTO item_fts(item_fts, rank) VALUES('integrity-check', 1)")
        return True
    except sqlite3.DatabaseError:
        return False
    finally:
        con.close()


def search(cfg, *args, source="toy"):
    rc, out, err = run_search("search", source, "--config", cfg, "--json", *args)
    if rc != 0:
        raise AssertionError(f"rc={rc} err={err}")
    return json.loads(out)


def manifest_path(data_root, seq):
    d = Path(data_root) / "raw" / "manifests"
    names = [n for n in T.names(d) if n.startswith(f"{seq:08d}-") and n.endswith(".json")]
    assert len(names) == 1, names
    return d / names[0]


def segments(data_root, seq):
    with manifest_path(data_root, seq).open(encoding="ascii") as fh:
        man = json.load(fh)
    return [Path(data_root, "raw", *s["path"].split("/")) for s in man["segments"]]


class ToyRounds:
    """Round 1: the standard inputs plus a non-string value (a:4). Round 2: a:2 edited, m:1 gets a new version,
    b:5 goes missing (pending). Round 3: b:5 absent, m:3 moves to c1, a new container a/c3 (a:5)."""

    def __init__(self):
        self.fx = T.ToyFixture().standard()
        self.rounds = 0

    def next_round(self):
        self.rounds += 1
        fx, r = self.fx, self.rounds
        fx.a_put("c2", [item(3, "API 키 발급"), item(4, 7)])
        if r >= 2:
            fx.a_put("c1", [item(1, "회의는 10월에 합니다"), item(2, "가나 나다 라마")])
            fx.m_put("c1", [item(1, "rapid 배포 준비"), item(1, "배포 완료"), item(2, "답글 하나")])
            fx.b_put("r1", [item(i, f"b{i}") for i in range(1, 21) if i != 5])
        if r >= 3:
            fx.m_put("c1", [item(1, "rapid 배포 준비"), item(1, "배포 완료"), item(2, "답글 하나"),
                            item(3, "잡담 메시지")])
            fx.m_put("c2", [])
            fx.a_put("c3", [item(5, "새 메시지 10월에")])
        return T.capture(fx)


class Scenario(T.ToyCase):
    """One three-round ledger, indexed incrementally after each round; tests that change anything work on clones."""
    APPS = (T.TOY, T.TOY2)
    STANDARD = False

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.sc = ToyRounds()
        cls.addClassCleanup(T.force_rmtree, cls.sc.fx.base)
        cls.cfg, cls.dr = cls.sc.fx.config_path, cls.sc.fx.data_root
        cls.reports, cls.dumps = [], {}
        for rnd in (1, 2, 3):
            assert cls.sc.next_round() == 0
            rc, out, err = run_search("index", "--config", cls.cfg, "--source", "toy", "--json")
            assert rc == 0, err
            cls.reports.append(json.loads(out)["toy"])
            cls.dumps[rnd] = dump(cls.dr)
            if rnd == 2:
                cls.round2 = cls.clone()

    @classmethod
    def clone(cls, src=None):
        """A copy of a data root (ledger + index) with a config for it -> (config path, data root)."""
        base = Path(tempfile.mkdtemp(prefix="rsx-", dir=T.TEST_PARENT))
        cls.addClassCleanup(T.force_rmtree, base)
        dr = base / "data"
        shutil.copytree(src or cls.dr, dr)
        cfg = base / "config.json"
        T.write_json(cfg, {"v": 1, "data_root": dr})
        return cfg, dr

    def index(self, cfg, *extra, source="toy"):
        rc, out, err = run_search("index", "--config", cfg, "--source", source, "--json", *extra)
        rep = json.loads(out) if out.strip() else None
        return rc, (rep[source] if rep and source != "all" else rep), err

    def round2_index_with_ledger3(self):
        """A clone whose index stands at seq 2 while its ledger has seq 3."""
        cfg, dr = self.clone()
        m3 = manifest_path(dr, 3)
        kept = T.suffixed(m3, ".keep")
        m3.rename(kept)
        self.assertEqual(self.index(cfg)[1]["seq"], 2)
        kept.rename(m3)
        return cfg, dr


class TestBuild(Scenario):
    def test_reports(self):
        r1, r2, r3 = self.reports
        self.assertEqual((r1["action"], r1["reason"], r1["seq"], r1["ledger_seq"], r1["records"], r1["behind"]),
                         ("rebuilt", "missing", 1, 1, 37, False))
        self.assertEqual((r2["action"], r2["reason"], r2["seq"], r2["manifests"], r2["records"]),
                         ("updated", None, 2, 1, 2))
        self.assertEqual((r3["action"], r3["seq"], r3["records"], r3["rows"], r3["held"], r3["bad_value"]),
                         ("updated", 3, 3, 38, 37, 1))
        rc, rep, _ = self.index(self.cfg)
        self.assertEqual((rc, rep["action"], rep["manifests"]), (0, "current", 0))
        rc, out, _ = run_search("index", "--config", self.cfg, "--source", "toy")
        self.assertIn("toy: current", out)
        self.assertIn("seq=3", out)

    def test_fold(self):
        rows = {r[0]: r for r in self.dumps[3]["item"]}
        self.assertEqual(rows["toy:toy_a:2"][1:6], ("toy_a", 2, 2, 1, "가나 나다 라마"))
        self.assertEqual(rows["toy:toy_b:5"][1:6], ("toy_b", 2, 3, 0, "b5"))           # absent: kept, not held
        self.assertEqual(rows["toy:toy_m:1"][1:6], ("toy_m", 2, 2, 1, "배포 완료"))
        self.assertEqual(rows["toy:toy_m:3"][1:6], ("toy_m", 2, 3, 1, "잡담 메시지"))  # the move: a put
        self.assertEqual(rows["toy:toy_a:4"][5], "")
        self.assertEqual(self.dumps[3]["facet"], [("toy_a", 5), ("toy_b", 29), ("toy_m", 3)])

    def test_search(self):
        self.assertEqual(search(self.cfg, "10월")["keys"], ["toy:toy_a:1", "toy:toy_a:5"])
        self.assertEqual(search(self.cfg, "가나다")["total"], 0)          # '가나 나다' is not '가나다'
        self.assertEqual(search(self.cfg, "배포")["keys"], ["toy:toy_m:1"])
        self.assertEqual(search(self.cfg, "b5")["total"], 0)
        self.assertEqual(search(self.cfg, "b5", "--include-gone")["keys"], ["toy:toy_b:5"])
        rc, out, _ = run_search("search", "toy", "--config", self.cfg, "10월")
        self.assertEqual((rc, out.split()), (0, ["toy:toy_a:1", "toy:toy_a:5"]))

    def test_incremental_equals_rebuild(self):
        self.assertTrue(fts_ok(self.dr))
        cfg, dr = self.clone()
        rc, rep, _ = self.index(cfg, "--rebuild")
        self.assertEqual((rc, rep["action"], rep["reason"], rep["seq"], rep["manifests"], rep["records"]),
                         (0, "rebuilt", "requested", 3, 3, 42))
        self.assertEqual(dump(dr), self.dumps[3])
        self.assertTrue(fts_ok(dr))
        for q in ("10월", "배포", "b5", "메시지", "api"):
            self.assertEqual(search(cfg, q, "--include-gone"), search(self.cfg, q, "--include-gone"))

    def test_round2_incremental_equals_rebuild(self):
        cfg, dr = self.round2
        self.assertEqual(dump(dr), self.dumps[2])
        self.assertEqual(self.index(cfg, "--rebuild")[1]["seq"], 2)
        self.assertEqual(dump(dr), self.dumps[2])


class TestLedgerChanges(Scenario):
    def test_reset_shorter_ledger_rebuilds(self):
        cfg, dr = self.clone()
        manifest_path(dr, 3).unlink()
        rc, rep, _ = self.index(cfg)
        self.assertEqual((rc, rep["action"], rep["reason"], rep["seq"]), (0, "rebuilt", "ledger_reset", 2))
        self.assertEqual(dump(dr), self.dumps[2])

    def test_reset_same_length_rebuilds(self):
        cfg, dr = self.clone()
        with manifest_path(dr, 3).open("ab") as fh:
            fh.write(b"\n")
        rc, rep, _ = self.index(cfg)
        self.assertEqual((rc, rep["reason"], rep["seq"]), (0, "ledger_reset", 3))

    def test_genesis_redone_rebuilds(self):
        sc = ToyRounds()
        try:
            self.assertEqual(sc.next_round(), 0)
            self.assertEqual(self.index(sc.fx.config_path)[1]["reason"], "missing")
            T.force_rmtree(sc.fx.data_root / "raw")
            self.assertEqual(sc.next_round(), 0)                 # a new genesis from round-2 inputs
            rc, rep, _ = self.index(sc.fx.config_path)
            self.assertEqual((rc, rep["action"], rep["reason"], rep["seq"], rep["rows"]),
                             (0, "rebuilt", "ledger_reset", 1, 36))
            rows = {r[0]: r for r in dump(sc.fx.data_root)["item"]}
            self.assertEqual(rows["toy:toy_a:2"][2:6], (1, 1, 1, "가나 나다 라마"))
        finally:
            T.force_rmtree(sc.fx.base)

    def test_non_capture_manifest_rebuilds(self):
        cfg, dr = self.clone()
        with (dr / "raw" / "manifests" / "00000004-1700000000000-abcd.json").open("w", encoding="ascii") as fh:
            json.dump({"v": 1, "type": "purge", "seq": 4, "run": "1700000000000-abcd", "segments": []}, fh)
        rc, rep, _ = self.index(cfg)
        self.assertEqual((rc, rep["action"], rep["reason"], rep["seq"]), (0, "rebuilt", "manifest_type", 4))

    def test_large_catch_up_rebuilds(self):
        cfg, dr = self.round2_index_with_ledger3()
        with mock.patch.object(toy.SPEC, "full_rebuild_records", 1):
            rc, rep, _ = self.index(cfg)
        self.assertEqual((rc, rep["reason"], rep["seq"]), (0, "large_catch_up", 3))
        self.assertEqual(dump(dr), self.dumps[3])

    def test_mid_commit_stops_then_resumes(self):
        cfg, dr = self.round2_index_with_ledger3()
        seg = segments(dr, 3)[0]
        part = T.suffixed(seg, ".part")
        seg.chmod(0o600)
        seg.rename(part)
        rc, rep, _ = self.index(cfg)
        self.assertEqual((rc, rep["action"], rep["seq"], rep["blocked"], rep["blocked_seq"], rep["behind"]),
                         (0, "current", 2, "mid_commit", 3, True))
        rc, out, _ = run_search("index", "--config", cfg, "--source", "toy")
        self.assertIn("still being committed", out)
        part.rename(seg)
        rc, rep, _ = self.index(cfg)
        self.assertEqual((rc, rep["action"], rep["seq"], rep["blocked"]), (0, "updated", 3, None))
        self.assertEqual(dump(dr), self.dumps[3])

    def test_part_renamed_during_the_commit_check_is_not_a_defect(self):
        """capture renames *.part to the final name between the index's two existence checks. Whichever check sees
        the file, the result is mid_commit or ok -- never a 'missing' defect (exit 1)."""
        cfg, dr = self.round2_index_with_ledger3()
        seg = segments(dr, 3)[0]
        part = T.suffixed(seg, ".part")
        seg.chmod(0o600)
        seg.rename(part)
        real, real_lexists = os.path.exists, os.path.lexists

        def racing(p):
            hit = real(p)
            if os.path.normcase(p) in (os.path.normcase(seg), os.path.normcase(part)) and real(part):
                part.rename(seg)                    # capture finalises right after this check looked
            return hit

        def path_exists(p, *, follow_symlinks=True):
            return racing(p) if follow_symlinks else real_lexists(p)
        # the check may ask os.path.exists or Path.exists: both race
        with mock.patch.object(os.path, "exists", side_effect=racing), mock.patch.object(Path, "exists", path_exists):
            rc, rep, err = self.index(cfg)
        self.assertEqual(rc, 0, err)
        self.assertIn((rep["seq"], rep["blocked"]), ((2, "mid_commit"), (3, None)))
        rc, rep, _ = self.index(cfg)
        self.assertEqual((rc, rep["seq"], rep["blocked"]), (0, 3, None))
        self.assertEqual(dump(dr), self.dumps[3])

    def test_missing_segment_is_a_defect(self):
        cfg, dr = self.round2_index_with_ledger3()
        seg = segments(dr, 3)[0]
        seg.chmod(0o600)
        seg.unlink()
        rc, rep, _ = self.index(cfg)
        self.assertEqual((rc, rep["seq"], rep["blocked"]), (1, 2, "defect"))
        rc, out, _ = run_search("index", "--config", cfg, "--source", "toy")
        self.assertEqual(rc, 1)
        self.assertIn("STOPPED", out)

    def test_corrupt_segment_rolls_back(self):
        cfg, dr = self.round2_index_with_ledger3()
        seg = segments(dr, 3)[0]
        seg.chmod(0o600)
        with seg.open("r+b") as fh:
            fh.seek(30)
            fh.write(b"\xff" * 16)
        before = dump(dr)
        rc, rep, _ = self.index(cfg)
        self.assertEqual((rc, rep["seq"], rep["blocked"]), (1, 2, "defect"))
        self.assertEqual(dump(dr), before)
        rc, _rep, _err = self.index(cfg, "--rebuild")
        self.assertEqual(rc, 1)
        self.assertEqual(dump(dr), before)                    # the failed rebuild left the index alone
        self.assertFalse(T.suffixed(ipath(dr), ".new").exists())

    def test_crash_mid_catch_up_keeps_previous_manifest(self):
        cfg, dr = self.round2_index_with_ledger3()
        before = dump(dr)
        with mock.patch.object(toy.ToyApplier, "end_manifest", side_effect=RuntimeError("boom")):
            rc, _out, err = run_search("index", "--config", cfg, "--source", "toy")
        self.assertEqual(rc, 1)
        self.assertIn("RuntimeError", err)
        self.assertEqual(dump(dr), before)
        self.assertTrue(fts_ok(dr))
        self.assertEqual(self.index(cfg)[1]["seq"], 3)
        self.assertEqual(dump(dr), self.dumps[3])

    def test_killed_catch_up_leaves_hot_journal_then_recovers(self):
        cfg, dr = self.round2_index_with_ledger3()
        before = dump(dr)
        code = ("import os, sqlite3; c = sqlite3.connect(%r, isolation_level=None); c.execute('PRAGMA cache_size=1'); "
                "c.execute('BEGIN'); c.execute(\"UPDATE item SET v = v || 'x', ng = ng || ' zz'\"); "
                "c.execute(\"UPDATE meta SET v = '3' WHERE k = 'head_seq'\"); os._exit(1)") % str(ipath(dr))
        subprocess.run([sys.executable, "-s", "-B", "-c", code], cwd=T.REPO, check=False)
        self.assertTrue(T.suffixed(ipath(dr), "-journal").exists())
        rc, _out, err = run_search("search", "toy", "--config", cfg, "가나다")
        self.assertEqual(rc, 2)
        self.assertIn("manage.py collect", err)
        rc, rep, _ = self.index(cfg)                 # rolls the journal back, then catches up from seq 2
        self.assertEqual((rc, rep["action"], rep["seq"], rep["manifests"]), (0, "updated", 3, 1))
        self.assertFalse(T.suffixed(ipath(dr), "-journal").exists())
        self.assertNotEqual(dump(dr), before)
        self.assertEqual(dump(dr), self.dumps[3])
        self.assertTrue(fts_ok(dr))

    def test_crash_mid_rebuild_keeps_old_index(self):
        cfg, dr = self.clone()
        with mock.patch.object(toy.ToyApplier, "finish", side_effect=RuntimeError("boom")):
            rc, _out, _err = run_search("index", "--config", cfg, "--source", "toy", "--rebuild")
        self.assertEqual(rc, 1)
        self.assertFalse(T.suffixed(ipath(dr), ".new").exists())
        self.assertEqual(dump(dr), self.dumps[3])
        self.assertEqual(search(cfg, "배포")["total"], 1)

    def test_version_gate(self):
        for k, v in (("tokenizer", "ngram-1"), ("extractor_version", "toy-0"), ("schema_version", "1.toy-0")):
            with self.subTest(stamp=k):
                cfg, dr = self.clone()
                con = sqlite3.connect(ipath(dr))
                con.execute("UPDATE meta SET v=? WHERE k=?", (v, k))
                con.commit()
                con.close()
                rc, _out, err = run_search("search", "toy", "--config", cfg, "배포")
                self.assertEqual(rc, 2)
                self.assertIn("--rebuild", err)
                rc, out, _ = run_search("index", "--config", cfg, "--source", "toy", "--status", "--json")
                self.assertEqual(json.loads(out)["toy"]["state"], "outdated")
                rc, rep, _ = self.index(cfg)
                self.assertEqual((rc, rep["action"], rep["reason"]), (0, "rebuilt", "version"))
                self.assertEqual(search(cfg, "배포")["total"], 1)

    def test_stale_warning(self):
        cfg, _dr = self.round2_index_with_ledger3()
        rc, _out, err = run_search("search", "toy", "--config", cfg, "배포")
        self.assertEqual(rc, 0)
        self.assertIn("behind the ledger", err)
        env = search(cfg, "배포")
        self.assertEqual(env["index"], {"seq": 2, "ledger_seq": 3, "stale": True})
        self.assertEqual(search(cfg, "b5")["keys"], ["toy:toy_b:5"])   # only pending at seq 2: still held
        rc, _out, err = run_search("search", "toy", "--config", self.cfg, "배포")
        self.assertEqual(err, "")

    def test_status(self):
        rc, out, _ = run_search("index", "--config", self.cfg, "--source", "toy", "--status", "--json")
        st = json.loads(out)["toy"]
        self.assertEqual(rc, 0)
        self.assertEqual((st["state"], st["seq"], st["ledger_seq"], st["stale"], st["rows"], st["writer_busy"]),
                         ("ok", 3, 3, False, 38, False))
        rc, out, _ = run_search("index", "--config", self.cfg, "--source", "toy", "--status")
        self.assertIn("state=ok", out)
        cfg, dr = self.clone()
        ipath(dr).unlink()
        rc, out, _ = run_search("index", "--config", cfg, "--source", "toy", "--status", "--json")
        self.assertEqual((json.loads(out)["toy"]["state"], json.loads(out)["toy"]["stale"]), ("missing", True))
        self.assertFalse(ipath(dr).exists())                  # status writes nothing


class TestLocks(Scenario):
    def hold(self, dr, name):
        code = ("import sys; from rawlayer.search.common import IndexLock; "
                f"l = IndexLock({str(dr / 'index')!r}, {name!r}); print('locked', flush=True); sys.stdin.read()")
        holder = subprocess.Popen([sys.executable, "-s", "-B", "-c", code], cwd=T.REPO, stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, text=True)
        self.assertEqual(holder.stdout.readline().strip(), "locked")

        def release():
            holder.stdin.close()
            holder.wait(10)
            holder.stdout.close()
        return release

    def test_lock_busy_exit_75(self):
        cfg, dr = self.clone()
        release = self.hold(dr, "toy")
        try:
            rc, out, _ = run_search("index", "--config", cfg, "--source", "toy")
            self.assertEqual(rc, 75)
            self.assertIn("busy", out)
            self.assertEqual(search(cfg, "배포")["total"], 1)       # readers are not blocked
            rc, out, _ = run_search("index", "--config", cfg, "--source", "toy", "--status", "--json")
            self.assertIs(json.loads(out)["toy"]["writer_busy"], True)
        finally:
            release()
        self.assertEqual(self.index(cfg)[0], 0)

    def test_index_all_sources(self):
        cfg, _dr = self.clone()
        rc, reps, err = self.index(cfg, source="all")
        self.assertEqual(rc, 0, err)
        self.assertEqual((reps["toy"]["action"], reps["toy2"]["action"], reps["toy2"]["rows"]),
                         ("current", "rebuilt", 38))
        self.assertEqual(search(cfg, "배포", source="toy2")["keys"], ["toy:toy_m:1"])

    def test_sources_lock_independently(self):
        cfg, dr = self.clone()
        release = self.hold(dr, "toy2")
        try:
            rc, reps, _ = self.index(cfg, source="all")
            self.assertEqual(rc, 75)
            self.assertEqual((reps["toy"]["action"], reps["toy2"]["action"]), ("current", "busy"))
        finally:
            release()
        rc, reps, _ = self.index(cfg, source="all")
        self.assertEqual((rc, reps["toy"]["action"], reps["toy2"]["action"], reps["toy2"]["rows"]),
                         (0, "current", "rebuilt", 38))

    def test_one_source_failing_does_not_stop_the_other(self):
        """An unexpected error in one index update is reported for it (exit 1) and the other is still built."""
        cfg, dr = self.clone()
        ipath(dr).unlink()
        with mock.patch.object(toy2.Toy2Index, "applier", side_effect=sqlite3.OperationalError("disk I/O error")):
            rc, out, err = run_search("index", "--config", cfg, "--json")
        self.assertEqual(rc, 1, err)
        rep = json.loads(out)
        self.assertEqual((rep["toy2"]["action"], rep["toy2"]["reason"]),
                         ("failed", "error OperationalError: disk I/O error"))
        self.assertEqual((rep["toy"]["action"], rep["toy"]["seq"]), ("rebuilt", 3))
        self.assertFalse(ipath(dr, "toy2").exists())
        self.assertFalse(T.suffixed(ipath(dr, "toy2"), ".new").exists())
        rc, out, _ = run_search("index", "--config", cfg)
        self.assertEqual(rc, 0)
        self.assertIn("toy: current", out)
        self.assertIn("toy2: rebuilt", out)


class TestEdges(T.ToyCase):
    APPS = (T.TOY,)
    STANDARD = False

    def bare_config(self, data_root):
        cfg = self.fx.base / "search.json"
        T.write_json(cfg, {"v": 1, "data_root": data_root})
        return cfg

    def test_missing_index_and_empty_ledger(self):
        cfg = self.bare_config(self.fx.data_root)
        rc, _out, err = run_search("search", "toy", "--config", cfg, "x")
        self.assertEqual(rc, 2)
        self.assertIn("manage.py collect", err)
        rc, out, _ = run_search("index", "--config", cfg, "--source", "toy", "--json")
        rep = json.loads(out)["toy"]
        self.assertEqual((rc, rep["action"], rep["seq"], rep["rows"]), (0, "rebuilt", 0, 0))
        env = search(cfg, "가나다")
        self.assertEqual((env["total"], env["index"]["stale"]), (0, False))
        self.assertEqual(run_search("index", "--config", cfg, "--source", "toy")[0], 0)

    def test_data_root_outside_parent_refused(self):
        cfg = self.bare_config(T.REPO)
        self.assertEqual(run_search("index", "--config", cfg)[0], 2)
        self.assertEqual(run_search("index", "--config", self.bare_config("relative"))[0], 2)
        self.assertEqual(run_search("index", "--config", self.fx.base / "none.json")[0], 2)

    def test_usage_errors(self):
        cfg = self.bare_config(self.fx.data_root)
        self.assertEqual(run_search("index", "--config", cfg, "--source", "toy")[0], 0)
        for argv, msg in ((["search", "toy", "--config", cfg], "give a query"),
                          (["search", "toy", "--config", cfg, "x", "-q", "y"], "once"),
                          (["search", "toy", "--config", cfg, "x:y"], "column filter"),
                          (["search", "toy", "--config", cfg, "x", "--limit", "0"], "--limit"),
                          (["search", "nope", "--config", cfg, "x"], "invalid choice"),
                          (["index", "--config", cfg, "--source", "nope"], "invalid choice")):
            with self.subTest(argv=argv):
                rc, _out, err = run_search(*argv)
                self.assertEqual(rc, 2)
                self.assertIn(msg, err)
                self.assertTrue(err.startswith("search: "))
