# -*- coding: utf-8 -*-
"""tools/wikiexport.py: the three layouts on a stand-in driver, closed months packed and the open month held back,
include/since, a rerun writing nothing, orphans kept until --prune, and the refusals. Synthetic data only; no app is
searched here."""
import datetime
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools import wikiexport as W

KST = datetime.timezone(datetime.timedelta(hours=9))
NOW = datetime.datetime(2026, 10, 15, 12, 0, tzinfo=KST).timestamp()


def ts(*a):
    return int(datetime.datetime(*a, tzinfo=KST).timestamp())


class Driver:
    """changed_containers/chunk_units over {container: (label, units)}."""
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


class Export(unittest.TestCase):
    def setUp(self):
        self.project = Path(tempfile.mkdtemp(prefix="wikiexport-"))
        self.addCleanup(shutil.rmtree, self.project, True)
        (self.project / "schema.md").write_text("schema", encoding="utf-8")
        (self.project / "wiki").mkdir()

    def run_export(self, app, data, **kw):
        return W.export(app, Driver(data), None, self.project, now=NOW, **kw)

    def files(self, app):
        root = self.project / "raw" / "sources" / app
        return sorted(p.relative_to(root).as_posix() for p in root.rglob("*.md"))

    def test_chat_files_pack_closed_months_and_hold_the_open_one(self):
        rep = self.run_export("mattermost", CHAT)
        dev, rnd = W.name("team › dev", "ch1"), W.name("team › random", "ch2")
        self.assertEqual(self.files("mattermost"), [f"{dev}/2026-09.md", f"{rnd}/2026-08.md"])
        self.assertEqual((rep["files"], rep["written"], rep["held"], rep["filtered"]), (2, 2, 1, 0))
        text = (self.project / "raw/sources/mattermost" / dev / "2026-09.md").read_text(encoding="utf-8")
        self.assertIn('container: "ch1"\n', text)
        self.assertIn('records: 3\n', text)
        self.assertIn("\n## 2026-09-03\n\n- 14:22 alice: hi\n- 14:23 bob: two\n  lines\n", text)
        self.assertIn("\n## 2026-09-04\n\n- 09:00 alice: next day\n", text)
        self.assertNotIn("open month", text)
        text = (self.project / "raw/sources/mattermost" / rnd / "2026-08.md").read_text(encoding="utf-8")
        self.assertIn('period: "2026-08..2026-09"\n', text)
        self.assertLess(text.index("## 2026-08-20"), text.index("## 2026-09-20"))

    def test_a_full_file_starts_the_next_one(self):
        with mock.patch.object(W, "PACK", 5):           # every month fills a file on its own
            self.run_export("mattermost", CHAT)
        rnd = W.name("team › random", "ch2")
        self.assertIn(f"{rnd}/2026-09.md", self.files("mattermost"))
        mails = {f"m{m}": (f"S{m} / x", [(f"k{m}", ts(2026, m, 5), "y" * 6, False)]) for m in (6, 7, 8)}
        with mock.patch.object(W, "PACK", 10):          # two 6-character mails fill a file
            self.run_export("gmail", mails)
        self.assertEqual(self.files("gmail"), ["2026-06.md", "2026-08.md"])

    def test_rerun_writes_nothing(self):
        self.run_export("discord", CHAT)
        path = next((self.project / "raw/sources/discord").rglob("2026-09.md"))
        mtime = path.stat().st_mtime_ns
        rep = self.run_export("discord", CHAT)
        self.assertEqual((rep["written"], rep["unchanged"], rep["orphans"]), (0, 2, 0))
        self.assertEqual(path.stat().st_mtime_ns, mtime)

    def test_include_and_since(self):
        rep = self.run_export("mattermost", CHAT, include=["DEV"], since="2026-09")
        self.assertEqual(self.files("mattermost"), [f"{W.name('team › dev', 'ch1')}/2026-09.md"])
        self.assertEqual(rep["filtered"], 2)        # ch2's two messages: not included

        rep = self.run_export("gmail", {"a": ("Hi / Bot", [("k1", ts(2026, 9, 1), "x", False)]),
                                        "b": ("Hi / ann", [("k2", ts(2026, 9, 2), "y", False)])},
                              include=["hi"], exclude=["BOT"])
        self.assertEqual((rep["files"], rep["filtered"]), (1, 1))  # an exclude wins over an include

        rep = self.run_export("discord", CHAT, since="2026-09")
        rnd = W.name("team › random", "ch2")
        self.assertIn(f"{rnd}/2026-08.md", self.files("discord"))   # a file ending in September keeps August
        self.assertEqual((len(self.files("discord")), rep["filtered"]), (2, 0))

        rep = self.run_export("gmail", {"m": ("S / x", [("k", ts(2026, 9, 1), "body", False)])}, since="2026-10")
        self.assertEqual((rep["files"], rep["filtered"]), (0, 1))  # its newest month is before since

    def test_mail_month_holds_a_section_per_mail(self):
        mails = {"m2": ("Second / bob", [("k2", ts(2026, 9, 9, 8, 0), "body two", False)]),
                 "m1": ("First / alice", [("k1", ts(2026, 9, 2, 8, 0), "para one", False),
                                          ("k1", ts(2026, 9, 2, 8, 0), "para two", False)]),
                 "m3": ("Now / carol", [("k3", ts(2026, 10, 2, 8, 0), "open", False)]),
                 "gone": ("", [])}
        rep = self.run_export("gmail", mails)
        self.assertEqual(self.files("gmail"), ["2026-09.md"])
        self.assertEqual((rep["held"], rep["files"]), (1, 1))
        text = (self.project / "raw/sources/gmail/2026-09.md").read_text(encoding="utf-8")
        self.assertIn("records: 2\n", text)
        self.assertLess(text.index("## First / alice (2026-09-02 08:00)"), text.index("## Second / bob"))
        self.assertIn("\npara one\n\npara two\n", text)

    def test_page_keeps_headings_and_is_never_held(self):
        pages = {"pg": ("Space › Plan", [("b1", ts(2026, 3, 1), "Plan", True), ("b2", ts(2026, 10, 2), "text\nmore",
                                                                                False)])}
        rep = self.run_export("notion", pages)
        self.assertEqual(self.files("notion"), [f"{W.name('Space › Plan', 'pg')}.md"])
        self.assertEqual(rep["held"], 0)
        text = next((self.project / "raw/sources/notion").glob("*.md")).read_text(encoding="utf-8")
        self.assertIn('period: "2026-10"\n', text)
        self.assertIn("\n## Plan\n\ntext\nmore\n", text)
        self.assertEqual(self.run_export("notion", pages, since="2026-11")["files"], 0)

    def test_orphans_are_kept_until_prune(self):
        self.run_export("discord", CHAT)
        root = self.project / "raw/sources/discord"
        old = root / "renamed (00000000)" / "2026-01.md"
        old.parent.mkdir()
        old.write_text("x", encoding="utf-8")
        other = self.project / "raw/sources/notion/keep.md"
        other.parent.mkdir(parents=True)
        other.write_text("x", encoding="utf-8")
        dry = self.run_export("discord", CHAT, prune=True, dry_run=True)
        self.assertEqual(dry["orphans"], 1)
        self.assertTrue(old.exists())
        self.assertEqual(self.run_export("discord", CHAT)["orphans"], 1)
        self.assertTrue(old.exists())
        rep = self.run_export("discord", CHAT, prune=True)
        self.assertEqual((rep["orphans"], rep["pruned"]), (1, 1))
        self.assertFalse(old.parent.exists())
        self.assertTrue(other.exists())
        self.assertEqual(len(self.files("discord")), 2)

    def test_dry_run_writes_nothing(self):
        rep = self.run_export("discord", CHAT, dry_run=True)
        self.assertEqual((rep["files"], rep["written"]), (2, 2))
        self.assertFalse((self.project / "raw").exists())

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
    def test_checks(self):
        project, include, exclude, since = W.options("dc")
        self.assertEqual([o.key for o in (project, include, exclude, since)],
                         ["dc_wiki_project", "dc_wiki_include", "dc_wiki_exclude", "dc_wiki_since"])
        self.assertEqual(project.check(""), "")
        self.assertEqual(since.check("2026-01"), "2026-01")
        for op, bad in ((project, "relative\\path"), (project, 3), (since, "2026-13"), (since, "2026"),
                        (include, [""]), (include, "dev")):
            with self.assertRaises(ValueError):
                op.check(bad)

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


if __name__ == "__main__":
    unittest.main()
