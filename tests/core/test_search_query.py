# -*- coding: utf-8 -*-
"""The query half every app's search shares (rawlayer.search.driver.SearchDriver) on a driver that has no real app
behind it: the --mode choices derived from group_modes, the shared --before / --after and the app's own flags in
add_arguments, the group rows and their text label built from a Group table (every fallback rule the apps used),
the --context window of the sort key, and the shared date span of a group."""
import sqlite3
import types
import unittest

from rawlayer.search import index
from rawlayer.search import driver as drv

SCHEMA = """
CREATE TABLE msgs(id INTEGER PRIMARY KEY, room TEXT NOT NULL, ts_ms INTEGER NOT NULL, sid INTEGER NOT NULL,
  deleted INTEGER NOT NULL, message TEXT NOT NULL);
CREATE TABLE users(user_id TEXT PRIMARY KEY, name TEXT NOT NULL, nick TEXT NOT NULL);
CREATE TABLE rooms(chat_id TEXT PRIMARY KEY, title TEXT NOT NULL);
CREATE TABLE labels(label_id TEXT PRIMARY KEY, name TEXT NOT NULL);
"""


class Fake(drv.SearchDriver):
    name, table, fts_table = "fake", "msgs", "msgs_fts"
    sort_cols = ("ts_ms", "sid")
    context_col = "room"
    cursor_form = "<ts_ms>.<sid>"
    sender_help = "the fake sender"
    group_modes = {
        "senders": drv.Group("t.user", "user_id", "users", "name", extra=(("nickname", "nick"),),
                             fallback=(None, "?"), tag="user"),
        "rooms": drv.Group("t.room", "chat_id", "rooms", "title", fallback=("(untitled)",), tag="chat"),
        "kinds": drv.Group("t.kind", "kind"),
        "labels": drv.Group("m.label", "id", "labels", "name", "label_id", keep_empty=True),
    }

    def arguments(self, p):
        p.add_argument("--extra", action="store_true")

    def hit(self, r, tz, marks, width):
        return {"id": r["id"]}


class Modes(unittest.TestCase):
    def test_modes_follow_group_modes(self):
        self.assertEqual(Fake(None).modes, ("hits", "count", "timeline", "senders", "rooms", "kinds", "labels"))
        self.assertEqual(drv.SearchDriver(None).modes, ("hits", "count", "timeline"))

    def test_shared_arguments(self):
        p = index.Parser(prog="fake")
        Fake(None).add_arguments(p)
        a = p.parse_args(["--config", "c", "--mode", "rooms", "--extra", "--before", "1.2", "--after", "3.4"])
        self.assertEqual((a.mode, a.extra, a.before, a.after), ("rooms", True, "1.2", "3.4"))
        self.assertEqual(p.parse_args(["--config", "c"]).before, None)
        for bad in (["--mode", "nosuch"], ["--mode", "labels2"]):
            with self.assertRaises(index.UsageError):
                p.parse_args(["--config", "c", *bad])
        text = " ".join(p.format_help().split())
        self.assertIn("(<ts_ms>.<sid>, printed with each page)", text)
        self.assertIn("the fake sender", text)
        self.assertLess(text.index("--extra"), text.index("--before"))      # the app's flags first, then the cursors
        self.assertIn("{hits,count,timeline,senders,rooms,kinds,labels}", text)


class Groups(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        self.con.row_factory = sqlite3.Row
        self.addCleanup(self.con.close)
        self.con.executescript(SCHEMA)
        self.con.executemany("INSERT INTO users VALUES(?,?,?)", [("u1", "Ann", "annie"), ("u2", "", "x")])
        self.con.executemany("INSERT INTO rooms VALUES(?,?)", [("r1", "Dev"), ("r2", "")])
        self.con.executemany("INSERT INTO labels VALUES(?,?)", [("L1", ""), ("L3", "Work")])
        self.d, self.tz = Fake(None), index.parse_tz("+09:00")

    def row(self, mode, gid, f=1727758980, l=1727845380):
        return self.d.group_row(self.con, {"gid": gid, "n": 4, "f": f, "l": l}, mode, self.tz)

    def test_label_from_the_table_then_the_fallbacks(self):
        span = {"first": "2024-10-01", "last": "2024-10-02"}
        self.assertEqual(self.row("senders", "u1"), {"user_id": "u1", "label": "Ann", "nickname": "annie", "n": 4, **span})
        self.assertEqual(self.row("senders", "u2")["label"], "u2")           # an empty name: the id
        self.assertEqual(self.row("senders", "u9"), {"user_id": "u9", "label": "u9", "nickname": "", "n": 4, **span})
        self.assertEqual(self.row("senders", "")["label"], "?")              # no name, no id: the last fallback
        row = self.row("senders", None)
        self.assertEqual((row["user_id"], row["label"], row["nickname"]), (None, "?", ""))
        self.assertEqual([self.row("rooms", g)["label"] for g in ("r1", "r2", "r9", None)],
                         ["Dev", "(untitled)", "(untitled)", "(untitled)"])
        self.assertEqual(self.row("rooms", "r1"), {"chat_id": "r1", "label": "Dev", "n": 4, **span})
        self.assertEqual(self.row("kinds", "k"), {"kind": "k", "label": "k", "n": 4, **span})   # no table: the id
        self.assertEqual(self.row("kinds", "")["label"], "")
        self.assertEqual(list(self.row("senders", "u1")), ["user_id", "label", "nickname", "n", "first", "last"])

    def test_keep_empty_is_for_an_existing_row_only(self):
        self.assertEqual(self.row("labels", "L1")["label"], "")              # the row exists with an empty name
        self.assertEqual(self.row("labels", "L2")["label"], "L2")            # no row: the id
        self.assertEqual(self.row("labels", "L3")["label"], "Work")
        self.assertEqual(self.row("labels", "L3")["id"], "L3")                # id_col is label_id, the JSON key id

    def test_text_label_is_the_label_and_tag_equals_id(self):
        for mode, gid, want in (("senders", "u1", ("Ann", "user=u1")), ("rooms", "r2", ("(untitled)", "chat=r2")),
                                ("kinds", "k", ("k", "kind=k")), ("labels", "L1", ("", "id=L1")),
                                ("senders", None, ("?", "user=None"))):
            self.assertEqual(self.d.group_label(self.row(mode, gid), mode), want)

    def test_day_span_is_in_the_given_zone(self):
        t = 1727758980                                                      # 2024-10-01 05:23 UTC
        self.assertEqual(index.day_span(t, t + 86400, self.tz), {"first": "2024-10-01", "last": "2024-10-02"})
        self.assertEqual(index.day_span(t, None, index.parse_tz("-10:00")), {"first": "2024-09-30", "last": ""})
        self.assertEqual(index.day_span(0, None, self.tz), {"first": "", "last": ""})


class Context(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        self.con.row_factory = sqlite3.Row
        self.addCleanup(self.con.close)
        self.con.executescript(SCHEMA)
        #            id room  ts  sid deleted
        rows = [(1, "A", 10, 1, 0), (2, "A", 10, 2, 0), (3, "A", 20, 1, 0), (4, "A", 30, 1, 1), (5, "A", 40, 1, 0),
                (6, "B", 25, 1, 0), (7, "A", 50, 9, 0)]
        self.con.executemany("INSERT INTO msgs VALUES(?,?,?,?,?,'')", rows)
        self.d, self.tz = Fake(None), index.parse_tz("+09:00")

    def around(self, hit, n, deleted=False):
        r = self.con.execute("SELECT * FROM msgs WHERE id=?", (hit,)).fetchone()
        a = types.SimpleNamespace(include_deleted=deleted, width=40)
        return [(c["id"], c["hit"]) for c in self.d.context(self.con, r, n, a, self.tz)]

    def test_window_of_the_sort_key_inside_the_context_column(self):
        self.assertEqual(self.around(3, 1), [(2, False), (3, True), (5, False)])      # id 4 is deleted, B is another room
        self.assertEqual(self.around(3, 1, deleted=True), [(2, False), (3, True), (4, False)])
        self.assertEqual(self.around(3, 9), [(1, False), (2, False), (3, True), (5, False), (7, False)])
        self.assertEqual(self.around(2, 1), [(1, False), (2, True), (3, False)])      # same ts_ms: the sid orders them
        self.assertEqual(self.around(7, 2), [(3, False), (5, False), (7, True)])      # nothing after the last row
        self.assertEqual(self.around(6, 3), [(6, True)])                              # the only row of room B


if __name__ == "__main__":
    unittest.main()
