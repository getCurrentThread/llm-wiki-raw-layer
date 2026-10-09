# -*- coding: utf-8 -*-
"""The app registry (rawlayer.registry): what it derives from the installed apps, what it refuses at load (the
app contract), use() for tests, and publicout's vocabulary (discovery, selection and manage.py: test_management)."""
import contextlib
import sys
import types
import unittest

from rawlayer import registry
from rawlayer.capture import publicout
from rawlayer.capture.config import InputSet
from rawlayer.capture.origins import HIST_DDL, MultiVersionMixin, Origin
from rawlayer.registry import AppConfig, Option, RegistryError
from tests.core import toyfix as T


@contextlib.contextmanager
def no_registry():
    """A block that starts with no registry loaded; whatever was installed before is put back afterwards."""
    prev = registry._current
    registry._current = None
    try:
        yield
    finally:
        if prev is not None:
            registry._install(prev)
        else:
            registry._current = None
            publicout.uninstall()


def _origin(name, **attrs):
    attrs.setdefault("kinds", frozenset({"item"}))
    return type("O_" + name, (Origin,), dict(name=name, **attrs))


class FakeApps:
    """Apps built as modules in sys.modules (no files): fake(name, ...) -> the package name to install."""

    def __init__(self):
        self.names = []

    def fake(self, name, label=None, origins=(), source_keys=(), options=(), words=(), patterns=(), search=None,
             configs=1, resolve=None):
        pkg = "fakeapps_" + name
        mods = {pkg: types.ModuleType(pkg)}
        apps = mods[pkg + ".apps"] = types.ModuleType(pkg + ".apps")
        for i in range(configs):
            cls = type(f"Cfg{i}", (AppConfig,), {"label": label or name, "source_keys": tuple(source_keys),
                                                 "options": tuple(options), "__module__": pkg + ".apps"})
            setattr(apps, cls.__name__, cls)
        mods[pkg + ".origins"] = types.SimpleNamespace(ORIGINS=tuple(origins))
        mods[pkg + ".inputs"] = types.SimpleNamespace(resolve=resolve or (lambda cfg, n, f: {}),
                                                      static_roots=lambda inputs: {})
        mods[pkg + ".vocab"] = types.SimpleNamespace(WORDS=tuple(words), PATTERNS=tuple(patterns))
        if search is not None:
            mods[pkg + ".search"] = search
        for k, v in mods.items():
            sys.modules[k] = v
        self.names.extend(mods)
        return pkg

    def clean(self):
        for k in self.names:
            sys.modules.pop(k, None)


class Base(unittest.TestCase):
    def setUp(self):
        self.apps = FakeApps()
        self.addCleanup(self.apps.clean)

    def refused(self, *names):
        with self.assertRaises(RegistryError):
            registry.Registry(list(names))


class TestDerived(unittest.TestCase):
    def test_toy_registry(self):
        with registry.use([T.TOY]) as reg:
            self.assertIs(registry.get(), reg)
            self.assertEqual(registry.origin_order(), ("toy_a", "toy_b", "toy_m"))
            self.assertEqual([type(o).__name__ for o in registry.origins()], ["ToyA", "ToyB", "ToyM"])
            self.assertEqual(registry.statuses(), registry.CORE_STATUSES + ("clean", "partial", "unreadable"))
            self.assertEqual([t for t, _ddl in registry.state_tables()], ["toy_last", "hist"])
            self.assertEqual(reg.source_keys, ("toy_dir",))
            self.assertEqual([op.key for op in reg.options], ["toy_system"])
            self.assertEqual(list(registry.search_modules()), ["toy"])
            self.assertEqual(registry.origin("toy_b").name, "toy_b")
            self.assertIsNone(registry.origin("nope"))
            self.assertEqual(registry.apps()[0].label, "toy")
            self.assertIs(registry.origins()[0].app, registry.apps()[0])
            for w in ("toy_a", "toy_m", "item", "rec", "clean", "unreadable", "bad_line", T.paths.IMPL_NAME):
                self.assertTrue(publicout.ok_str(w), w)
            self.assertTrue(publicout.ok_str("toy:toy_b:12"))
            self.assertFalse(publicout.ok_str("toy:toy_b:A1"))
            self.assertFalse(publicout.ok_str("somebody"))

    def test_installed_apps_order_the_origins_and_indexes(self):
        with registry.use([T.TOY, T.TOY2]):
            self.assertEqual(list(registry.search_modules()), ["toy", "toy2"])
            self.assertEqual(registry.origin_order(), ("toy_a", "toy_b", "toy_m"))
            self.assertEqual([a.label for a in registry.apps()], ["toy", "toy2"])

    def test_use_restores_the_previous_registry(self):
        with registry.use([T.TOY]) as outer:
            with registry.use([T.TOY2]):
                self.assertEqual(registry.origin_order(), ())
                self.assertFalse(publicout.ok_str("toy_a"))
            self.assertIs(registry.get(), outer)
            self.assertTrue(publicout.ok_str("toy_a"))

    def test_use_without_a_previous_registry_uninstalls(self):
        with no_registry():
            with registry.use([T.TOY]):
                self.assertTrue(publicout.ok_str("toy_a"))
            self.assertIsNone(registry._current)
            self.assertFalse(publicout._installed)

    def test_shared_table_with_the_same_ddl_is_one_table(self):
        apps = FakeApps()
        try:
            a = apps.fake("hist1", origins=[type("M1", (MultiVersionMixin, Origin), {"name": "m_one", "kinds": {"r"}})])
            b = apps.fake("hist2", origins=[type("M2", (MultiVersionMixin, Origin), {"name": "m_two", "kinds": {"r"}})])
            self.assertEqual(registry.Registry([a, b]).state_tables, (("hist", HIST_DDL),))
        finally:
            apps.clean()


class TestRefusals(Base):
    """The app contract, checked when the registry loads (Django's system checks)."""

    def test_app_config_count(self):
        self.refused(self.apps.fake("none", configs=0))
        self.refused(self.apps.fake("two", configs=2))

    def test_labels(self):
        self.refused(self.apps.fake("dup1", label="same"), self.apps.fake("dup2", label="same"))
        self.refused(self.apps.fake("badlabel", label="Bad-Label"))

    def test_origin_names(self):
        o = _origin("toy_x")
        self.refused(self.apps.fake("o1", origins=[o]), self.apps.fake("o2", origins=[o]))
        for bad in ("Toy", "toy-x", "toy1", "", "x" * 41):
            with self.subTest(name=bad):
                self.refused(self.apps.fake("bad_" + str(len(bad)) + bad.replace("-", "_"), origins=[_origin(bad)]))
        notorigin = type("NotOrigin", (), {"name": "toy_y", "__init__": lambda self, app: None})
        self.refused(self.apps.fake("notorigin", origins=[notorigin]))

    def test_kind_sets(self):
        self.refused(self.apps.fake("days", origins=[_origin("toy_d", day_kinds=frozenset({"other"}))]))
        self.refused(self.apps.fake("complete", origins=[_origin("toy_c", complete_status="clean")]))

    def test_run_is_the_cores(self):
        o = _origin("toy_r", run=lambda self, ctx: None)
        self.refused(self.apps.fake("run", origins=[o]))

    def test_source_keys(self):
        self.refused(self.apps.fake("sk", source_keys=["toy dir"]))
        self.refused(self.apps.fake("sk1", source_keys=["k"]), self.apps.fake("sk2", source_keys=["k"]))

    def test_option_keys(self):
        op = Option("same", 0, int)
        self.refused(self.apps.fake("op1", options=[op]), self.apps.fake("op2", options=[op]))
        self.refused(self.apps.fake("opcore", options=[Option("data_root", None, str)]))

    def test_search_name_is_the_label(self):
        search = types.SimpleNamespace(SPEC=types.SimpleNamespace(name="other"))
        self.refused(self.apps.fake("srch", search=search))

    def test_vocabulary_words(self):
        self.refused(self.apps.fake("words", words=["two words"]))

    def test_state_tables(self):
        t1 = _origin("toy_t", state_tables=(("t", "CREATE TABLE IF NOT EXISTS t(a);"),))
        t2 = _origin("toy_u", state_tables=(("t", "CREATE TABLE IF NOT EXISTS t(b);"),))
        self.refused(self.apps.fake("tab", origins=[t1, t2]))
        self.refused(self.apps.fake("core", origins=[_origin("toy_v", state_tables=(("heads", "x"),))]))

    def test_input_attribute_claimed_twice(self):
        a = self.apps.fake("in1", resolve=lambda cfg, n, f: {"toy_root": "x"})
        b = self.apps.fake("in2", resolve=lambda cfg, n, f: {"toy_root": "y"})
        cfg = types.SimpleNamespace(check_input_set=lambda name: None)
        with registry.use([a, b]):
            with self.assertRaises(RegistryError):
                InputSet(cfg, "live")
        c = self.apps.fake("in3", resolve=lambda cfg, n, f: {"name": "x"})       # a core attribute
        with registry.use([c]):
            with self.assertRaises(RegistryError):
                InputSet(cfg, "live")


if __name__ == "__main__":
    unittest.main()
