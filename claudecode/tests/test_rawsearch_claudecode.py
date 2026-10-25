import json
import sqlite3
import unittest
from contextlib import closing
from rawlayer.search import common, dense, textindex as ti
from claudecode.search import SPEC, DRIVER, ClaudeCodeApplier, add_arguments, tool_text
from .fixtures import Fixture, CID, MID, row
from .ledgerfix import rounds
from . import helpers as h


class SearchTests(unittest.TestCase):
    def setUp(self):
        self.fx = Fixture()
        self.addCleanup(h.force_rmtree, self.fx.base)
        self.assertEqual(h.capture(self.fx), 0)
        common.update(self.fx.data_root, SPEC)

    def query(self, *args):
        rc, out, err = h.run_rawsearch(["claudecode", "--config", self.fx.config_path, "--json", *args])
        self.assertEqual(rc, 0, err)
        return json.loads(out)

    def update(self, containers):
        self.fx.write(containers)
        self.assertEqual(h.capture(self.fx), 0)
        common.update(self.fx.data_root, SPEC)

    def snapshot(self):
        with closing(sqlite3.connect(common.index_path(self.fx.data_root, SPEC))) as con:
            return {t: con.execute(f"SELECT * FROM {t} ORDER BY 1").fetchall() for t in ("msgs", "sessions", "counts")}

    def test_incremental_equals_rebuild_several_rounds(self):
        rounds(self.fx)
        common.update(self.fx.data_root, SPEC)
        self.update({CID: [[row()], [row("reply", text="deployed", role="assistant", n=1)]]})
        incremental = self.snapshot()
        common.update(self.fx.data_root, SPEC, rebuild=True)
        self.assertEqual(incremental, self.snapshot())

    def test_every_mode_envelope_and_text(self):
        for mode in ("hits", "count", "timeline", "senders", "sessions", "projects"):
            with self.subTest(mode=mode):
                result = self.query("deployment", "--mode", mode, "--project", "synthetic", "--role", "user",
                                    "--since", "2024-01", "--until", "2025-01")
                self.assertEqual(result["total"], 1)
                self.assertEqual(result["source"], "claudecode")
                self.assertFalse(result["index"]["stale"])
                rc, out, err = h.run_rawsearch(["claudecode", "--config", self.fx.config_path, "deployment", "--mode", mode])
                self.assertEqual(rc, 0, err)
                self.assertTrue(out)
        self.assertEqual(self.query("deployment", "--mode", "timeline")["rows"], [{"bucket": "2024-10", "n": 1}])
        self.assertEqual(self.query("deployment", "--mode", "senders")["rows"][0]["role"], "user")
        self.assertEqual(self.query("deployment", "--mode", "sessions")["rows"][0]["session_id"], CID)
        self.assertEqual(self.query("deployment", "--mode", "projects")["rows"][0]["project"], "/synthetic/project")

    def test_filters_and_escaped_project(self):
        for args in (("--project", "absent"), ("--role", "assistant"), ("--sender", "assistant"),
                     ("--since", "2025"), ("--until", "2023"), ("--project", "%")):
            self.assertEqual(self.query("deployment", *args)["total"], 0)
        self.assertEqual(self.query("--project", "synthetic")["total"], 1)
        self.assertEqual(self.query("--sender", "us")["total"], 1)
        self.assertEqual(self.query("--project", "absent", "--project", "synthetic")["total"], 1)
        self.assertEqual(self.query("deployment", "--tz", "+00:00")["rows"][0]["date"], "2024-10-01 00:00")

    def test_whole_session_includes_subagents_and_order(self):
        agent = CID + ".agent.test"
        self.update({CID: [[row(), row("reply", role="assistant", n=2, ts="2024-10-01T00:00:02Z")]],
                     agent: [[row("agent-reply", role="assistant", agent="agent.test", n=1,
                                  ts="2024-10-01T00:00:01Z")]],
                     "other": [[row("other", session="other")]]})
        result = self.query("--session", CID, "--limit", "1")
        self.assertEqual(result["mode"], "session")
        self.assertEqual(result["total"], 3)
        self.assertEqual([r["key"] for r in result["rows"]],
                         [f"cc:msg:{CID}:{MID}", f"cc:msg:{agent}:agent-reply", f"cc:msg:{CID}:reply"])
        self.assertEqual(result["rows"][1]["agent"], "agent.test")

    def test_list_sessions_labels_counts_filters(self):
        label = "a synthetic prompt " * 8
        self.update({CID: [[row(text=label), row("reply", role="assistant", n=1)]],
                     "other": [[row("other", session="other", text="history prompt", role="prompt", cwd="/other")]]})
        result = self.query("--list-sessions")
        self.assertEqual(result["mode"], "list")
        self.assertEqual(result["total"], 2)
        self.assertEqual(result["rows"][0]["label"], label[:80])
        self.assertEqual(result["rows"][0]["n"], 2)
        self.assertEqual(self.query("--list-sessions", "--project", "other")["total"], 1)
        self.assertEqual(self.query("--list-sessions", "--role", "prompt")["rows"][0]["session_id"], "other")
        self.assertEqual(len(self.query("--list-sessions", "--limit", "1")["rows"]), 1)

    def test_context_and_keyset_paging_with_dotted_container(self):
        self.update({CID: [[row(), *[row(f"m{i}", text=f"deployment {i}", n=i) for i in range(1, 5)]]],
                     CID + ".agent.test": [[row("agent", agent="agent.test", n=4)]]})
        context = self.query("deployment", "--context", "1")["rows"][0]["context"]
        self.assertTrue(context)
        self.assertEqual(sum(r["hit"] for r in context), 1)
        first = self.query("--project", "synthetic", "--limit", "2")
        self.assertTrue(first["page"]["more_older"])
        older = self.query("--project", "synthetic", "--limit", "2", "--before", first["page"]["before"])
        self.assertFalse({r["key"] for r in first["rows"]} & {r["key"] for r in older["rows"]})
        newer = self.query("--project", "synthetic", "--limit", "2", "--after", older["page"]["after"])
        self.assertEqual([r["key"] for r in first["rows"]], [r["key"] for r in newer["rows"]])
        self.assertEqual(DRIVER.parse_cursor(first["rows"][-1]["cursor"], "--before")[2], CID + ".agent.test")

    def test_tools_search_literal_exclusion_snippet_and_hit_shape(self):
        tools = [{"name": "Bash", "summary": "run checks | python verify.py"}]
        self.update({CID: [[row(), row("tool", text="", n=1, role="assistant", tools=tools)]]})
        for term in ("verify", '"run checks | python"'):
            result = self.query(term)
            self.assertEqual(result["total"], 1)
            hit = result["rows"][0]
            self.assertEqual(hit["text"], "")
            self.assertEqual(hit["tools"], "Bash: run checks | python verify.py")
            self.assertIn("[", hit["snippet"])
            self.assertEqual(set(hit), {"id", "key", "session_id", "agent", "role", "project", "branch", "model",
                                       "ts", "ts_ms", "date", "text", "snippet", "tools", "cursor"})
        self.assertEqual(self.query('Bash -"run checks"')["total"], 0)
        self.assertEqual(tool_text([{"name": "Bash", "summary": "first\nsecond"}]), "Bash: first second")

    def test_chunk_units_order_tools_and_dense_plan(self):
        self.update({CID: [[row(), row("later", text="second", n=2, ts="2024-10-01T00:00:02Z")]],
                     CID + ".agent": [[row("tool", text="", agent="agent", n=1, ts="2024-10-01T00:00:01Z",
                                           tools=[{"name": "Read", "summary": "synthetic.py"}])]]})
        con, _, _ = common.open_search(self.fx.data_root, SPEC)
        self.addCleanup(con.close)
        label, units = DRIVER.chunk_units(con, CID)
        self.assertTrue(label)
        self.assertEqual([u[0] for u in units],
                         [f"cc:msg:{CID}:{MID}", f"cc:msg:{CID}.agent:tool", f"cc:msg:{CID}:later"])
        self.assertIn("Read: synthetic.py", units[1][2])
        self.assertTrue(all(u[3] is False for u in units))
        self.assertEqual(DRIVER.changed_containers(con, 10 ** 9), [])
        chunks, containers = dense.plan(con, DRIVER, 0)
        self.assertEqual(containers, [CID])
        self.assertEqual({k for v in chunks.values() for k in v[1]}, {u[0] for u in units})

    def test_hybrid_falls_back_without_sidecar(self):
        result = self.query("deployment", "--hybrid")
        self.assertEqual(result["total"], 1)
        self.assertFalse(result["hybrid"]["available"])

    def fold(self, full=False):
        con = sqlite3.connect(":memory:")
        self.addCleanup(con.close)
        con.row_factory = sqlite3.Row
        ti.register_functions(con)
        con.executescript(common.META_DDL)
        SPEC.create_tables(con)
        if not full:
            SPEC.create_triggers(con)
        return con, ClaudeCodeApplier(con, full)

    def test_absent_and_purged_clear_search_and_chunks(self):
        rec = h.records(self.fx)[0]
        con, app = self.fold()
        app.record(rec, 1)
        app.end_manifest(1)
        app.record(dict(rec, op="absent", rev=2, native=None), 2)
        app.end_manifest(2)
        held = con.execute("SELECT * FROM msgs").fetchone()
        self.assertEqual(held["held"], 0)
        self.assertEqual(held["message"], "synthetic deployment")
        self.assertEqual(DRIVER.chunk_units(con, CID)[1], [])
        self.assertEqual(con.execute("SELECT count(*) FROM sessions").fetchone()[0], 0)
        parser = common.Parser()
        add_arguments(parser)
        for flags, expected in (([], 0), (["--include-deleted"], 1)):
            args = parser.parse_args(["--config", str(self.fx.config_path), "--role", "user", *flags])
            body, params = DRIVER.body(ti.build_fts_query("deployment"), DRIVER.filters(args, common.parse_tz("+00:00")))
            self.assertEqual(con.execute(f"SELECT count(*) {body}", params).fetchone()[0], expected)
        app.record(dict(rec, op="purged", rev=3, native=None), 3)
        app.end_manifest(3)
        held = con.execute("SELECT message,tools,ng FROM msgs").fetchone()
        self.assertEqual(tuple(held), ("", "", ""))
        self.assertEqual(con.execute("SELECT count(*) FROM msgs_fts WHERE msgs_fts MATCH 'deployment'").fetchone()[0], 0)

    def test_direct_fold_full_incremental_odd_values_stale_revisions(self):
        records = h.records(self.fx)
        snapshots = []
        for full in (False, True):
            con, app = self.fold(full)
            for rec in records:
                app.record(rec, 1)
                app.record(rec, 1)
                if not full:
                    app.end_manifest(1)
            for bad in (None, {}, dict(records[0], key="bad"), dict(records[0], rev=2 ** 80),
                        dict(records[0], key="cc:msg:bad\ud800:key"),
                        dict(records[0], rev=2, native=[]),
                        dict(records[0], rev=2, native={**records[0]["native"], "n": 2 ** 80}),
                        dict(records[0], rev=2, native={**records[0]["native"], "ts": "bad"})):
                app.record(bad, 2)
            app.end_manifest(2)
            if full:
                app.finish()
            snapshots.append({t: [tuple(r) for r in con.execute(f"SELECT * FROM {t} ORDER BY 1")]
                              for t in ("msgs", "sessions", "counts")})
        self.assertEqual(*snapshots)
        self.assertTrue(snapshots[0]["counts"])

    def test_usage_errors(self):
        for args in ([], ["--session", "bad:key"], ["--session", "missing"],
                     ["--session", CID, "--list-sessions"], ["--project", "synthetic", "--before", "bad"],
                     ["--project", "synthetic", "--before", "9" * 5000 + ".1.container"],
                     ["deployment", "--before", "1.1.c", "--after", "2.2.c"],
                     ["deployment", "--mode", "count", "--before", "1.1.c"],
                     ["deployment", "--role", "invalid"], ["deployment", "--limit", "-1"],
                     ["deployment", "--since", "bad"], ["--session", CID, "--hybrid"],
                     ["deployment", "--context", "-1"], ['"unclosed']):
            with self.subTest(args=args):
                rc, out, err = h.run_rawsearch(["claudecode", "--config", self.fx.config_path, *args])
                self.assertEqual(rc, 2, (out, err))
