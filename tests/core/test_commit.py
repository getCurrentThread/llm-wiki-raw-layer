# -*- coding: utf-8 -*-
"""The commit (SPEC 9.1) and everything that keeps it whole, on the toy app: a crash between every commit step,
in-process and as a real process kill; narrow recovery (only what inflight.json names), its path spellings, log.md
backfill, recovery that is itself killed; ledger defects that stop every command (a seq gap, a flipped segment byte,
a manifest that parses but lost a field, a record SQLite cannot store); the run lock; a damaged state.sqlite set aside
and rebuilt; the environment of a SYSTEM run; srcmap.json."""
import gzip
import json
import os
import stat
import subprocess
import sys
import unittest
from pathlib import Path

from rawlayer.capture import canon, run
from rawlayer.capture.ledger import LedgerCorrupt
from tests.core import toyfix as T
from tests.core.toyfix import (capture, cli_main, crash_at, export, invariants, item, ledger, log_runs, manifests,
                               replay_diff)


def more(fx):
    """A new container, so the next capture has segments to write."""
    fx.a_put("c5", [item(50, "more"), item(51, "more too")])


def rewrite(path, data):
    path = Path(path)
    path.chmod(stat.S_IWRITE | stat.S_IREAD)
    path.write_bytes(data)


def read_json(path):
    with Path(path).open(encoding="utf-8") as fh:
        return json.load(fh)


def set_aside(p):
    """The copies of state.sqlite `p` set aside as damaged (<p>.bad-*)."""
    return list(p.parent.glob(p.name + ".bad-*"))


class Reference(T.ToyCase):
    """The export of one uninterrupted genesis of the standard inputs."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        fx = T.ToyFixture().standard()
        try:
            assert capture(fx) == 0
            cls.ref = export(fx)
        finally:
            T.force_rmtree(fx.base)


class TestCrashPoints(Reference):
    STEPS = ("inflight", "segment", "manifest_part", "commit", "renamed", "finalize_one", "state", "log")

    def test_each_step(self):
        for step in self.STEPS:
            with self.subTest(step=step):
                fx = T.ToyFixture().standard()
                try:
                    crash_at(fx, step)
                    lg = ledger(fx)
                    if lg.inflight.exists():                # SPEC 9.1 step 5: {run, seq, parts, manifest_part}
                        inf = read_json(lg.inflight)
                        self.assertEqual(set(inf), {"run", "seq", "parts", "manifest_part"})
                        listed = {lg.raw.joinpath(*p.split("/")) for p in inf["parts"]}
                        self.assertLessEqual(set(lg.part_files()), listed)
                    self.assertEqual(capture(fx), 0)
                    self.assertEqual(export(fx), self.ref)
                    self.assertEqual(lg.part_files(), [])
                    self.assertFalse(lg.inflight.exists())
                    self.assertEqual(replay_diff(fx), 0)
                    self.assertEqual(invariants(fx)[0], {})
                    committed = step not in ("inflight", "segment", "manifest_part")
                    self.assertEqual(len(lg.manifests()), 2 if committed else 1)
                finally:
                    T.force_rmtree(fx.base)


class TestRealProcessKill(Reference):
    """The process dies (os._exit: no finally, no flush) between commit steps; the next run recovers to the same
    ledger as an uninterrupted run, with no leftovers and state equal to a replay."""
    STEPS = ("inflight", "segment", "manifest_part", "commit", "renamed", "finalize_one", "finalize_one#2", "state",
             "log")

    @staticmethod
    def kill_at(fx, step):
        name, _, nth = step.partition("#")
        return T.child_capture(fx, before=(
            "seen = {'n': 0}\n"
            "def hook(step):\n"
            f"    if step == {name!r}:\n"
            "        seen['n'] += 1\n"
            f"        if seen['n'] == {int(nth or 1)}:\n"
            "            os._exit(9)\n"
            "run.CRASH_HOOK = hook\n"))

    def test_each_step(self):
        for step in self.STEPS:
            with self.subTest(step=step):
                fx = T.ToyFixture().standard()
                try:
                    self.assertEqual(self.kill_at(fx, step), 9)
                    self.assertEqual(capture(fx), 0)
                    lg = ledger(fx)
                    self.assertEqual(export(fx), self.ref)
                    self.assertEqual(lg.leftovers(), 0)
                    self.assertEqual(replay_diff(fx), 0)
                    self.assertEqual(invariants(fx)[0], {})
                    mans = manifests(fx)
                    committed = step not in ("inflight", "segment", "manifest_part")
                    self.assertEqual(len(mans), 2 if committed else 1)
                    self.assertEqual(log_runs(fx), [m["run"] for m in mans])
                finally:
                    T.force_rmtree(fx.base)


class TestRecovery(T.ToyCase):
    def stray(self, rel):
        p = ledger(self.fx).raw.joinpath(*rel.split("/"))
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"damage evidence")
        return p

    def test_stray_part_is_kept_and_reported(self):
        """Recovery touches only what inflight.json names; a stray .part stays (leftovers, the seal check)."""
        self.assertEqual(capture(self.fx), 0)
        seg = self.stray("ledger/toy_b/2026/seg-00000007-toy_b-000.jsonl.gz.part")
        man = self.stray("manifests/00000007-1700000000000-abcd.json.part")
        self.assertEqual(capture(self.fx), 0)
        self.assertTrue(seg.exists() and man.exists())
        self.assertEqual(ledger(self.fx).leftovers(), 2)
        self.assertEqual(invariants(self.fx)[0], {"leftover": 2})

    def test_same_seq_stray_survives_a_discarded_commit(self):
        """A stray .part with the crashed run's seq is not the run's own (not in inflight.json)."""
        self.assertEqual(capture(self.fx), 0)
        more(self.fx)
        stray = self.stray("ledger/toy_b/2026/seg-00000002-toy_b-009.jsonl.gz.part")
        crash_at(self.fx, "manifest_part")
        lg = ledger(self.fx)
        named = read_json(lg.inflight)["parts"]
        self.assertTrue(named)
        self.assertEqual(capture(self.fx), 0)
        self.assertTrue(stray.exists())
        self.assertEqual(lg.part_files(), [stray])
        for rel in named:
            self.assertFalse(lg.raw.joinpath(*rel.split("/")).exists())

    def test_inflight_path_spellings(self):
        """SPEC 9.1 leaves the spelling of inflight.json paths open: absolute native paths and data_root-relative
        ones ('raw/ledger/...', 'raw\\manifests\\...') recover like the raw-relative ones commit writes, on the
        finish branch (crash after COMMIT) and the discard branch (crash before it)."""
        for spelling in ("absolute", "raw_prefixed"):
            for step, n_manifests in (("commit", 3), ("manifest_part", 2)):
                with self.subTest(spelling=spelling, step=step):
                    fx = T.ToyFixture().standard()
                    try:
                        self.assertEqual(capture(fx), 0)
                        more(fx)
                        crash_at(fx, step)
                        lg = ledger(fx)
                        inf = read_json(lg.inflight)
                        self.assertTrue(inf["parts"])
                        if spelling == "absolute":      # native, as str(path) spells it
                            inf["parts"] = [str(lg.raw.joinpath(*p.split("/"))) for p in inf["parts"]]
                            inf["manifest_part"] = str(lg.raw.joinpath(*inf["manifest_part"].split("/")))
                        else:                           # 'raw/ledger/...' and the native 'raw\\manifests\\...'
                            inf["parts"] = ["raw/" + p for p in inf["parts"]]
                            inf["manifest_part"] = str(Path("raw", *inf["manifest_part"].split("/")))
                        T.write_json(lg.inflight, inf)
                        self.assertEqual(capture(fx), 0)
                        self.assertEqual((lg.leftovers(), replay_diff(fx)), (0, 0))
                        self.assertEqual(len(manifests(fx)), n_manifests)
                    finally:
                        T.force_rmtree(fx.base)

    def test_log_backfilled_after_crash_past_commit(self):
        """A run that died after COMMIT but before its log.md line gets the line on recovery (once)."""
        crash_at(self.fx, "state")
        crashed = manifests(self.fx)[0]["run"]
        self.assertFalse(ledger(self.fx).log_md.exists() and log_runs(self.fx))
        self.assertEqual(capture(self.fx), 0)
        runs = log_runs(self.fx)
        self.assertEqual(runs, [m["run"] for m in manifests(self.fx)])
        self.assertEqual(runs[0], crashed)

    def test_log_not_duplicated_when_crash_after_log(self):
        crash_at(self.fx, "log")
        self.assertEqual(capture(self.fx), 0)
        self.assertEqual(log_runs(self.fx), [m["run"] for m in manifests(self.fx)])

    def test_torn_inflight_tmp_is_removed(self):
        lg = ledger(self.fx)
        lg.ensure_dirs()
        tmp = T.suffixed(lg.inflight, ".tmp")
        tmp.write_bytes(b'{"run": "17')
        self.assertEqual(capture(self.fx), 0)
        self.assertFalse(tmp.exists())
        self.assertEqual(lg.leftovers(), 0)

    def test_inflight_outside_ledger_refused(self):
        """An inflight.json naming a file outside ledger/ or manifests/ is a ledger defect: capture exits 1 and
        deletes nothing; the inflight.json stays (a leftover)."""
        self.assertEqual(capture(self.fx), 0)
        lg = ledger(self.fx)
        victim = self.fx.base / "victim.part"
        victim.write_bytes(b"x")
        T.write_json(lg.inflight, {"run": "1700000000000-abcd", "seq": 2, "parts": ["../../victim.part"],
                                   "manifest_part": "manifests/00000002-1700000000000-abcd.json.part"})
        self.assertEqual(cli_main(self.fx, "capture", "--origins", "toy_a"), 1)
        self.assertTrue(victim.exists())
        self.assertEqual(ledger(self.fx).leftovers(), 1)
        self.assertEqual(invariants(self.fx)[0], {"leftover": 1})

    def test_killed_during_recovery(self):
        """The process dies inside recovery (parts finalised, log line written, inflight.json not yet removed); the
        next run's recovery is re-entrant and the ledger ends as an uninterrupted run."""
        ref_fx = T.ToyFixture().standard()
        try:
            self.assertEqual(capture(ref_fx), 0)
            ref = export(ref_fx)
        finally:
            T.force_rmtree(ref_fx.base)
        self.assertEqual(T.child_capture(self.fx, "run.CRASH_HOOK = lambda s: os._exit(9) if s == 'commit' else None\n"),
                         9)
        inflight = ledger(self.fx).inflight
        self.assertTrue(inflight.exists())
        # whether recovery hands remove_file a Path or its text, and in whatever case: the same file
        self.assertEqual(T.child_capture(self.fx, (
            "real = fsio.remove_file\n"
            f"fsio.remove_file = lambda p: os._exit(9) if os.path.normcase(p) == {os.path.normcase(inflight)!r} "
            "else real(p)\n")), 9)
        self.assertEqual(capture(self.fx), 0)
        lg = ledger(self.fx)
        self.assertEqual((lg.leftovers(), invariants(self.fx)[0]), (0, {}))
        self.assertEqual(log_runs(self.fx), [m["run"] for m in manifests(self.fx)])
        self.assertEqual(export(self.fx), ref)


class TestLedgerDefects(T.ToyCase):
    def test_removed_manifest_is_a_seq_gap(self):
        """A missing manifest is a seq gap: capture, replay and export-state refuse (exit 1)."""
        for _ in range(3):
            self.assertEqual(capture(self.fx), 0)
        lg = ledger(self.fx)
        lg.manifests()[1][2].unlink()
        self.assertEqual(invariants(self.fx)[0].get("seq_gap"), 1)
        out = lg.verify_root / "x.tsv"
        for argv in (["capture", "--origins", "toy_a"], ["replay", "--check"], ["export-state", "--out", out]):
            with self.subTest(command=argv[0]):
                self.assertEqual(cli_main(self.fx, *argv), 1)
        self.assertEqual(len(T.names(lg.man_dir)), 2)

    def test_flipped_segment_byte(self):
        """A damaged segment: the seal check sees the sha mismatch, and replay (which folds the ledger again) and
        export-state stop with exit 1 instead of producing a state or an export from it."""
        self.assertEqual(capture(self.fx), 0)
        seg = sorted(ledger(self.fx).ledger_dir.glob("*/*/*.jsonl.gz"), key=str)[0]    # text order, as glob.glob's
        seg.chmod(0o600)
        with seg.open("r+b") as fh:
            fh.seek(seg.stat().st_size // 2)
            b = fh.read(1)
            fh.seek(-1, 1)
            fh.write(bytes([b[0] ^ 0xFF]))
        seg.chmod(0o400)
        self.assertEqual(invariants(self.fx)[0].get("sha_mismatch"), 1)
        out = ledger(self.fx).verify_root / "x.tsv"
        for argv in (["replay", "--check"], ["export-state", "--out", out]):
            with self.subTest(command=argv[0]):
                self.assertEqual(cli_main(self.fx, *argv), 1)

    def test_seal_catches_what_still_folds(self):
        """A segment recompressed (valid gzip, the same records) or an earlier manifest rewritten (the same JSON, other
        bytes) folds as before: only its sha256 / the prev_sha256 chain shows it, and replay --check exits 1."""
        def rewrite(path, data):
            path.chmod(0o600)
            path.write_bytes(data)
            path.chmod(0o400)

        for case in ("segment", "manifest"):
            with self.subTest(case=case):
                self.fx = T.ToyFixture().standard()
                self.addCleanup(T.force_rmtree, self.fx.base)
                for _ in range(2):
                    self.assertEqual(capture(self.fx), 0)
                lg = ledger(self.fx)
                self.assertEqual(cli_main(self.fx, "replay", "--check"), 0)
                if case == "segment":
                    seg = sorted(lg.ledger_dir.glob("*/*/*.jsonl.gz"), key=str)[0]
                    rewrite(seg, gzip.compress(gzip.decompress(seg.read_bytes()), compresslevel=1, mtime=0))
                else:
                    first = lg.manifests()[0][2]
                    rewrite(first, json.dumps(json.loads(first.read_bytes()), indent=4).encode("ascii"))
                self.assertEqual(cli_main(self.fx, "replay", "--check"), 1)

    def test_manifest_that_parses_but_lost_a_field(self):
        """Valid JSON with a renamed key, a wrong type or a status outside the vocabulary is an unreadable manifest:
        replay and export-state stop with exit 1 and change nothing."""
        def rename_path(m):
            m["segments"][0]["pbth"] = m["segments"][0].pop("path")

        def status_type(m):
            m["origins"]["toy_a"]["status"] = 7

        def input_set(m):
            m["input_set"] = "lively"

        def container_status(m):
            facts = next(iter(m["origins"]["toy_b"]["containers"].values()))
            facts["status"] = "clfan"

        def unknown_origin(m):
            m["origins"]["toy_q"] = m["origins"].pop("toy_m")
        for change in (rename_path, status_type, input_set, container_status, unknown_origin):
            with self.subTest(change=change.__name__):
                fx = T.ToyFixture().standard()
                try:
                    self.assertEqual(capture(fx), 0)
                    self.assertEqual(capture(fx), 0)
                    first = ledger(fx).manifests()[0][2]
                    m = json.loads(first.read_bytes())
                    change(m)
                    rewrite(first, json.dumps(m, sort_keys=True, indent=1).encode("ascii"))
                    state_p = ledger(fx).state_dir / "state.sqlite"
                    state_before = state_p.read_bytes()
                    out = ledger(fx).verify_root / "x.tsv"
                    for argv in (["replay", "--check"], ["replay"], ["export-state", "--out", out]):
                        self.assertEqual(cli_main(fx, *argv), 1, argv[0])
                    self.assertEqual(state_p.read_bytes(), state_before)
                finally:
                    T.force_rmtree(fx.base)

    def test_record_sqlite_cannot_store_is_a_ledger_defect(self):
        """A record with op null is LedgerCorrupt, not state damage: nothing is set aside, capture stops (and keeps
        stopping)."""
        self.assertEqual(capture(self.fx), 0)
        more(self.fx)
        self.assertEqual(capture(self.fx), 0)
        lg = ledger(self.fx)
        seg = lg.raw.joinpath(*manifests(self.fx)[1]["segments"][0]["path"].split("/"))
        with gzip.open(seg, "rb") as fh:
            lines = fh.read().splitlines()
        rec = json.loads(lines[0])
        rec["op"] = None
        lines[0] = json.dumps(rec, sort_keys=True).encode("ascii")
        rewrite(seg, gzip.compress(b"\n".join(lines) + b"\n", mtime=0))
        p = lg.state_dir / "state.sqlite"
        p.unlink()
        with self.assertRaises(LedgerCorrupt):
            capture(self.fx)
        self.assertEqual(set_aside(p), [])
        self.assertEqual(invariants(self.fx)[0].get("op_bad"), 1)
        self.assertEqual(cli_main(self.fx, "capture", "--origins", "toy_a"), 1)
        self.assertEqual(len(manifests(self.fx)), 2)


class TestStateDamage(T.ToyCase):
    def damage(self, offset=0, data=b"not a database!!" * 8):
        p = ledger(self.fx).state_dir / "state.sqlite"
        with p.open("r+b") as fh:
            fh.seek(offset)
            fh.write(data)
        return p

    def test_capture_sets_it_aside_and_rebuilds(self):
        self.assertEqual(capture(self.fx), 0)
        ref = export(self.fx)
        p = self.damage()
        self.assertEqual(capture(self.fx), 0)
        self.assertEqual(len(set_aside(p)), 1)
        self.assertEqual((export(self.fx), replay_diff(self.fx)), (ref, 0))
        self.assertEqual(len(manifests(self.fx)), 2)

    def test_replay_repairs(self):
        self.assertEqual(capture(self.fx), 0)
        p = self.damage()
        self.assertEqual(cli_main(self.fx, "replay", "--check"), 0)
        self.assertEqual(len(set_aside(p)), 1)
        self.assertEqual(replay_diff(self.fx), 0)

    def test_damage_below_the_header(self):
        """A damaged page past the header opens fine; quick_check finds it and the state is set aside and rebuilt."""
        self.assertEqual(capture(self.fx), 0)
        ref = export(self.fx)
        p = self.damage(2 * 4096, b"\xa5" * 4096)                  # page 3: the heads table's root
        self.assertEqual(capture(self.fx), 0)
        self.assertEqual(len(set_aside(p)), 1)
        self.assertEqual((export(self.fx), replay_diff(self.fx)), (ref, 0))

    def test_app_state_tables_are_rebuilt_too(self):
        self.assertEqual(capture(self.fx), 0)
        (ledger(self.fx).state_dir / "state.sqlite").unlink()
        self.assertEqual(capture(self.fx), 0)                       # the fold again, toy_last and hist included
        self.assertEqual(replay_diff(self.fx), 0)


class TestLockAndEnvironment(T.ToyCase):
    def test_lock_busy_exit_75(self):
        lg = ledger(self.fx)
        lg.ensure_dirs()
        code = ("import sys; from rawlayer.capture.run import RunLock; from rawlayer.capture.ledger import Ledger; "
                f"l = RunLock(Ledger({str(lg.raw)!r})); print('locked', flush=True); sys.stdin.read()")
        holder = subprocess.Popen([sys.executable, "-s", "-B", "-c", code], cwd=T.REPO, stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(holder.stdout.readline().strip(), "locked")
            self.assertEqual(capture(self.fx), 75)
            self.assertEqual(manifests(self.fx), [])
        finally:
            holder.stdin.close()
            holder.wait(10)
            holder.stdout.close()
        self.assertEqual(capture(self.fx), 0)

    def test_environment_systemprofile(self):
        """A SYSTEM run's profile folders stay untouched: TEMP and the app-data variables point into raw/.work."""
        fake = self.fx.base / "systemprofile"
        (fake / "AppData" / "Local").mkdir(parents=True)
        with T.preserved_env():
            os.environ.update({"LOCALAPPDATA": str(fake / "AppData" / "Local"), "USERPROFILE": str(fake),
                               "APPDATA": str(fake / "AppData" / "Roaming"),
                               "TEMP": str(fake / "Temp"), "TMP": str(fake / "Temp")})
            self.assertEqual(capture(self.fx), 0)
        # os.walk, not Path.walk: Path.walk enters no junction and files a link to a directory as a file
        found = [Path(dp, f) for dp, _d, fs in os.walk(fake) for f in fs]
        self.assertEqual(found, [])
        self.assertEqual(T.names(fake / "AppData"), ["Local"])

    def test_srcmap_is_flat(self):
        """SPEC 3: {source_id: relpath}; the old nested form is flattened on the next run."""
        def srcmap():
            return read_json(ledger(self.fx).state_dir / "srcmap.json")
        self.assertEqual(capture(self.fx), 0)
        m = srcmap()
        self.assertTrue(m and all(isinstance(v, str) for v in m.values()))
        self.assertEqual(m[canon.source_id("a/c1.jsonl")], "a/c1.jsonl")
        self.assertEqual(m[canon.source_id("m/c2.jsonl")], "m/c2.jsonl")
        T.write_json(ledger(self.fx).state_dir / "srcmap.json", {"toy_m": {"0123456789ab": "m/x.jsonl"}, "toy_a": m})
        self.assertEqual(capture(self.fx), 0)
        m2 = srcmap()
        self.assertEqual(m2["0123456789ab"], "m/x.jsonl")
        self.assertTrue(all(isinstance(v, str) for v in m2.values()))

    def test_crash_hook_is_off_by_default(self):
        self.assertIsNone(run.CRASH_HOOK)


if __name__ == "__main__":
    unittest.main()
