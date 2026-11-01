# -*- coding: utf-8 -*-
"""rawlayer/collect.py and `manage.py collect`: the three layouts on a stand-in driver, closed months packed and the
open month held back, include/since, a rerun writing nothing, orphans kept until --prune, the refusals, the toy apps'
wiki declarations through the command, and the refusal to run as LocalSystem. Synthetic data only; no real app."""
import contextlib
import datetime
import importlib
import io
import json
import shutil
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from rawlayer import collect as W
from rawlayer import registry
from rawlayer.capture import run as capture_run
from rawlayer.management.commands import collect as command
from rawlayer.search import index
from tests.core import toyfix as T

KST = datetime.timezone(datetime.timedelta(hours=9))
NOW = datetime.datetime(2026, 10, 15, 12, 0, tzinfo=KST).timestamp()
FOLDER = {"chat": "toy", "mail": "toy2", "page": "toy3"}       # the source folder of each layout in the project


def ts(*a):
    return int(datetime.datetime(*a, tzinfo=KST).timestamp())


class Driver:
    """changed_containers/chunk_units over {container: (label, units)}."""
    spec = types.SimpleNamespace(name="toy")

    def __init__(self, data):
        self.data = data

    def changed_containers(self, con, seq):
        return list(self.data)

    def chunk_units(self, con, c):
        return self.data[c]


CHAT = {
    "ch1": ("team › dev", [("p1", ts(2026, 9, 3, 14, 22), "alice: hi", False),
                           ("p2", ts(2026, 9, 3, 14, 23), "bob: two\nlines", False),
                           ("p3", ts(2026, 9, 4, 9, 0), "alice: next day", False),
                           ("p4", ts(2026, 10, 1, 9, 0), "alice: open month", False)]),
    "ch2": ("team › random", [("q1", ts(2026, 8, 20, 10, 0), "carol: old", False),
                              ("q2", ts(2026, 9, 20, 10, 0), "carol: kept?", False)]),
}


class Project(unittest.TestCase):
    """A throwaway LLM Wiki project (schema.md and wiki/) under the temp folder, which is the test data parent."""

    def setUp(self):
        self.project = Path(tempfile.mkdtemp(prefix="collect-", dir=T.TEST_PARENT))
        self.addCleanup(shutil.rmtree, self.project, True)
        (self.project / "schema.md").write_text("schema", encoding="utf-8")
        (self.project / "wiki").mkdir()

    def files(self, app):
        root = self.project / "raw" / "sources" / app
        return sorted(p.relative_to(root).as_posix() for p in root.rglob("*.md"))


class Export(Project):
    def run_export(self, kind, data, **kw):
        return W.export(FOLDER[kind], kind, Driver(data), None, self.project, now=NOW, **kw)

    def test_chat_files_pack_closed_months_and_hold_the_open_one(self):
        rep = self.run_export("chat", CHAT)
        dev, rnd = W.name("team › dev", "ch1"), W.name("team › random", "ch2")
        self.assertEqual(self.files("toy"), [f"{dev}/2026-09.md", f"{rnd}/2026-08.md"])
        self.assertEqual((rep["files"], rep["written"], rep["held"], rep["filtered"]), (2, 2, 1, 0))
        text = (self.project / "raw/sources/toy" / dev / "2026-09.md").read_text(encoding="utf-8")
        self.assertIn('container: "ch1"\n', text)
        self.assertIn('records: 3\n', text)
        self.assertIn("\n## 2026-09-03\n\n- 14:22 alice: hi\n- 14:23 bob: two\n  lines\n", text)
        self.assertIn("\n## 2026-09-04\n\n- 09:00 alice: next day\n", text)
        self.assertNotIn("open month", text)
        text = (self.project / "raw/sources/toy" / rnd / "2026-08.md").read_text(encoding="utf-8")
        self.assertIn('period: "2026-08..2026-09"\n', text)
        self.assertLess(text.index("## 2026-08-20"), text.index("## 2026-09-20"))

    def test_rerun_writes_nothing(self):
        self.run_export("chat", CHAT)
        path = next((self.project / "raw/sources/toy").rglob("2026-09.md"))
        mtime = path.stat().st_mtime_ns
        rep = self.run_export("chat", CHAT)
        self.assertEqual((rep["written"], rep["unchanged"], rep["orphans"]), (0, 2, 0))
        self.assertEqual(path.stat().st_mtime_ns, mtime)

    def test_include_and_since(self):
        rep = self.run_export("chat", CHAT, include=["DEV"], since="2026-09")
        self.assertEqual(self.files("toy"), [f"{W.name('team › dev', 'ch1')}/2026-09.md"])
        self.assertEqual(rep["filtered"], 2)        # ch2's two messages: not included

        rep = self.run_export("mail", {"a": ("Hi / Bot", [("k1", ts(2026, 9, 1), "x", False)]),
                                       "b": ("Hi / ann", [("k2", ts(2026, 9, 2), "y", False)])},
                              include=["hi"], exclude=["BOT"])
        self.assertEqual((rep["files"], rep["filtered"]), (1, 1))  # an exclude wins over an include

        rep = self.run_export("chat", CHAT, since="2026-09")
        rnd = W.name("team › random", "ch2")
        self.assertIn(f"{rnd}/2026-08.md", self.files("toy"))       # a file ending in September keeps August
        self.assertEqual((len(self.files("toy")), rep["filtered"]), (2, 0))

        rep = self.run_export("mail", {"m": ("S / x", [("k", ts(2026, 9, 1), "body", False)])}, since="2026-10")
        self.assertEqual((rep["files"], rep["filtered"]), (0, 1))  # its newest month is before since

    def test_mail_month_holds_a_section_per_mail(self):
        mails = {"m2": ("Second / bob", [("k2", ts(2026, 9, 9, 8, 0), "body two", False)]),
                 "m1": ("First / alice", [("k1", ts(2026, 9, 2, 8, 0), "para one", False),
                                          ("k1", ts(2026, 9, 2, 8, 0), "para two", False)]),
                 "m3": ("Now / carol", [("k3", ts(2026, 10, 2, 8, 0), "open", False)]),
                 "gone": ("", [])}
        rep = self.run_export("mail", mails)
        self.assertEqual(self.files("toy2"), ["2026-09.md"])
        self.assertEqual((rep["held"], rep["files"]), (1, 1))
        text = (self.project / "raw/sources/toy2/2026-09.md").read_text(encoding="utf-8")
        self.assertIn("records: 2\n", text)
        self.assertLess(text.index("## First / alice (2026-09-02 08:00)"), text.index("## Second / bob"))
        self.assertIn("\npara one\n\npara two\n", text)

    def test_page_keeps_headings_and_is_never_held(self):
        pages = {"pg": ("Space › Plan", [("b1", ts(2026, 3, 1), "Plan", True), ("b2", ts(2026, 10, 2), "text\nmore",
                                                                                False)])}
        rep = self.run_export("page", pages)
        self.assertEqual(self.files("toy3"), [f"{W.name('Space › Plan', 'pg')}.md"])
        self.assertEqual(rep["held"], 0)
        text = next((self.project / "raw/sources/toy3").glob("*.md")).read_text(encoding="utf-8")
        self.assertIn('period: "2026-10"\n', text)
        self.assertIn("\n## Plan\n\ntext\nmore\n", text)
        self.assertEqual(self.run_export("page", pages, since="2026-11")["files"], 0)

    def test_orphans_are_kept_until_prune(self):
        self.run_export("chat", CHAT)
        root = self.project / "raw/sources/toy"
        old = root / "renamed (00000000)" / "2026-01.md"
        old.parent.mkdir()
        old.write_text("x", encoding="utf-8")
        other = self.project / "raw/sources/toy2/keep.md"
        other.parent.mkdir(parents=True)
        other.write_text("x", encoding="utf-8")
        dry = self.run_export("chat", CHAT, prune=True, dry_run=True)
        self.assertEqual(dry["orphans"], 1)
        self.assertTrue(old.exists())
        self.assertEqual(self.run_export("chat", CHAT)["orphans"], 1)
        self.assertTrue(old.exists())
        rep = self.run_export("chat", CHAT, prune=True)
        self.assertEqual((rep["orphans"], rep["pruned"]), (1, 1))
        self.assertFalse(old.parent.exists())
        self.assertTrue(other.exists())
        self.assertEqual(len(self.files("toy")), 2)

    def test_prune_refuses_a_stale_or_empty_index(self):
        self.run_export("chat", CHAT)
        before = self.files("toy")
        for data, kw in (({}, {}), (CHAT, {"stale": True})):
            with self.subTest(stale=bool(kw)), self.assertRaisesRegex(W.Refused, "--prune"):
                self.run_export("chat", data, prune=True, **kw)
            self.assertEqual(self.files("toy"), before)
        self.assertEqual(self.run_export("chat", {}, prune=True, dry_run=True)["orphans"], 2)
        self.assertEqual(self.run_export("chat", {})["orphans"], 2)         # without --prune: kept, counted

    def test_dry_run_writes_nothing(self):
        rep = self.run_export("chat", CHAT, dry_run=True)
        self.assertEqual((rep["files"], rep["written"]), (2, 2))
        self.assertFalse((self.project / "raw").exists())

    def test_a_path_leaving_the_source_folder_is_refused(self):
        root = self.project / "raw" / "sources" / "toy"
        with self.assertRaises(W.Refused):
            W.write(root, root / ".." / "escape.md", b"x")
        self.assertFalse((root.parent / "escape.md").exists())

    def test_target_refusals(self):
        with self.assertRaises(W.Refused):
            W.target("", self.project.parent)
        with self.assertRaises(W.Refused):          # outside the data parent
            W.target(str(self.project), self.project / "wiki")
        bare = self.project / "wiki"
        with self.assertRaises(W.Refused):          # not a project
            W.target(str(bare), self.project)
        self.assertEqual(W.target(str(self.project), self.project.parent), self.project)


class Names(unittest.TestCase):
    def test_safe_cut_and_unique(self):
        n = W.name('a<b>:c"d/e\\f|g?h*i\x01  j', "x")
        self.assertTrue(n.startswith("a_b_c_d_e_f_g_h_i_ j ("), n)
        self.assertNotEqual(W.name("same", "a"), W.name("same", "b"))
        self.assertEqual(W.name("same", "a"), W.name("same", "a"))
        self.assertEqual(len(W.name("가" * 200, "x")), W.NAME_MAX + 11)
        self.assertTrue(W.name("..", "x").startswith("untitled ("))
        self.assertTrue(W.name("CON", "x").startswith("CON ("))     # never the bare device name


class Options(unittest.TestCase):

    def test_settings_command_line_over_config(self):
        root = str(Path(tempfile.gettempdir()).resolve())
        cfg = {"mm_wiki_project": root, "mm_wiki_include": ["dev"], "mm_wiki_exclude": ["bot"],
               "mm_wiki_since": "2026-01"}
        self.assertEqual(W.settings(cfg, "mm"), (root, ["dev"], ["bot"], "2026-01"))
        self.assertEqual(W.settings(cfg, "mm", include=["ops"], exclude=[], since=""), (root, ["ops"], [], ""))
        self.assertEqual(W.settings(cfg, None), ("", [], [], ""))   # no prefix: the config is not read
        self.assertEqual(W.settings({}, None, project=root, exclude=["a"]), (root, [], ["a"], ""))
        with self.assertRaises(W.Refused):
            W.settings({}, None, since="26-01")


class Command(Project):
    """manage.py collect on the toy apps' declarations (toy: chat, prefix toy; toy2: mail, prefix toy2), the index
    replaced by a stand-in driver."""

    def setUp(self):
        super().setUp()
        self.data_root = self.project / "data"
        self.data_root.mkdir()
        self.config = self.project / "config.json"

    def write_config(self, **keys):
        self.config.write_text(json.dumps({"data_root": str(self.data_root), **keys}), encoding="utf-8")

    def collect(self, pkg, data, *argv, stale=False, system=False, config=True):
        """The command's exit code, stdout and stderr, as `manage.py collect <app> ...` runs it (the app selected)."""
        label = {T.TOY: "toy", T.TOY2: "toy2"}[pkg]
        search = importlib.import_module(pkg + ".search")
        export = W.export
        out, err = io.StringIO(), io.StringIO()
        with registry.use([pkg]), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                mock.patch.object(search, "DRIVER", Driver(data), create=True), \
                mock.patch.object(index, "open_search", return_value=(mock.Mock(), {}, {"stale": stale})), \
                mock.patch.object(index, "warn_if_stale"), \
                mock.patch.object(W, "export", lambda *a, **kw: export(*a, now=NOW, **kw)), \
                mock.patch.object(capture_run, "is_system", return_value=system):
            rc = command.main([label] + (["--config", str(self.config)] if config else []) + list(argv))
        return rc, out.getvalue(), err.getvalue()

    def test_chat_from_the_config(self):
        self.write_config(toy_wiki_project=str(self.project), toy_wiki_include=["dev"], toy_wiki_since="2026-09")
        rc, out, err = self.collect(T.TOY, CHAT)
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(out, "collect: toy files=1 written=1 unchanged=0 held=1 filtered=2 orphans=0\n")
        self.assertEqual(self.files("toy"), [f"{W.name('team › dev', 'ch1')}/2026-09.md"])

    def test_mail_from_the_command_line_over_the_config(self):
        self.write_config(toy2_wiki_project="relative", toy2_wiki_include=["nothing matches"])
        mails = {"m": ("S / x", [("k", ts(2026, 9, 1), "body", False)])}
        rc, out, _err = self.collect(T.TOY2, mails, "--project", str(self.project), "--include", "S", "--dry-run")
        self.assertEqual((rc, out), (0, "collect: toy2 dry-run files=1 written=1 unchanged=0 held=0 filtered=0 "
                                        "orphans=0\n"))
        self.assertFalse((self.project / "raw").exists())
        self.assertEqual(self.collect(T.TOY2, mails, "--project", str(self.project), "--include", "S")[0], 0)
        self.assertEqual(self.files("toy2"), ["2026-09.md"])
        self.assertEqual(self.collect(T.TOY2, mails, "--project", str(self.project))[0], 0)    # the config's include
        self.assertEqual(self.files("toy2"), ["2026-09.md"])                                  # leaves it an orphan

    def test_refusals_exit_2_and_write_nothing(self):
        self.write_config()
        project, parent = str(self.project), str(self.project.parent)
        for argv, why in (((), "toy_wiki_project is not set"), (("--project", parent), "no schema.md"),
                          (("--project", project, "--since", "2026"), "wiki settings"),
                          (("--project", project, "--prune"), None)):
            with self.subTest(argv=argv):
                rc, out, err = self.collect(T.TOY, CHAT, *argv, stale=bool(why is None))
                self.assertEqual((rc, out), (2, ""))
                self.assertTrue(err.startswith("collect: ") and (why is None or why in err), err)
        self.assertFalse((self.project / "raw").exists())
        self.assertEqual(self.collect(T.TOY, CHAT, config=False)[0], 2)             # no config: the default one

    def test_a_stale_scheduled_task_exports_nothing_as_localsystem(self):
        """A task registered as `manage.py collect <app> --config <cfg>` (what capture was called) must not export
        private data as SYSTEM: refused before the config is read."""
        rc, out, err = self.collect(T.TOY, CHAT, "--project", str(self.project), system=True, config=False)
        self.assertEqual((rc, err), (2, ""))
        self.assertEqual(out, "collect refused: running as LocalSystem; run it as the owner's account (llm.md 13)\n")
        self.assertFalse((self.project / "raw").exists())
        with mock.patch.object(capture_run.fsio, "token_user_sid", return_value="S-1-5-18"):
            self.assertTrue(capture_run.is_system())
            with contextlib.redirect_stdout(io.StringIO()) as o:
                self.assertEqual(command.main(["toy", "--config", str(self.config)]), 2)
            self.assertIn("LocalSystem", o.getvalue())


if __name__ == "__main__":
    unittest.main()
