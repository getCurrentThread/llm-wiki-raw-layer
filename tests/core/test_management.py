# -*- coding: utf-8 -*-
"""The project around the core: apps found next to manage.py (registry.discover), one app selected per process
(registry.select), manage.py's dispatch of the core's commands, and the ledger ownership guard (one app, one
ledger). Every project here is a throwaway folder (projectfix.Project)."""
import contextlib
import io
import json
import os
import types
import unittest
from unittest import mock

from rawlayer import management, registry
from rawlayer.capture import cli, ledger, paths
from rawlayer.search import common
from tests.core import toyfix as T
from tests.core.projectfix import Project, app_files


def run(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = management.main(list(argv))
    return rc, out.getvalue(), err.getvalue()


class TestDiscover(unittest.TestCase):
    def test_what_is_an_app(self):
        apps = {"good": app_files("good"), "second": app_files("second"), "excluded": app_files("excluded"),
                "tests": app_files("tests"), "config": app_files("config"), "Upper": app_files("upper"),
                "noapps": {"__init__.py": ""}, "noinit": {"apps.py": ""}, "_private": app_files("private"),
                "dash-name": app_files("dash")}
        with Project(apps, exclude=["excluded"]) as proj:
            (proj.dir / ".hidden").mkdir()
            self.assertEqual(registry.discover(), ["good", "second"])

    def test_nothing_is_imported(self):
        with Project({"broken": app_files("broken", extra={"apps.py": "raise RuntimeError('x')\n"})}):
            self.assertEqual(registry.discover(), ["broken"])
            with self.assertRaises(RuntimeError):
                registry.app_registry("broken")


class TestSelect(unittest.TestCase):
    def test_one_app_per_process(self):
        with Project({"one": app_files("one", "(ToyA,)"), "two": app_files("two", "(ToyB,)")}):
            self.assertIsNone(registry.selected())
            with self.assertRaisesRegex(registry.RegistryError, "several apps"):
                registry.get()
            reg = registry.select("one")
            self.assertEqual((registry.selected(), registry.get(), registry.origin_order()), ("one", reg, ("toy_a",)))
            self.assertIs(registry.select("one"), reg)
            with self.assertRaisesRegex(registry.RegistryError, "one is already selected"):
                registry.select("two")
            self.assertEqual(registry.app_registry("two").origin_order, ("toy_b",))     # loaded, not installed
            self.assertEqual(registry.origin_order(), ("toy_a",))

    def test_a_single_app_project_needs_no_name(self):
        with Project({"only": app_files("only", "(ToyM,)")}):
            self.assertEqual(registry.origin_order(), ("toy_m",))
            self.assertEqual(registry.selected(), "only")
        with Project({}):
            with self.assertRaisesRegex(registry.RegistryError, "no app"):
                registry.get()

    def test_the_contract_of_a_project_app(self):
        with Project({"named": app_files("other")}):
            with self.assertRaisesRegex(registry.RegistryError, "label must be the folder name"):
                registry.select("named")
            with self.assertRaisesRegex(registry.RegistryError, "no app 'absent'"):
                registry.select("absent")
            rc, out, _err = run("check")
            self.assertEqual(rc, 1)
            self.assertIn("named: app contract broken (named: AppConfig.label must be the folder name)", out)
            self.assertEqual(run("collect", "named")[0], 1)
        # a package of the app's name imported from elsewhere first (an installed package, say) is not the app
        with Project({"shadowed": app_files("shadowed")}):
            fake = types.ModuleType("shadowed")
            fake.__file__ = str(T.TEST_PARENT / "elsewhere" / "shadowed" / "__init__.py")
            with mock.patch.dict("sys.modules", {"shadowed": fake}), \
                    mock.patch.object(registry, "Registry", lambda names: types.SimpleNamespace(apps=())):
                with self.assertRaisesRegex(registry.RegistryError, "not the folder next to manage.py"):
                    registry.app_registry("shadowed")


class TestDispatch(unittest.TestCase):
    def test_a_core_command_names_the_app(self):
        with Project({"a1": app_files("a1"), "a2": app_files("a2")}):
            rc, _out, err = run("collect")
            self.assertEqual(rc, 2)
            self.assertIn("name the app: manage.py collect <app>", err)
            self.assertIsNone(registry.selected())
            rc, _out, err = run("collect", "nope")
            self.assertEqual(rc, 2)
            self.assertIsNone(registry.selected())

    def test_an_app_that_does_not_load(self):
        with Project({"bad": app_files("bad", extra={"origins.py": "import not_a_module_anywhere\n"})}):
            rc, _out, err = run("search", "bad")
            self.assertEqual(rc, 1)
            self.assertIn("app bad does not load (error ModuleNotFoundError", err)
            rc, out, _err = run("check")
            self.assertEqual(rc, 1)
            self.assertIn("bad: does not load (error ModuleNotFoundError not_a_module_anywhere", out)

    def test_unknown_command_and_help(self):
        with Project({}):
            self.assertEqual(run("nope")[0], 2)
            self.assertIn("unknown command", run("nope")[2])
            self.assertEqual(run()[0], 2)
            self.assertEqual(run("--help")[0], 0)
            for name in ("collect", "check", "schedule", "test", "setup"):
                self.assertIn(f"manage.py {name}", run("help", name)[1])
            self.assertEqual(run("help", "nope")[0], 2)


class TestOwnership(T.ToyCase):
    """One app owns one ledger: what another app committed is never read or written by this one."""

    def setUp(self):
        super().setUp()
        self.assertEqual(ledger.foreign_origins(self.fx.data_root / "raw"), [])     # no ledger yet
        self.assertEqual(T.capture(self.fx), 0)

    def test_foreign_origins(self):
        raw = self.fx.data_root / "raw"
        self.assertEqual(ledger.foreign_origins(raw), [])
        with mock.patch.object(registry, "origin_order", lambda: ("toy_a",)):
            self.assertEqual(ledger.foreign_origins(raw), ["toy_b", "toy_m"])
        empty = self.fx.base / "empty"
        self.assertEqual(ledger.foreign_origins(empty), [])
        (empty / "manifests").mkdir(parents=True)
        self.assertEqual(ledger.foreign_origins(empty), [])

    def test_capture_replay_index_and_search_refuse_another_apps_ledger(self):
        """The refusal is the public one of every refused config (its reason is not printed); the same command on
        the app's own ledger goes through."""
        raw = self.fx.data_root / "raw"
        before = T.names(raw / "manifests")
        argv = {"capture": ["--input-set", "live"], "replay": ["--check"],
                "export-state": ["--out", str(raw / ".verify" / "state.json")]}
        with mock.patch.object(registry, "origin_order", lambda: ("toy_a",)):
            for command, extra in argv.items():
                with self.subTest(command=command):
                    out = io.StringIO()
                    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                        rc = cli.main([command, "--config", str(self.fx.config_path), *extra])
                    self.assertEqual(rc, 2)
                    self.assertIn("refused", out.getvalue())
            with self.assertRaisesRegex(common.UsageError, "belongs to another app"):
                common.load_data_root(self.fx.config_path)
        self.assertEqual(T.names(raw / "manifests"), before)
        self.assertEqual(T.cli_main(self.fx, "replay", "--check"), 0)
        self.assertEqual(common.load_data_root(self.fx.config_path), self.fx.data_root)


class TestSetup(unittest.TestCase):
    """manage.py setup <app> on a throwaway data parent: the folders, the protected ACL, config.json from the
    example or from another config (stdin), and what is left to fill in."""

    EXAMPLE = {"v": 1, "impl": "x", "data_root": "<DATA_PARENT>/toyish", "sources": {"toy_dir": "<abs dir>"},
               "toy_system": False}

    def setUp(self):
        self.parent = T.TEST_PARENT / f"llmw-setup-{os.getpid()}-{id(self)}" / "data parent"
        self.addCleanup(T.force_rmtree, self.parent.parent)
        p = mock.patch.object(paths, "DATA_PARENT", self.parent)
        p.start()
        self.addCleanup(p.stop)
        self.toy = self.parent.parent / "in" / "toy"
        self.toy.mkdir(parents=True)
        self.files = app_files("toyish", "(ToyA, ToyB, ToyM)", extra={
            "apps.py": ("from tests.core.toyapp.apps import ToyConfig\n\n\n"
                        "class Config(ToyConfig):\n    label = 'toyish'\n"),
            "inputs.py": "from tests.core.toyapp.inputs import resolve, static_roots  # noqa: F401\n",
            "config.example.json": json.dumps(self.EXAMPLE)})

    def setup(self, *argv, stdin=None):
        with mock.patch("sys.stdin", io.StringIO(stdin or "")), \
                mock.patch("rawlayer.management.commands.setup.search_problem", lambda folder: None):
            return run("setup", "toyish", *argv)

    def config(self):
        with (self.parent / "toyish" / "config.json").open(encoding="utf-8") as fh:
            return json.load(fh)

    def test_from_the_example_names_what_to_fill_in(self):
        with Project({"toyish": self.files}):
            rc, out, _err = self.setup()
            self.assertEqual(rc, 3)
            self.assertIn("data parent created", out)
            self.assertIn("fill in config.json: sources.toy_dir", out)
            cfg = self.config()
            self.assertEqual((cfg["v"], cfg["impl"], cfg["data_root"]),
                             (1, paths.IMPL_NAME, str(self.parent / "toyish")))
            self.assertEqual(cfg["inputs_root"], str(self.parent / "inputs"))
            if os.name == "nt":
                from rawlayer.management.commands import setup  # noqa: PLC0415
                self.assertIsNone(setup.acl_problem(self.parent))      # what capture's precheck wants
            rc, _out, err = self.setup()
            self.assertEqual(rc, 2)
            self.assertIn("already there", err)

    def test_from_another_config_on_stdin(self):
        other = {"v": 1, "impl": "elsewhere", "data_root": str(T.TEST_PARENT / "old" / "place"),
                 "sources": {"toy_dir": str(self.toy)}, "toy_system": True}
        with Project({"toyish": self.files}):
            rc, out, _err = self.setup("--from", "-", stdin=json.dumps(other))
            self.assertEqual(rc, 0, out)
            self.assertIn("toyish: config ok", out)
            self.assertNotIn(str(self.toy), out)                    # values are never printed
            cfg = self.config()
            self.assertEqual((cfg["impl"], cfg["sources"], cfg["toy_system"]), (paths.IMPL_NAME, other["sources"], True))
            self.assertEqual(self.setup("--from", "-", "--force", stdin="[]")[0], 2)
            self.assertEqual(self.setup("--from", "-", "--force", stdin="{")[0], 2)

    def test_placeholders(self):
        from rawlayer.management.commands import setup  # noqa: PLC0415
        self.assertEqual(setup.placeholders({"a": "<x>", "b": {"c": ["ok", "<y>"]}, "d": 1}), ["a", "b.c"])


if __name__ == "__main__":
    unittest.main()
