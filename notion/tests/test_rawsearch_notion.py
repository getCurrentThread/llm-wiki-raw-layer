import json
import sqlite3
import unittest
from contextlib import closing
from rawlayer.search import common
from notion.search import SPEC, DRIVER, NotionApplier
from .fixtures import Fixture, PID, BID, UID, OTHER, page, block
from . import helpers as h


class SearchTests(unittest.TestCase):
    def setUp(self):
        self.fx = Fixture()
        self.addCleanup(h.force_rmtree, self.fx.base)
        self.assertEqual(h.capture(self.fx), 0)
        common.update(self.fx.data_root, SPEC)

    def query(self, *args):
        rc, out, err = h.run_rawsearch(["notion", "--config", self.fx.config_path, "--json", *args])
        self.assertEqual(rc, 0, err)
        return json.loads(out)

    def snapshot(self):
        with closing(sqlite3.connect(common.index_path(self.fx.data_root, SPEC))) as con:
            return {t: con.execute(f"SELECT * FROM {t} ORDER BY 1").fetchall()
                    for t in ("nrows", "page_src", "page_order", "ds_src", "users", "pages", "counts")}

    def test_incremental_equals_rebuild_rounds_order_trash_restore_rename(self):
        from .ledgerfix import rounds
        rounds(self.fx)
        common.update(self.fx.data_root, SPEC)
        for trash in (True, False):
            value = page(blocks={BID: block(text="edited deployment"), OTHER: block(OTHER, "inserted")},
                         order=[[OTHER, 0, None], [BID, 1, OTHER]])
            value["page"]["in_trash"] = trash
            self.fx.write(value)
            self.fx.save("users.json", {UID: {"id": UID, "name": "Renamed"}})
            self.assertEqual(h.capture(self.fx), 0)
            common.update(self.fx.data_root, SPEC)
            incremental = self.snapshot()
            common.update(self.fx.data_root, SPEC, rebuild=True)
            self.assertEqual(incremental, self.snapshot())
        result = self.query("--page-text", PID)
        self.assertEqual([r["native_id"] for r in result["rows"] if r["kind"] == "block"], [OTHER, BID])

    def test_modes_filters_json_text(self):
        for mode in ("hits", "count", "timeline", "senders", "pages"):
            result = self.query("deployment", "--mode", mode, "--page", "Synthetic", "--sender", "Synthetic",
                                "--since", "2024-01", "--until", "2025-01")
            self.assertEqual(result["total"], 1)
            self.assertEqual(result["source"], "notion")
            self.assertFalse(result["index"]["stale"])
            rc, out, err = h.run_rawsearch(["notion", "--config", self.fx.config_path, "deployment", "--mode", mode])
            self.assertEqual(rc, 0, err)
        self.assertEqual(self.query("deployment", "--page", "absent")["total"], 0)

    def test_context_list_breadcrumb_page_text(self):
        value = page(pid=OTHER, blocks={BID: block(), OTHER: block(OTHER, "next deployment")})
        value["page"]["parent"] = {"type": "page_id", "page_id": PID}
        self.fx.write(value)
        self.assertEqual(h.capture(self.fx), 0)
        common.update(self.fx.data_root, SPEC)
        result = self.query("deployment", "--page", OTHER, "--context", "1")
        self.assertEqual(len(result["rows"][0]["context"]), 2)
        result = self.query("--page-text", OTHER)
        self.assertEqual(len(result["breadcrumb"]), 2)
        self.assertEqual(result["total"], 3)
        self.assertEqual(self.query("--list-pages")["total"], 2)
        self.assertEqual(self.query("--list-pages", "--page", PID)["total"], 1)

    def test_deleted_missing_order_and_gone(self):
        self.fx.write(page(order=[]))
        self.assertEqual(h.capture(self.fx), 0)
        common.update(self.fx.data_root, SPEC)
        self.assertEqual(self.query("deployment")["total"], 0)
        self.assertEqual(self.query("deployment", "--include-deleted")["total"], 1)
        self.fx.write(page(_gone_at="2024-10-02T00:00:00Z"))
        self.assertEqual(h.capture(self.fx), 0)
        common.update(self.fx.data_root, SPEC)
        self.assertEqual(self.query("--list-pages")["total"], 0)
        self.assertEqual(self.query("--list-pages", "--include-deleted")["total"], 1)

    def test_paging(self):
        first = self.query("--page", PID, "--limit", "1")
        older = self.query("--page", PID, "--limit", "1", "--before", first["page"]["before"])
        self.assertEqual(len(older["rows"]), 1)
        self.assertNotEqual(first["rows"][0]["key"], older["rows"][0]["key"])
        newer = self.query("--page", PID, "--limit", "1", "--after", older["page"]["after"])
        self.assertEqual(first["rows"][0]["key"], newer["rows"][0]["key"])

    def test_page_title_match_ranks_first_and_block_type(self):
        self.fx.write(page(title="예산 계획", blocks={BID: block(text="예산 예산 예산 정리")}))
        self.assertEqual(h.capture(self.fx), 0)
        common.update(self.fx.data_root, SPEC)
        self.assertEqual([r["kind"] for r in self.query("예산")["rows"]], ["page", "block"])
        with closing(sqlite3.connect(common.index_path(self.fx.data_root, SPEC))) as con:
            self.assertEqual(con.execute("SELECT kind, btype, tng != '' FROM nrows ORDER BY kind").fetchall(),
                             [("block", "paragraph", 0), ("page", "", 1)])

    def test_chunk_units_page_first_headings_break(self):
        heading = dict(block(OTHER, "개요"), type="heading_2")
        heading["heading_2"] = heading.pop("paragraph")
        self.fx.write(page(blocks={OTHER: heading, BID: block()}, order=[[OTHER, 0, None], [BID, 1, OTHER]]))
        self.assertEqual(h.capture(self.fx), 0)
        common.update(self.fx.data_root, SPEC)
        con, _meta, _fresh = common.open_search(self.fx.data_root, SPEC)
        self.addCleanup(con.close)
        self.assertEqual(DRIVER.changed_containers(con, 0), [PID])
        label, units = DRIVER.chunk_units(con, PID)
        self.assertEqual(label, "Synthetic page")
        self.assertEqual([(u[0].split(":")[1], u[3]) for u in units], [("page", False), ("block", True), ("block", False)])
        self.assertEqual(units[1][2], "개요")

    def test_usage_errors(self):
        for args in ([], ["--page-text", "bad"], ["--page", PID, "--before", "bad"],
                     ["--page", PID, "--before", "9" * 5000 + ".nt:page:" + PID],
                     ["deployment", "--context", "-1"], ["--page-text", PID, "--list-pages"]):
            rc, out, err = h.run_rawsearch(["notion", "--config", self.fx.config_path, *args])
            self.assertEqual(rc, 2, (args, out, err))

    def test_direct_fold_block_before_page_full_incremental(self):
        # Each record in its own manifest exposes accidental dependency on derived columns.
        recs = sorted(h.records(self.fx), key=lambda r: r["key"])
        page_rec = next(r for r in recs if r["kind"] == "page")
        changed = dict(page_rec, rev=2, derived={"order": []})
        restored = dict(page_rec, rev=3)
        renamed = {"origin": "nt_archive", "kind": "user", "key": "nt:user:" + UID,
                   "op": "put", "rev": 2, "native": {"name": "Renamed"}}
        recs += [changed, restored, renamed]
        out = []
        for full in (False, True):
            con = sqlite3.connect(":memory:")
            self.addCleanup(con.close)
            con.executescript(common.META_DDL)
            SPEC.create_tables(con)
            app = NotionApplier(con, full)
            for seq, rec in enumerate(recs, 1):
                app.record(rec, seq)
                app.end_manifest(seq)
            for rec in (None, {}, {"origin": "nt_archive", "op": "put", "rev": 1, "key": "bad", "kind": "block"},
                        dict(page_rec, rev=2 ** 80), dict(page_rec, rev=99, native=[])):
                app.record(rec, 99)
            app.end_manifest(99)
            if full:
                app.finish()
            out.append({t: con.execute(f"SELECT * FROM {t} ORDER BY 1").fetchall()
                        for t in ("nrows", "page_src", "page_order", "users", "pages", "counts")})
        self.assertEqual(out[0], out[1])
