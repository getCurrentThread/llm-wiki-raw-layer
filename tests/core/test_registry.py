# -*- coding: utf-8 -*-
"""The app registry (rawlayer.registry): what it derives from the installed apps, what it refuses at load (the
app contract), use() for tests, and publicout's vocabulary (discovery, selection and manage.py: test_management)."""
import contextlib
import os
import sys
import types
import unittest
from pathlib import Path

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
             configs=1, resolve=None, wiki=None, archive=None, inputs=True, vocab=True):
        pkg = "fakeapps_" + name
        mods = {pkg: types.ModuleType(pkg)}
        apps = mods[pkg + ".apps"] = types.ModuleType(pkg + ".apps")
        for i in range(configs):
            cls = type(f"Cfg{i}", (AppConfig,), {"label": label or name, "source_keys": tuple(source_keys),
                                                 "options": tuple(options), "wiki": wiki,
                                                 "archive": archive, "__module__": pkg + ".apps"})
            setattr(apps, cls.__name__, cls)
        mods[pkg + ".origins"] = types.SimpleNamespace(ORIGINS=tuple(origins))
        if inputs:                                          # inputs.py and vocab.py are optional modules
            mods[pkg + ".inputs"] = types.SimpleNamespace(resolve=resolve or (lambda cfg: {}))
        if vocab:
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
            self.assertEqual([op.key for op in reg.options], ["toy_system", "toy_wiki_project", "toy_wiki_include",
                                                              "toy_wiki_exclude", "toy_wiki_since"])
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

    def test_refused(self):
        f, o, op = self.apps.fake, _origin("toy_x"), Option("same", 0, int)
        notorigin = type("NotOrigin", (), {"name": "toy_y", "__init__": lambda self, app: None})
        state = (("t", "CREATE TABLE IF NOT EXISTS t(a);"),), (("t", "CREATE TABLE IF NOT EXISTS t(b);"),)
        cases = {
            "no AppConfig": lambda: [f("none", configs=0)],
            "two AppConfigs": lambda: [f("two", configs=2)],
            "a label twice": lambda: [f("dup1", label="same"), f("dup2", label="same")],
            "a label's shape": lambda: [f("badlabel", label="Bad-Label")],
            "an origin twice": lambda: [f("o1", origins=[o]), f("o2", origins=[o])],
            "not an Origin": lambda: [f("notorigin", origins=[notorigin])],
            "day_kinds outside kinds": lambda: [f("days", origins=[_origin("toy_d", day_kinds=frozenset({"other"}))])],
            "a core complete_status": lambda: [f("complete", origins=[_origin("toy_c", complete_status="clean")])],
            "an own run()": lambda: [f("run", origins=[_origin("toy_r", run=lambda self, ctx: None)])],
            "a source key's shape": lambda: [f("sk", source_keys=["toy dir"])],
            "a source key twice": lambda: [f("sk1", source_keys=["k"]), f("sk2", source_keys=["k"])],
            "an option twice": lambda: [f("op1", options=[op]), f("op2", options=[op])],
            "a core key as option": lambda: [f("opcore", options=[Option("data_root", None, str)])],
            "a wiki key taken": lambda: [f("wka", options=[Option("w_wiki_since", "", str)], wiki=("w", "chat"))],
            "a wiki prefix twice": lambda: [f("wkb", wiki=("w", "chat")), f("wkc", wiki=("w", "mail"))],
            "an archive key taken": lambda: [f("arc1", archive="ar", inputs=False), f("arc2", archive="ar", inputs=False)],
            "an archive prefix's shape": lambda: [f("arc3", archive="Bad-Prefix")],
            "a search SPEC named otherwise": lambda: [f("srch", search=types.SimpleNamespace(
                SPEC=types.SimpleNamespace(name="other")))],
            "a vocabulary word's shape": lambda: [f("words", words=["two words"])],
            "one state table, two DDLs": lambda: [f("tab", origins=[_origin("toy_t", state_tables=state[0]),
                                                                    _origin("toy_u", state_tables=state[1])])],
            "a core state table": lambda: [f("core", origins=[_origin("toy_v", state_tables=(("heads", "x"),))])],
        }
        for bad in ("Toy", "toy-x", "toy1", "", "x" * 41):
            cases[f"origin name {bad!r}"] = lambda bad=bad: [f("bad_" + str(len(bad)) + bad.replace("-", "_"),
                                                              origins=[_origin(bad)])]
        for i, bad in enumerate(("w", ("w",), ("w", "chat", 1), ("w", "web"), ("W", "chat"), ("1w", "chat"),
                                 (3, "chat"), ("toolongprefix", "chat"), ["w", "chat"], ("", "chat"))):
            cases[f"wiki {bad!r}"] = lambda i=i, bad=bad: [f(f"wk{i}", wiki=bad)]
        for why, names in cases.items():
            with self.subTest(why):
                self.refused(*names())

    def test_wiki_declaration(self):
        """AppConfig.wiki = (prefix, layout) adds the four export keys to the app's options; a None prefix adds none."""
        a = self.apps.fake("wk1", options=[Option("own", 0, int)], wiki=("w", "mail"))
        b = self.apps.fake("wk2", wiki=(None, "page"))
        reg = registry.Registry([a, b])
        self.assertEqual([op.key for op in reg.options], ["own", "w_wiki_project", "w_wiki_include", "w_wiki_exclude",
                                                          "w_wiki_since"])
        self.assertEqual([x.wiki for x in reg.apps], [("w", "mail"), (None, "page")])
        self.assertEqual(AppConfig.wiki, None)
        project, include, exclude, since = reg.options[1:]
        root = os.path.abspath(os.sep)
        self.assertEqual((project.default, include.default, exclude.default, since.default), ("", [], [], ""))
        self.assertEqual((project.check(root), project.check(""), include.check(["a"]), since.check("2026-01")),
                         (root, "", ["a"], "2026-01"))
        for op, bad in ((project, "relative"), (project, 3), (since, "2026-13"), (since, "2026"), (include, [""]),
                        (exclude, "x")):
            with self.assertRaises(ValueError):
                op.check(bad)

    def test_inputs_and_vocab_are_optional(self):
        app = registry.Registry([self.apps.fake("bare", inputs=False, vocab=False)]).apps[0]
        self.assertEqual(app.inputs.resolve(None), {})
        self.assertEqual((app.vocab.WORDS, app.vocab.PATTERNS), ((), ()))

    def test_archive_declaration(self):
        """AppConfig.archive = p adds the source key <p>_archive and, without an inputs.py, that input attribute (the
        config's path); an inputs.py replaces it."""
        pkg = self.apps.fake("arc", archive="ar", source_keys=["other"], inputs=False)
        reg = registry.Registry([pkg])
        self.assertEqual(reg.source_keys, ("ar_archive", "other"))
        inputs = reg.apps[0].inputs
        cfg = types.SimpleNamespace(sources={"ar_archive": os.path.join(os.sep, "x", "y")})
        self.assertEqual(inputs.resolve(cfg), {"ar_archive": Path(os.sep, "x", "y")})
        own = registry.Registry([self.apps.fake("arc4", archive="aq", resolve=lambda cfg: {"aq_dir": 1})])
        self.assertEqual(own.apps[0].inputs.resolve(None), {"aq_dir": 1})

    def test_input_attribute_claimed_twice(self):
        a = self.apps.fake("in1", resolve=lambda cfg: {"toy_root": "x"})
        b = self.apps.fake("in2", resolve=lambda cfg: {"toy_root": "y"})
        with registry.use([a, b]):
            with self.assertRaises(RegistryError):
                InputSet(None)


if __name__ == "__main__":
    unittest.main()
