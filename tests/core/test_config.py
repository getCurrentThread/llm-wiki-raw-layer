# -*- coding: utf-8 -*-
"""Preconditions, on the toy app: config refusals (SPEC 2, 16) and the apps' options, the data parent allowlist and
junctions (SPEC 10.1), the frozen input sets (freeze.json, static drift), the work-tree precheck, and provenance
that is never clean when git fails or refuses (SYSTEM's git ownership rules)."""
import contextlib
import datetime
import hashlib
import io
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from rawlayer import registry
from rawlayer.capture import paths, precheck, run
from rawlayer.capture.config import Config, ConfigError, InputSet
from rawlayer.search import index
from tests.core import toyfix as T
from tests.core.toyfix import capture, cli_main, manifests


class TestConfig(T.ToyCase):
    def refused(self, **kv):
        self.fx.set_cfg(**kv)
        with self.assertRaises(ConfigError):
            Config(self.fx.config_path)

    def test_impl_and_version(self):
        self.refused(impl=None)
        self.fx.write_config()
        self.refused(impl="another")
        self.fx.write_config()
        self.refused(v=2)
        self.fx.config_path.write_text("{not json", encoding="utf-8")
        with self.assertRaises(ConfigError):
            Config(self.fx.config_path)

    def test_sources(self):
        missing = self.fx.base / "nope"
        for v in ([], {"toy_dir": missing}, {"toy_dir": "relative"}, {}):
            with self.subTest(sources=v):
                self.fx.write_config()
                self.refused(sources=v)

    def test_aux_paths(self):
        """data_root exists and is absolute; inputs_root may be left out or point nowhere until a frozen set is used;
        keys nobody reads are accepted and ignored."""
        missing = self.fx.base / "nope"
        for v in (missing, "relative"):
            with self.subTest(key="data_root", value=v):
                self.fx.write_config()
                self.refused(data_root=v)
        (self.fx.base / "a_file").write_bytes(b"")
        self.fx.write_config()
        self.refused(data_root=self.fx.base / "a_file")
        with self.subTest(key="inputs_root", value="relative"):
            self.fx.write_config()
            self.refused(inputs_root="relative")
        for k, v in (("old_tool", missing / "tool.py"), ("old_dir", missing), ("old_tool", 5),
                     ("inputs_root", None), ("inputs_root", missing)):
            with self.subTest(key=k, value=v):
                self.fx.write_config(old_tool="x", old_dir="y")
                self.fx.set_cfg(**{k: v})
                Config(self.fx.config_path)
        self.fx.write_config(old_tool="x", old_dir="y")
        self.assertEqual(capture(self.fx), 0)
        self.fx.set_cfg(old_tool=None, old_dir=None, inputs_root=None)
        self.assertEqual(capture(self.fx), 0)
        for name in ("T1", "T2"):                    # a frozen set needs inputs_root
            with self.subTest(input_set=name), self.assertRaises(ConfigError):
                InputSet(Config(self.fx.config_path), name)

    def test_option_types(self):
        self.assertIs(self.fx.cfg().toy_system, False)
        self.refused(toy_system="yes")
        self.fx.write_config()
        self.fx.set_cfg(toy_system=True)
        self.assertIs(self.fx.cfg().toy_system, True)

    def test_data_root_allowlist(self):
        """Every command refuses a data root outside the data parent, before it writes anything."""
        elsewhere = self.fx.base / "parent"
        elsewhere.mkdir()
        before = T.names(self.fx.data_root)
        out = self.fx.data_root / "raw" / ".verify" / "x.tsv"
        with mock.patch.object(paths, "DATA_PARENT", elsewhere):
            with self.assertRaises(ConfigError):
                Config(self.fx.config_path)
            for argv in (["capture"], ["replay"], ["export-state", "--out", out]):
                with self.subTest(command=argv[0]):
                    self.assertEqual(cli_main(self.fx, *argv), 2)
        self.assertEqual(T.names(self.fx.data_root), before)
        self.assertTrue(paths.inside(paths.DATA_PARENT / "x", paths.DATA_PARENT))
        self.assertFalse(paths.inside(paths.DATA_PARENT, paths.DATA_PARENT))
        self.assertFalse(paths.inside(Path(f"{paths.DATA_PARENT}-x"), paths.DATA_PARENT))

    def test_frozen_set_without_inputs_root_is_refused_before_anything_is_written(self):
        """llm-wiki refused a missing inputs_root at Config load: no run lock, no state.sqlite, no raw/."""
        self.fx.set_cfg(inputs_root=self.fx.base / "no-such-folder")
        before = T.names(self.fx.data_root)
        for name in ("T1", "T2"):
            with self.subTest(input_set=name):
                self.assertEqual(cli_main(self.fx, "capture", "--input-set", name, "--origins", "toy_a"), 2)
                with T.preserved_env(), contextlib.redirect_stdout(io.StringIO()), self.assertRaises(ConfigError):
                    run.capture(Config(self.fx.config_path), name, ["toy_a"], skip_precheck=True)
        self.assertEqual(T.names(self.fx.data_root), before)
        self.assertFalse((self.fx.data_root / "raw").exists())
        self.assertEqual(capture(self.fx), 0)                  # live never reads inputs_root


class TestFrozenSet(T.ToyCase):
    """T1 / T2 read copies under <inputs_root>/<name> that freeze.json describes; the static files must still be
    there with their hashes, and a closed static root holds no file the freeze did not list."""

    def freeze(self, name="T1", static=None, finished="2026-10-01T00:00:00Z"):
        root = self.fx.frozen / name
        shutil.copytree(self.fx.toy, root / "toy")
        if static is None:
            static = {"toy_a": {n: {"sha256": self.sha(root / "toy" / "a" / n)} for n in T.names(root / "toy" / "a")}}
        T.write_json(root / "freeze.json", {"finished_at": finished, "static": static})
        return root / "toy"

    @staticmethod
    def sha(path):
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()

    def test_capture_from_a_frozen_set(self):
        root = self.freeze()
        inputs = InputSet(self.fx.cfg(), "T1")
        finished = int(datetime.datetime(2026, 10, 1, tzinfo=datetime.timezone.utc).timestamp())
        self.assertEqual((inputs.toy_a, inputs.live_frozen, inputs.snapshot_s), (root / "a", True, finished))
        shutil.rmtree(self.fx.toy)                          # live inputs are not read
        self.fx.toy.mkdir(parents=True)
        self.assertEqual(capture(self.fx, input_set="T1"), 0)
        m = manifests(self.fx)[0]
        self.assertEqual((m["input_set"], m["origins"]["toy_a"]["counts"]["put"]), ("T1", 3))

    def test_drift_and_unfrozen_sets_are_refused(self):
        def edit(root):
            with (root / "a" / "c1.jsonl").open("a", encoding="utf-8") as fh:
                fh.write("{}\n")

        def add(root):                                     # a closed static root holds no file the freeze did not list
            T.ToyFixture.write(root / "a", "c9", [T.item(9, "x")])
        for name, change in (("edited file", edit), ("new file", add)):
            with self.subTest(change=name):
                shutil.rmtree(self.fx.frozen / "T1", ignore_errors=True)
                change(self.freeze())
                with self.assertRaises(ConfigError):
                    InputSet(self.fx.cfg(), "T1")
                with self.assertRaises(ConfigError):
                    capture(self.fx, input_set="T1")
        self.assertEqual(manifests(self.fx), [])
        shutil.rmtree(self.fx.frozen / "T1")
        self.freeze(static={"toy_q": {}})                  # a static label the app does not have
        with self.assertRaises(ConfigError):
            InputSet(self.fx.cfg(), "T1")
        (self.fx.frozen / "T2").mkdir(parents=True)        # no freeze.json
        with self.assertRaises(ConfigError):
            InputSet(self.fx.cfg(), "T2")


@unittest.skipUnless(os.name == "nt", "junctions (mklink /J) are Windows'")
class TestJunctions(T.ToyCase):
    def junction(self, link):
        elsewhere = Path(tempfile.mkdtemp(prefix="llmw-out-"))
        self.addCleanup(T.force_rmtree, elsewhere)
        r = subprocess.run(["cmd", "/c", "mklink", "/J", link, elsewhere], capture_output=True, text=True)
        if r.returncode != 0:
            self.skipTest("cannot create a junction here")
        self.addCleanup(lambda: link.exists(follow_symlinks=False) and link.rmdir())

    def test_junction_below_the_data_root_is_refused(self):
        """data_root is inside the parent but raw/ is a junction that leads out of it."""
        link = self.fx.data_root / "raw"
        self.junction(link)
        with mock.patch.object(run.paths, "DATA_PARENT", self.fx.base):
            with self.assertRaises(ConfigError):
                Config(self.fx.config_path)
            link.rmdir()                        # removes the junction, not its target
            Config(self.fx.config_path)

    def test_index_folder_junction_does_not_change_capture_acceptance(self):
        """<data_root>/index belongs to the search side, which refuses a junction there itself; capture accepts
        what llm-wiki accepted (it never looked at that folder)."""
        self.junction(self.fx.data_root / "index")
        with mock.patch.object(run.paths, "DATA_PARENT", self.fx.base):
            Config(self.fx.config_path)
            with self.assertRaises(index.UsageError):
                index.load_data_root(self.fx.config_path)


GIT_FOREIGN = {"GIT_TEST_ASSUME_DIFFERENT_OWNER": "1"}      # git's own test knob: every repo looks foreign


def _git(*args, env=None):
    return subprocess.run(["git", *args], capture_output=True, text=True, env=env)


class TestProvenance(unittest.TestCase):

    def test_git_failure_is_never_clean(self):
        with mock.patch.object(run.subprocess, "run", side_effect=OSError(2, "no git")):
            head, dirty = run.repo_info()
        self.assertEqual((head, dirty), (run._head_from_files(paths.REPO), True))


class TestGitUnderSystem(unittest.TestCase):
    """As SYSTEM git refuses an admin-owned repo (dubious ownership, rc 128). The work-tree precheck must still
    refuse, provenance must still come from git, and status must not take index.lock."""

    @classmethod
    def setUpClass(cls):
        if _git("-C", T.REPO, "rev-parse", "--verify", "-q", "HEAD").returncode != 0:
            raise unittest.SkipTest("this checkout has no commit yet")
        foreign = dict(os.environ, **GIT_FOREIGN)
        if _git("-C", T.REPO, "rev-parse", "HEAD", env=foreign).returncode != 128:
            raise unittest.SkipTest("this git ignores GIT_TEST_ASSUME_DIFFERENT_OWNER")

    def setUp(self):
        self.base = Path(tempfile.mkdtemp(prefix="llmw-git-"))
        self.addCleanup(T.force_rmtree, self.base)
        self.repo = self.base / "repo"
        self.inside = self.repo / "data"
        self.outside = self.base / "out"
        for d in (self.inside, self.outside):
            d.mkdir(parents=True)
        self.assertEqual(_git("init", "-q", self.repo).returncode, 0)

    def test_work_tree_check(self):
        self.assertTrue(precheck.in_work_tree(self.inside))
        self.assertFalse(precheck.in_work_tree(self.outside))
        with mock.patch.dict(os.environ, GIT_FOREIGN):
            self.assertTrue(precheck.in_work_tree(self.inside))
            self.assertFalse(precheck.in_work_tree(self.outside))

    def test_work_tree_check_fails_closed(self):
        with mock.patch.object(precheck.subprocess, "run", side_effect=FileNotFoundError(2, "no git")):
            self.assertTrue(precheck.in_work_tree(self.outside))
        failed = subprocess.CompletedProcess([], 128, "", "fatal: something else\n")
        with mock.patch.object(precheck.subprocess, "run", return_value=failed):
            self.assertTrue(precheck.in_work_tree(self.outside))

    def test_provenance_rc128_is_never_clean(self):
        refused = subprocess.CompletedProcess([], 128, "", "fatal: detected dubious ownership\n")
        with mock.patch.object(run.subprocess, "run", return_value=refused):
            self.assertEqual(run.repo_info(), (run._head_from_files(run.paths.REPO), True))

    def test_dirty_is_the_cores_the_settings_and_the_selected_apps(self):
        """In a project of several apps another app's change is not this ledger's code (run.code_paths); with no
        app selected (the toy apps of these tests) the whole project counts."""
        proj = self.repo / "proj"
        for d in ("one", "two", "config", "rawlayer", "appkit"):
            (proj / d).mkdir(parents=True)
        if _git("-C", self.repo, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q",
                "--allow-empty", "-m", "x").returncode != 0:
            self.skipTest("cannot commit in a scratch repository")
        head = _git("-C", self.repo, "rev-parse", "HEAD").stdout.strip()

        def put(rel):
            p = proj.joinpath(*rel.split("/"))
            p.write_text("x", encoding="utf-8")
            return p
        with mock.patch.object(run.paths, "REPO", proj), mock.patch.dict(os.environ, GIT_FOREIGN), \
                mock.patch.object(run, "_head_from_files", return_value="sentinel"), \
                mock.patch.object(registry, "selected", lambda: "one"):
            self.assertEqual(run.code_paths()[-2:], ["config", "one"])
            put("two/x.py")
            put("notes.md")
            self.assertEqual(run.repo_info(), (head, False))
            for rel in ("one/y.py", "config/settings.py", "rawlayer/z.py", "manage.py", "appkit/archive.py"):
                with self.subTest(changed=rel):
                    p = put(rel)
                    self.assertEqual(run.repo_info(), (head, True))
                    p.unlink()
            with mock.patch.object(registry, "selected", lambda: None):
                self.assertEqual(run.code_paths(), ["."])
                self.assertEqual(run.repo_info(), (head, True))


if __name__ == "__main__":
    unittest.main()
