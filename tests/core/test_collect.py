# -*- coding: utf-8 -*-
"""manage.py collect (rawlayer/collect.py), the scheduled task's body: capture, then the search indexes after every
capture, whatever its exit code; the task log; the exit code is capture's. The steps are replaced by a stub
(TestCollectSteps), or by the real capture and index run in-process on the toy app (TestIndexAfterSkipped: a 75
that committed)."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from rawlayer import collect
from rawlayer.capture import paths
from rawlayer.search import common
from tests.core import toyfix as T
from tests.core.toyapp import search as toy
from tests.core.toyapp2 import search as toy2


class TestCollectSteps(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp(prefix="llmw-task-", dir=T.TEST_PARENT))
        self.addCleanup(T.force_rmtree, self.base)
        self.data_root = self.base / "data"
        self.data_root.mkdir()
        self.cfg = self.base / "config.json"
        T.write_json(self.cfg, {"v": 1, "data_root": self.data_root})
        self.log = self.data_root / "task.log"

    def run_task(self, capture_rc, index_rc=0, capture_err="", capture_dies=False):
        calls = []

        def fake(name, argv):
            calls.append((name, list(argv)))
            if name == "capture":
                if capture_dies:
                    raise RuntimeError("boom")
                print("capture done run=1 put=2")
                if capture_err:
                    print(capture_err, end="", file=sys.stderr)
                return capture_rc
            print("index one=1 two=2")
            print("a warning", file=sys.stderr)
            return index_rc

        with mock.patch.object(collect, "one", side_effect=fake), \
                mock.patch.object(collect, "log_path", return_value=self.log):
            rc = collect.main(["toy", "--config", str(self.cfg)])
        return rc, calls, self.log.read_text(encoding="utf-8")

    def test_the_capture_step_argv(self):
        _rc, calls, _log = self.run_task(0)
        self.assertEqual(calls[0], ("capture", ["--config", str(self.cfg), "--input-set", "live"]))

    def test_index_follows_every_capture(self):
        """75 also follows a commit (an origin or container skipped), so the index step never depends on rc."""
        for capture_rc in (0, 3, 75, 2, 1):
            with self.subTest(capture_rc=capture_rc):
                rc, calls, log = self.run_task(capture_rc, index_rc=5)
                self.assertEqual(rc, capture_rc)                       # the task's result is capture's
                self.assertEqual(calls[1], ("index", ["--config", str(self.cfg)]))
                self.assertIn(f"step=capture rc={capture_rc} ", log)
                self.assertIn("step=index rc=5 ", log)
                self.assertIn("  index one=1 two=2\n", log)
                self.assertIn("  stderr: a warning\n", log)
                self.log.unlink()

    def test_capture_stderr_kept_only_as_cli_error_lines(self):
        _rc, _calls, log = self.run_task(1, capture_err="error OSError errno=13\nTraceback (most recent call last):\n")
        self.assertIn("  error OSError errno=13\n", log)
        self.assertIn("  stderr line withheld\n", log)
        self.assertNotIn("Traceback", log)

    def test_a_step_that_dies_is_rc1_and_the_index_still_runs(self):
        rc, calls, log = self.run_task(0, capture_dies=True)
        self.assertEqual((rc, len(calls)), (1, 2))
        self.assertIn("step=capture rc=1 ", log)
        self.assertIn("step=index rc=0 ", log)
        self.assertIn("  stderr line withheld\n", log)               # the traceback is never appended

    def test_a_run_in_progress_is_on_disk_before_capture_ends(self):
        """`schedule status` reads task.log while the task runs: the run's line is there before its first step."""
        seen = []

        def fake(name, argv):
            seen.append(self.log.read_text(encoding="utf-8"))
            return 0

        self.log.write_text("run\n2026-10-05T01:00:00Z step=capture rc=0 seconds=9\n"
                            "2026-10-05T01:00:09Z step=index rc=0 seconds=1\n", encoding="utf-8", newline="\n")
        with mock.patch.object(collect, "one", side_effect=fake), \
                mock.patch.object(collect, "log_path", return_value=self.log):
            collect.main(["toy", "--config", str(self.cfg)])
        self.assertTrue(seen[0].endswith("step=index rc=0 seconds=1\nrun\n"))

    def test_log_path_stays_inside_the_data_parent(self):
        with mock.patch.object(paths, "DATA_PARENT", T.TEST_PARENT):
            self.assertEqual(collect.log_path(self.cfg), self.data_root / "reports" / "task.log")
            T.write_json(self.cfg, {"v": 1, "data_root": self.base / "typo"})
            self.assertEqual(collect.log_path(self.cfg), T.TEST_PARENT.resolve() / "task-refused.log")
            self.assertFalse((self.base / "typo").exists())


class TestIndexAfterSkipped(T.ToyCase):
    """capture commits and THEN returns 75 when an origin or a container could not be read (here a toy_a container
    that fails with an I/O error). The task must still bring every index up to the ledger, run after run."""
    APPS = (T.TOY, T.TOY2)

    def setUp(self):
        super().setUp()
        self.fx.a_put("c9", [{"fail": "io"}])

    def task(self):
        def fake(name, argv):
            if name == "capture":
                return T.capture(self.fx)
            rc, _out, _err = T.run_search("index", "--config", str(self.fx.config_path))
            return rc

        log = self.fx.data_root / "task.log"
        with mock.patch.object(collect, "one", side_effect=fake), \
                mock.patch.object(collect, "log_path", return_value=log):
            return collect.main(["toy", "--config", str(self.fx.config_path)])

    def heads(self):
        ledger_seq = len(common.LedgerView(self.fx.data_root).manifests())
        out = {}
        for spec in (toy.SPEC, toy2.SPEC):
            con = common.open_ro(common.index_path(self.fx.data_root, spec))
            try:
                out[spec.name] = int(dict(con.execute("SELECT k, v FROM meta").fetchall())["head_seq"])
            finally:
                con.close()
        return ledger_seq, out

    def test_a_committing_75_still_updates_every_index(self):
        self.assertEqual(self.task(), 75)
        self.assertEqual(self.heads(), (1, {"toy": 1, "toy2": 1}))
        self.fx.a_put("c2", [T.item(3, "API 키 발급"), T.item(30, "later")])          # new rows for run 2
        self.assertEqual(self.task(), 75)
        self.assertEqual(self.heads(), (2, {"toy": 2, "toy2": 2}))


if __name__ == "__main__":
    unittest.main()
