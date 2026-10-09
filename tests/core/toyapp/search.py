# -*- coding: utf-8 -*-
"""Toy raw search: the index <data_root>\\index\\toy.sqlite over the toy app's records. One row per key: the
latest record decides (a put holds the row and its text native.v when that is a string; absent / purged release
it, purged also drops the text). facet: rows held per origin. Odd records are counted, never raised."""
from collections import Counter

from rawlayer.search import common
from rawlayer.search import textindex as ti
from rawlayer.search.common import UsageError

NAME = "toy"
HELP = "search the toy index"
ORIGINS = ("toy_a", "toy_b", "toy_m")
OPS = ("put", "absent", "purged")

SCHEMA = """
CREATE TABLE item(id INTEGER PRIMARY KEY, key TEXT NOT NULL UNIQUE, origin TEXT NOT NULL, rev INTEGER NOT NULL,
  seq INTEGER NOT NULL, held INTEGER NOT NULL, v TEXT NOT NULL, ng TEXT NOT NULL);
CREATE TABLE facet(origin TEXT PRIMARY KEY, n INTEGER NOT NULL) WITHOUT ROWID;
"""

TRIGGERS = """
CREATE TRIGGER item_ai AFTER INSERT ON item BEGIN
  INSERT INTO item_fts(rowid, ng) VALUES (new.id, new.ng);
END;
CREATE TRIGGER item_ad AFTER DELETE ON item BEGIN
  INSERT INTO item_fts(item_fts, rowid, ng) VALUES ('delete', old.id, old.ng);
END;
CREATE TRIGGER item_au AFTER UPDATE OF ng ON item BEGIN
  INSERT INTO item_fts(item_fts, rowid, ng) VALUES ('delete', old.id, old.ng);
  INSERT INTO item_fts(rowid, ng) VALUES (new.id, new.ng);
END;
"""


class ToyApplier:
    def __init__(self, con, full):
        self.con, self.full = con, full
        self.counts = Counter()
        self.touched = set()

    def record(self, rec, seq):
        if not isinstance(rec, dict) or rec.get("origin") not in ORIGINS or rec.get("op") not in OPS \
                or not isinstance(rec.get("key"), str) or not isinstance(rec.get("rev"), int):
            self.counts["bad_record"] += 1
            return
        origin, op, key, rev = rec["origin"], rec["op"], rec["key"], rec["rev"]
        if op == "put":
            native = rec.get("native")
            v = native.get("v") if isinstance(native, dict) else None
            if not isinstance(v, str):
                self.counts["bad_value"] += 1
                v = ""
            self.con.execute(
                "INSERT INTO item(key, origin, rev, seq, held, v, ng) VALUES(?,?,?,?,1,?,?) ON CONFLICT(key) DO UPDATE "
                "SET origin=excluded.origin, rev=excluded.rev, seq=excluded.seq, held=1, v=excluded.v, ng=excluded.ng",
                (key, origin, rev, seq, v, ti.ng_text(v)))
        elif op == "absent":
            self.con.execute("UPDATE item SET rev=?, seq=?, held=0 WHERE key=?", (rev, seq, key))
        else:
            self.con.execute("UPDATE item SET rev=?, seq=?, held=0, v='', ng='' WHERE key=?", (rev, seq, key))
        self.touched.add(origin)

    def end_manifest(self, seq):
        common.bump_counts(self.con, self.counts)
        self.counts.clear()
        if not self.full:
            for o in sorted(self.touched):
                self._facet(o)
        self.touched.clear()

    def finish(self):
        for o in ORIGINS:
            self._facet(o)

    def _facet(self, origin):
        n = self.con.execute("SELECT count(*) FROM item WHERE origin=? AND held=1", (origin,)).fetchone()[0]
        self.con.execute("INSERT OR REPLACE INTO facet(origin, n) VALUES(?, ?)", (origin, n))


class ToyIndex(common.SourceIndex):
    name = NAME
    origins = frozenset(ORIGINS)
    schema_version = "toy-1"
    extractor_version = "toy-1"
    fts_tables = ("item_fts",)

    def create_tables(self, con):
        con.executescript(SCHEMA)
        con.execute(ti.fts_ddl("item"))

    def create_triggers(self, con):
        for stmt in TRIGGERS.strip().split("END;"):
            if stmt.strip():
                con.execute(stmt + "END;")

    def applier(self, con, full):
        return ToyApplier(con, full)

    def stats(self, con):
        rows, held = con.execute("SELECT count(*), coalesce(sum(held), 0) FROM item").fetchone()
        out = {"rows": rows, "held": held}
        out.update(common.read_counts(con))
        return out


SPEC = ToyIndex()


def add_arguments(p):
    common.add_search_arguments(p, ["hits"])
    p.add_argument("--include-gone", action="store_true", help="also rows whose latest record is not a put")


def run(a, spec=SPEC):
    data_root = common.load_data_root(a.config)
    common.check_numbers(a)
    query = common.query_text(a)
    if query is None:
        raise UsageError("give a query")
    try:
        q = ti.build_fts_query(query)
    except ti.QueryError as e:
        raise UsageError(f"bad query: {e}") from None
    con, _meta, fresh = common.open_search(data_root, spec)
    try:
        common.warn_if_stale(spec, fresh)
        frm, where, params = ti.sql_parts(q, "item", "item_fts", text_col="v")
        if not a.include_gone:
            where = list(where) + ["t.held = 1"]
        rows = con.execute(f"SELECT t.key FROM {frm} WHERE {' AND '.join(where) or '1'} ORDER BY t.key",
                           params).fetchall()
    finally:
        con.close()
    keys = [r[0] for r in rows][:a.limit]
    if a.json:
        print(common.dumps({"source": spec.name, "total": len(rows), "keys": keys,
                            "index": {"seq": fresh["seq"], "ledger_seq": fresh["ledger_seq"],
                                      "stale": fresh["stale"]}}))
    else:
        for k in keys:
            print(k)
    return common.EXIT_OK
