# -*- coding: utf-8 -*-
"""Preconditions, on the toy app: config refusals (SPEC 2, 16) and the apps' options, the data parent allowlist and
junctions (SPEC 10.1), the work-tree precheck, and provenance
that is never clean when git fails or refuses (SYSTEM's git ownership rules)."""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from rawlayer import registry
from rawlayer.capture import paths, precheck, run
from rawlayer.capture.config import Config, ConfigError
from rawlayer.search import index
from tests.core import toyfix as T
from tests.core.toyfix import capture, cli_main


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
        """data_root exists and is absolute; keys nobody reads (inputs_root of the removed frozen input sets
        among them) are accepted and ignored."""
        missing = self.fx.base / "nope"
        for v in (missing, "relative"):
            with self.subTest(key="data_root", value=v):
                self.fx.write_config()
                self.refused(data_root=v)
        (self.fx.base / "a_file").write_bytes(b"")
        self.fx.write_config()
        self.refused(data_root=self.fx.base / "a_file")
        for k, v in (("old_tool", missing / "tool.py"), ("old_dir", missing), ("old_tool", 5),
                     ("inputs_root", "relative")):
            with self.subTest(key=k, value=v):
                self.fx.write_config(old_tool="x", old_dir="y")
                self.fx.set_cfg(**{k: v})
                Config(self.fx.config_path)
        self.fx.write_config(old_tool="x", old_dir="y")
        self.assertEqual(capture(self.fx), 0)
        self.fx.set_cfg(old_tool=None, old_dir=None, inputs_root=None)
        self.assertEqual(capture(self.fx), 0)

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
        with mock.patch.object(paths, "DATA_PARENT", elsewhere):
            with self.assertRaises(ConfigError):
                Config(self.fx.config_path)
            for argv in (["capture"], ["replay"]):
                with self.subTest(command=argv[0]):
                    self.assertEqual(cli_main(self.fx, *argv), 2)
        self.assertEqual(T.names(self.fx.data_root), before)
        self.assertTrue(paths.inside(paths.DATA_PARENT / "x", paths.DATA_PARENT))
        self.assertFalse(paths.inside(paths.DATA_PARENT, paths.DATA_PARENT))
        self.assertFalse(paths.inside(Path(f"{paths.DATA_PARENT}-x"), paths.DATA_PARENT))

    def test_credential_path_in_the_data_roots_folder(self):
        """A token in a data root under <DATA_PARENT>/raw is accepted; a data root's archive/ and raw/ are not."""
        from appkit.archive import FetchError, credential_path
        parent = self.fx.base / "parent"
        with mock.patch.object(paths, "DATA_PARENT", parent), mock.patch.object(precheck, "check"):
            self.assertEqual(paths.data_root("app"), parent / "raw" / "app")
            for good in ("raw/app/token.txt", "app/token.txt"):
                self.assertEqual(credential_path(str(parent / good)), parent / good)
            for bad in ("raw/app/raw/token.txt", "raw/app/archive/token.txt", "app/raw/token.txt"):
                with self.subTest(bad=bad), self.assertRaises(FetchError):
                    credential_path(str(parent / bad))

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
