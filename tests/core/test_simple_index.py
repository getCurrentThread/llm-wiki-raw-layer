# -*- coding: utf-8 -*-
"""index.SimpleIndex / SimpleApplier on a toy index of the simple shape (one content table folded from one
origin, an FTS5 table, a derived table), built from a three-round toy ledger: the rows and the derived table after the incremental rounds equal a full
rebuild's; the exact rows, version stamp and stats; odd records are counted."""
import sqlite3
import unittest
from collections import Counter

from rawlayer.capture import fsio
from rawlayer.search import index
from rawlayer.search import fts
from tests.core import toyfix as T
from tests.core.test_search_driver import ToyRounds

ORIGIN = "toy_a"
SCHEMA = """
CREATE TABLE msgs(id INTEGER PRIMARY KEY, key TEXT NOT NULL UNIQUE, origin TEXT NOT NULL, rev INTEGER NOT NULL,
  seq INTEGER NOT NULL, held INTEGER NOT NULL, deleted INTEGER NOT NULL DEFAULT 0, message TEXT NOT NULL,
  ng TEXT NOT NULL, tng TEXT NOT NULL DEFAULT '');
CREATE TABLE tally(origin TEXT PRIMARY KEY, n INTEGER NOT NULL) WITHOUT ROWID;
"""
UPSERT = ("INSERT INTO msgs(key, origin, rev, seq, held, message, ng, tng) VALUES(?,?,?,?,1,?,?,?) ON CONFLICT(key) "
          "DO UPDATE SET rev=excluded.rev, seq=excluded.seq, held=1, deleted=0, message=excluded.message, "
          "ng=excluded.ng, tng=excluded.tng")


def fold(con, counts, head, rec, seq, title):
    """The app's own part, shared by both builds: what one valid record does to the rows."""
    key, _kind, op, rev = head
    cur = con.execute("SELECT rev FROM msgs WHERE key=?", (key,)).fetchone()
    if cur and rev <= cur[0]:
        counts["stale_rev"] += 1
        return False
    if op == "put":
        native = rec.get("native")
        v = native.get("v") if isinstance(native, dict) else None
        if not isinstance(v, str):
            counts["bad_value"] += 1
            v = ""
        ng = fts.ng_text(v)
        con.execute(UPSERT, (key, rec["origin"], rev, seq, v, ng, fts.title_ng(v[:6], ng) if title else ""))
    elif op == "absent":
        con.execute("UPDATE msgs SET rev=?, seq=?, held=0, deleted=1 WHERE key=?", (rev, seq, key))
    else:
        con.execute("UPDATE msgs SET rev=?, seq=?, held=0, deleted=1, message='', ng='', tng='' WHERE key=?",
                    (rev, seq, key))
    return True


def finish(con):
    con.execute("DELETE FROM tally")
    con.execute("INSERT INTO tally SELECT origin, count(*) FROM msgs WHERE held=1 GROUP BY origin")


def make_simple(title):
    class Applier(index.SimpleApplier):
        origin = ORIGIN

        def record(self, rec, seq):
            if (head := self.header(rec)) is None:
                return
            self.dirty |= fold(self.con, self.counts, head, rec, seq, title)

        def finish(self):
            finish(self.con)

    class Index(index.SimpleIndex):
        name = "toysimple"
        schema_version = extractor_version = "toysimple-1"
        schema, table, applier_class = SCHEMA, "msgs", Applier

    Index.title = title                 # not in the class body: there `title = title` would not see the argument
    return Index()


def dump(data_root, spec):
    """Every table (the FTS5 shadow tables too), the schema, the version stamp and the stats of an index."""
    con = sqlite3.connect(fsio.ro_uri(index.index_path(data_root, spec), immutable=False), uri=True)
    try:
        out = {"sql": sorted(r[0] or "" for r in con.execute("SELECT sql FROM sqlite_master"))}
        for (name,) in con.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall():
            out[name] = sorted(repr(r) for r in con.execute(f"SELECT * FROM {name}"))
        meta = dict(con.execute("SELECT k, v FROM meta").fetchall())
        out["meta"] = {k: v for k, v in meta.items()
                       if k in ("head_seq", "schema_version", "tokenizer", "extractor_version")}
        out["stats"] = spec.stats(con)
        return out
    finally:
        con.close()


class TestSimpleIndex(T.ToyCase):
    APPS = (T.TOY,)
    STANDARD = False

    def build(self, title):
        sc = ToyRounds()
        self.addCleanup(T.force_rmtree, sc.fx.base)
        dr, spec = sc.fx.data_root, make_simple(title)
        for _ in range(3):
            self.assertEqual(sc.next_round(), 0)
            index.update(dr, spec)
        return dr, spec

    def test_incremental_rounds_and_rebuild_leave_the_same_rows(self):
        for title in (False, True):
            with self.subTest(title=title):
                dr, spec = self.build(title)
                incremental = dump(dr, spec)
                self.assertEqual(index.update(dr, spec, rebuild=True)["action"], "rebuilt")
                rebuilt = dump(dr, spec)
                self.assertEqual((rebuilt["msgs"], rebuilt["tally"]), (incremental["msgs"], incremental["tally"]))

    def test_exact_rows_stamp_and_stats(self):
        dr, simple = self.build(False)
        d = dump(dr, simple)
        self.assertEqual(d["stats"], {"rows": 5, "deleted": 0, "bad_value": 1})
        self.assertEqual(d["tally"], [repr((ORIGIN, 5))])
        con = sqlite3.connect(fsio.ro_uri(index.index_path(dr, simple), immutable=False), uri=True)
        self.addCleanup(con.close)
        rows = {r[0]: r[1:] for r in con.execute("SELECT key, origin, rev, seq, held, deleted, message FROM msgs")}
        self.assertEqual(rows["toy:toy_a:2"], (ORIGIN, 2, 2, 1, 0, "가나 나다 라마"))   # edited in round 2
        self.assertEqual(rows["toy:toy_a:4"], (ORIGIN, 1, 1, 1, 0, ""))                # the non-str value: counted
        self.assertEqual(d["meta"], {"head_seq": "3", "schema_version": index.COMMON_SCHEMA + ".toysimple-1",
                                     "tokenizer": fts.TOKENIZER_VERSION, "extractor_version": "toysimple-1"})
        self.assertEqual(simple.stamp()["source"], "toysimple")
        self.assertEqual((simple.origins, simple.fts_tables), (frozenset({ORIGIN}), ("msgs_fts",)))
        self.assertIn("CREATE TRIGGER msgs_ai", "\n".join(d["sql"]))


class TheRecordHeader(unittest.TestCase):
    """What every SimpleApplier counts as a bad record, and what it lets through."""

    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        self.addCleanup(self.con.close)
        self.con.executescript(index.META_DDL)
        self.spec = make_simple(False)
        self.spec.create_tables(self.con)

    def rec(self, **kv):
        return {"origin": ORIGIN, "op": "put", "rev": 1, "key": "k:1", "kind": "item", "native": {"v": "x"}, **kv}

    def test_valid_and_odd_records(self):
        app = self.spec.applier(self.con, full=False)
        self.assertEqual(app.header(self.rec()), ("k:1", "item", "put", 1))
        self.assertEqual(app.header(self.rec(rev=2 ** 63 - 1, op="purged", key=None, kind=None)),
                         (None, None, "purged", 2 ** 63 - 1))
        self.assertEqual(app.counts, Counter())
        odd = [None, [], {}, self.rec(origin="other"), self.rec(op="bump"), self.rec(op=None), self.rec(rev=0),
               self.rec(rev=-3), self.rec(rev=True), self.rec(rev=1.0), self.rec(rev="1"), self.rec(rev=2 ** 63)]
        self.assertEqual([app.header(r) for r in odd], [None] * len(odd))
        self.assertEqual(app.counts, Counter(bad_record=len(odd)))

    def test_end_manifest_counts_then_derives_once_when_something_changed(self):
        calls = []
        spec = make_simple(False)
        spec.applier_class.finish = lambda self: calls.append(self.full)
        for full, expect in ((False, [False]), (True, [])):
            calls.clear()
            app = spec.applier(self.con, full=full)
            app.record(self.rec(key="k:%d" % full), 1)
            app.record(None, 1)
            app.end_manifest(1)
            app.end_manifest(2)                         # nothing changed since: no second derivation
            self.assertEqual(calls, expect)             # a full build derives only in finish(), called by the driver
            self.assertEqual(index.read_counts(self.con).get("bad_record"), 1 if not full else 2)
            self.assertFalse(app.counts)

    def test_derive_can_be_narrower_than_finish(self):
        done = []

        class Narrow(index.SimpleApplier):
            origin = ORIGIN

            def derive(self):
                done.append("derive")

            def finish(self):
                done.append("finish")

        app = Narrow(self.con, False)
        app.end_manifest(1)
        Narrow(self.con, True).end_manifest(1)
        self.assertEqual(done, ["derive"])              # a catch-up calls derive() (whatever dirty says); a full build never


if __name__ == "__main__":
    unittest.main()
