# -*- coding: utf-8 -*-
"""tools/mcp_rawsearch.py: the MCP protocol over a real pipe (binary, one JSON message per line) against a stand-in
manage.py (STUB) that echoes the arguments it was given; the arguments the tools build and refuse; and the shaping
of results (texts dropped, warnings, the size cap, browse pages and chunks). No app is searched here."""
import json
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from tools import mcp_rawsearch as M

SERVER = Path(M.__file__).resolve()
# The stand-in manage.py: `search <app> --json ...` prints an envelope echoing argv; the query picks a behaviour.
# Its --help names the app's flags (and some a filter may never set); --thread returns a whole thread of 7 rows.
STUB = r'''
import json, sys, time
argv = sys.argv[1:]
if "--help" in argv:
    print("usage: manage.py search " + argv[1] + " [--channel C] [--team T] [--label L] [--thread T]")
    print("       [--page-text P] [--list-channels] [--before CURSOR] [--after CURSOR] [--config FILE] [--json]")
    print("  --context N   rows around each hit")
    sys.exit(0)
q = next((a.split("=", 1)[1] for a in argv if a.startswith("--query=")), "")
if q == "SLEEP":
    time.sleep(30)
if q == "USAGE":
    print("search: bad query near " + sys.argv[0], file=sys.stderr); sys.exit(2)
if q == "BUSY":
    sys.exit(75)
if q == "NOINDEX" or (argv[1] == "beta" and "--mode=count" in argv):
    print("search: no " + argv[1] + " index yet -- run collect", file=sys.stderr); sys.exit(2)
if q == "NOTE":
    print("# note: a note line", file=sys.stderr)
index = {"seq": 2, "ledger_seq": 3 if q == "STALE" else 2, "stale": q == "STALE"}
if "--thread=BIG" in argv:              # its row 1 is longer than any answer: 42000 characters, all distinct
    rows = [{"id": i, "text": "".join("%06d" % k for k in range(7000)) if i == 1 else "post %d" % i,
             "snippet": "post %d" % i} for i in range(3)]
    print(json.dumps({"source": argv[1], "mode": "thread", "argv": argv, "total": 3, "rows": rows, "index": index}))
    sys.exit(0)
if "--label=LONG" in argv:              # a gmail-like browse page: its newest mail's body is 42000 characters
    body = "".join("%06d" % k for k in range(7000))
    rows = [{"id": i, "cursor": "c%d" % i, "subject": "s", "body_text": body if i == 2 else "short",
             "text": "s " + (body if i == 2 else "short"), "snippet": "s"} for i in range(3)]
    limit = int(next((a.split("=", 1)[1] for a in argv if a.startswith("--limit=")), "3"))
    rows = rows[-limit:]
    page = {"before": rows[0]["cursor"], "after": rows[-1]["cursor"], "more_older": limit < 3, "more_newer": None}
    print(json.dumps({"source": argv[1], "mode": "browse", "argv": argv, "total": 3, "rows": rows, "page": page,
                      "index": index}))
    sys.exit(0)
if any(a.startswith("--thread=") for a in argv):
    rows = [{"id": i, "text": "post %d" % i, "snippet": "post %d" % i} for i in range(7)]
    print(json.dumps({"source": argv[1], "mode": "thread", "argv": argv, "total": 7, "rows": rows, "index": index}))
    sys.exit(0)
mode = next((a.split("=", 1)[1] for a in argv if a.startswith("--mode=")), "hits")
n = 0 if mode == "count" else int(next((a.split("=", 1)[1] for a in argv if a.startswith("--limit=")), "3"))
rows = [{"id": i, "text": "full text " * 50, "body_text": "body", "snippet": "snip", "sender": "s",
         "context": [{"id": "c", "text": "ctx clipped", "hit": False},
                     {"id": i, "text": "t", "snippet": "c", "hit": True}]} for i in range(n)]
print(json.dumps({"source": argv[1], "mode": mode, "argv": argv, "fts": "x", "total": len(rows), "rows": rows,
                  "index": index, "updated_at": "u", "built_at": "b"}, ensure_ascii=False))
'''


class Protocol(unittest.TestCase):
    """The server as Claude Code runs it: a child process on binary pipes, here over the stand-in manage.py."""

    @classmethod
    def setUpClass(cls):
        cls.dir = Path(tempfile.mkdtemp(prefix="llmw-mcp-"))
        cls.manage = cls.dir / "manage.py"
        cls.manage.write_text(STUB, encoding="utf-8")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir, True)

    def setUp(self):
        self.start()
        self.addCleanup(self.stop)

    def start(self, *extra):
        self.p = subprocess.Popen([sys.executable, "-s", "-B", str(SERVER), "--apps", "alpha,beta", "--timeout", "8",
                                   "--manage", str(self.manage), *extra], stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def stop(self):
        """Close the server's input and return what it still printed: (messages, log)."""
        if self.p.stdin.closed:
            return [], ""
        self.p.stdin.close()
        try:
            out, err = self.p.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            self.p.kill()
            out, err = self.p.communicate()
        return [json.loads(x) for x in out.decode("utf-8").splitlines() if x.strip()], err.decode("utf-8", "replace")

    def send(self, msg, end=b"\n"):
        data = msg if isinstance(msg, bytes) else json.dumps(msg, ensure_ascii=False).encode("utf-8")
        self.p.stdin.write(data + end)
        self.p.stdin.flush()

    def recv(self):
        line = self.p.stdout.readline()
        self.assertTrue(line.endswith(b"\n"), "the server closed its output")
        return json.loads(line.decode("utf-8"))

    def call(self, rid, name, args):
        self.send({"jsonrpc": "2.0", "id": rid, "method": "tools/call", "params": {"name": name, "arguments": args}})
        r = self.recv()
        self.assertEqual(r["id"], rid)
        return r

    def payload(self, r):
        self.assertFalse(r["result"]["isError"], r["result"]["content"][0]["text"])
        return json.loads(r["result"]["content"][0]["text"])

    def error_text(self, r):
        self.assertTrue(r["result"]["isError"])
        return r["result"]["content"][0]["text"]

    def test_session(self):
        self.send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                   "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t"}}})
        r = self.recv()
        self.assertEqual(r["result"]["protocolVersion"], "2025-06-18")
        self.assertIn("tools", r["result"]["capabilities"])
        self.assertIn("NOT masked", r["result"]["instructions"])
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, end=b"\r\n")      # CRLF framing
        r = self.recv()
        self.assertEqual(r["id"], 2)                        # nothing was answered to the notification
        self.assertEqual([t["name"] for t in r["result"]["tools"]], list(M.TOOLS))
        self.assertTrue(all(t["annotations"]["readOnlyHint"] for t in r["result"]["tools"]))
        env = self.payload(self.call(3, "raw_search", {"app": "alpha", "query": "배포 OR 보냈", "limit": 2,
                                                       "filters": {"channel": ["개발", "ops"], "list-channels": True}}))
        argv = env["argv"]
        self.assertEqual(argv[:3], ["search", "alpha", "--json"])
        for want in ("--mode=hits", "--query=배포 OR 보냈", "--channel=개발", "--channel=ops", "--list-channels",
                     "--limit=2"):
            self.assertIn(want, argv)
        self.assertLess(argv.index("--list-channels"), argv.index("--mode=hits"))   # a typed argument comes last
        self.assertFalse(any(x.startswith("--config") for x in argv))
        self.assertEqual(len(env["rows"]), 2)
        row = env["rows"][0]
        self.assertNotIn("text", row)
        self.assertEqual(row["snippet"], "snip")
        self.assertEqual(row["context"][0], {"id": "c", "hit": False, "snippet": "ctx clipped"})   # its only content
        self.assertEqual(row["context"][1], {"id": 0, "snippet": "c", "hit": True})
        self.assertNotIn("fts", env)
        self.assertTrue(env["privacy"].startswith("unmasked"))
        self.send({"jsonrpc": "2.0", "id": 4, "method": "ping"})
        self.assertEqual(self.recv(), {"jsonrpc": "2.0", "id": 4, "result": {}})
        messages, log = self.stop()
        self.assertEqual(messages, [])
        self.assertIn("tool=raw_search app=alpha ok rows=2", log)
        self.assertNotIn("배포", log)                       # the log never holds a query

    def test_errors(self):
        self.send(b"{not json")
        r = self.recv()
        self.assertEqual((r["id"], r["error"]["code"]), (None, -32700))
        self.send({"jsonrpc": "2.0", "id": 1, "method": "resources/list"})
        self.assertEqual(self.recv()["error"]["code"], -32601)
        self.send({"jsonrpc": "2.0", "id": 2, "method": "initialize", "params": {"protocolVersion": "1999-01-01"}})
        self.assertEqual(self.recv()["result"]["protocolVersion"], M.PROTOCOLS[0])
        self.assertEqual(self.call(3, "no_such_tool", {})["error"]["code"], -32602)
        self.send({"jsonrpc": "2.0", "id": {"x": 1}, "method": "ping"})
        self.assertEqual(self.recv()["error"]["code"], -32600)
        bad = [{"app": "gamma"}, {"app": "alpha", "limit": 500}, {"app": "alpha", "x": 1},
               {"app": "alpha", "query": "a\nb"}, {"app": "alpha", "limit": True}, {"app": "alpha", "offset": -1}]
        # a flag a typed argument owns, an abbreviation (argparse would take it), a name the app does not have
        bad += [{"app": "alpha", "filters": f} for f in ({"config": "elsewhere.json"}, {"json": True}, {"cont": "9"},
                                                          {"wid": "9999"}, {"que": "y"}, {"chan": "x"},
                                                          {"nosuch": "a b"}, ["channel"], {"hybrid": True})]
        for rid, args in enumerate(bad, 10):
            with self.subTest(args=args):
                self.error_text(self.call(rid, "raw_search", args))
        self.assertIn("not one of", self.error_text(self.call(30, "raw_aggregate", {"app": "alpha", "mode": "count",
                                                                                  "filters": {"mo": "hits"}})))
        self.assertIn("app is required", self.error_text(self.call(31, "raw_aggregate", {"mode": "count"})))
        text = self.error_text(self.call(32, "raw_search", {"app": "alpha", "query": "USAGE"}))
        self.assertIn("bad query", text)
        self.assertNotIn(str(Path.home()).lower(), text.lower())        # the stand-in's path is under the home
        self.assertIn("busy", self.error_text(self.call(33, "raw_search", {"app": "alpha", "query": "BUSY"})))
        self.assertIn("only with", self.error_text(self.call(34, "raw_aggregate", {"app": "all", "mode": "senders"})))
        self.assertIn("filters names", self.error_text(self.call(35, "raw_read", {"app": "alpha"})))
        env = self.payload(self.call(36, "raw_read", {"app": "alpha", "filters": {"label": "a b"}}))
        self.assertIn("--label=a b", env["argv"])           # an exact flag with a space in its value still passes
        env = self.payload(self.call(37, "raw_search", {"app": "alpha", "query": "q", "hybrid": True}))
        self.assertIn("--hybrid", env["argv"])
        env = self.payload(self.call(38, "raw_search", {"app": "alpha", "query": "q", "hybrid": False}))
        self.assertNotIn("--hybrid", env["argv"])

    def test_timeout_and_cancel(self):
        t0 = time.monotonic()
        self.assertIn("longer than", self.error_text(self.call(1, "raw_search", {"app": "alpha", "query": "SLEEP"})))
        self.assertLess(time.monotonic() - t0, 25)
        t0 = time.monotonic()
        self.send({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                   "params": {"name": "raw_search", "arguments": {"app": "alpha", "query": "SLEEP"}}})
        time.sleep(1)
        self.send({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 2}})
        self.send({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 99}})
        messages, log = self.stop()
        self.assertEqual(messages, [])                      # the cancelled request is never answered
        self.assertLess(time.monotonic() - t0, 7)           # its child was killed, not left to the 8 s timeout
        self.assertIn("tool=raw_search app=alpha cancelled", log)

    def test_status_aggregate_read_help(self):
        env = self.payload(self.call(1, "raw_status", {}))
        self.assertEqual(env["apps"]["alpha"]["state"], "ok")
        self.assertEqual(env["apps"]["alpha"]["rows"], 0)
        self.assertEqual(env["apps"]["beta"]["state"], "missing")
        self.assertNotIn("named_only", env)
        agg = self.payload(self.call(2, "raw_aggregate", {"app": "alpha", "mode": "timeline", "since": "2020",
                                                          "limit": 12}))
        self.assertEqual(agg["argv"][3:], ["--mode=timeline", "--since=2020", "--limit=12"])
        both = self.payload(self.call(3, "raw_aggregate", {"app": "all", "mode": "timeline", "since": "2020"}))
        self.assertEqual(set(both["apps"]), {"alpha", "beta"})
        read = self.payload(self.call(4, "raw_read", {"app": "alpha", "filters": {"thread": "t1"}}))
        self.assertIn("--thread=t1", read["argv"])
        self.assertIn("text", read["rows"][0])              # raw_read keeps the full texts by default
        self.assertEqual(read["chunk"], {"offset": 0, "rows": 7, "of": 7, "next_offset": None})
        part = self.payload(self.call(5, "raw_read", {"app": "alpha", "filters": {"thread": "t1"}, "limit": 3}))
        self.assertEqual(([r["id"] for r in part["rows"]], part["chunk"]["next_offset"]), ([0, 1, 2], 3))
        last = self.payload(self.call(6, "raw_read", {"app": "alpha", "filters": {"thread": "t1"}, "limit": 3,
                                                      "offset": 6}))
        self.assertEqual(([r["id"] for r in last["rows"]], last["chunk"]["next_offset"]), ([6], None))
        self.assertIn("--list-channels", self.payload(self.call(7, "raw_help", {"app": "beta"}))["help"])
        note = self.payload(self.call(8, "raw_search", {"app": "alpha", "query": "NOTE"}))
        self.assertEqual(note["warnings"], ["a note line"])
        stale = self.payload(self.call(9, "raw_search", {"app": "alpha", "query": "STALE"}))
        self.assertIn("behind the ledger", stale["warnings"][0])
        ignored = self.payload(self.call(10, "raw_search", {"app": "alpha", "query": "x", "offset": 5}))
        self.assertIn("offset", ignored["warnings"][0])
        self.assertIn("raw_read", self.error_text(self.call(11, "raw_aggregate", {"app": "alpha", "mode": "count",
                                                                                 "filters": {"thread": "t1"}})))

    def test_a_row_longer_than_an_answer_is_read_in_windows(self):
        whole = "".join("%06d" % k for k in range(7000))
        args, got, rid = {"app": "alpha", "filters": {"thread": "BIG"}, "offset": 1}, "", 1
        while True:
            env = self.payload(self.call(rid, "raw_read", args))
            rid += 1
            row = env["rows"][0]
            self.assertEqual(row["id"], 1)
            self.assertLessEqual(len(json.dumps(env, ensure_ascii=False)), M.MAX_CHARS)
            chunk = env["chunk"]
            if "next_text_offset" not in chunk:
                got += row["text"]
                break
            got += row["text"][:-1]                         # the clipped window ends with "…"
            self.assertEqual(chunk["next_offset"], 1)
            self.assertIn("text_offset", env["truncated"]["hint"])
            args = dict(args, text_offset=chunk["next_text_offset"])
        self.assertEqual(got, whole)
        self.assertEqual([r["id"] for r in env["rows"]], [1, 2])     # the tail, then the next row
        self.assertIsNone(env["chunk"]["next_offset"])
        self.assertLess(rid, 6)

    def test_a_long_mail_is_read_in_windows(self):
        # outside a thread or page's text (a gmail label browse): limit 1 and the same arguments select the same row
        whole = "".join("%06d" % k for k in range(7000))
        self.assertIn("limit 1", self.error_text(self.call(1, "raw_read", {"app": "alpha", "filters": {"label": "LONG"},
                                                                          "text_offset": 5})))
        args, got, rid = {"app": "alpha", "filters": {"label": "LONG"}, "limit": 1}, "", 2
        while True:
            env = self.payload(self.call(rid, "raw_read", args))
            rid += 1
            row = env["rows"][0]
            self.assertEqual((row["id"], "text" in row), (2, False))     # the body once: text repeats it
            self.assertLessEqual(len(json.dumps(env, ensure_ascii=False)), M.MAX_CHARS)
            if "chunk" not in env:
                got += row["body_text"]
                break
            got += row["body_text"][:-1]
            self.assertIn("limit 1", env["truncated"]["hint"])
            args = dict(args, text_offset=env["chunk"]["next_text_offset"])
        self.assertEqual(got, whole)
        self.assertLess(rid, 7)

    def test_named_only(self):
        self.stop()
        self.start("--named-only", "beta")
        self.send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}})
        self.assertIn("leaves out beta", self.recv()["result"]["instructions"])
        env = self.payload(self.call(2, "raw_status", {}))
        self.assertEqual(set(env["apps"]), {"alpha"})
        self.assertEqual(env["named_only"]["apps"], ["beta"])
        both = self.payload(self.call(3, "raw_aggregate", {"app": "all", "mode": "count", "query": "x"}))
        self.assertEqual(set(both["apps"]), {"alpha"})
        named = self.payload(self.call(4, "raw_status", {"app": "beta"}))
        self.assertEqual(named["apps"]["beta"]["state"], "missing")
        self.assertNotIn("named_only", named)

    def test_probe(self):
        base = [sys.executable, "-s", "-B", str(SERVER), "--apps", "alpha,beta", "--manage", str(self.manage),
                "--probe"]
        r = subprocess.run(base, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 3)
        self.assertIn("mcp_rawsearch: alpha: ok rows=0 seq=2/2", r.stdout)
        self.assertIn("mcp_rawsearch: beta: missing", r.stdout)
        r = subprocess.run(base + ["--named-only", "beta"], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0)
        self.assertIn("mcp_rawsearch: beta: named only", r.stdout)
        r = subprocess.run(base + ["--named-only", "gamma"], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 2)


class Replies(unittest.TestCase):
    def test_every_request_is_answered(self):
        # a lone surrogate (valid JSON as an escape) echoed into an error message cannot be encoded as UTF-8
        import io
        out = io.BytesIO()
        server = M.Server(M.Tools(["alpha"], M.Runner()), out, log=io.StringIO())
        lines = [json.dumps({"jsonrpc": "2.0", "id": 1, "method": "x\ud800"}).encode(),
                 json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                             "params": {"name": "raw_help", "arguments": {"app": "alpha", "x\ud800": 1}}}).encode(),
                 json.dumps({"jsonrpc": "2.0", "id": 3, "method": "ping"}).encode()]
        server.serve(lines)
        replies = [json.loads(x) for x in out.getvalue().decode("ascii").splitlines()]
        self.assertEqual(sorted(r["id"] for r in replies), [1, 2, 3])


class Arguments(unittest.TestCase):
    def test_filters(self):
        self.assertEqual(M.filter_args({"channel": ["a", "b"], "list-channels": True, "x": False, "team": "t"}),
                         ["--channel=a", "--channel=b", "--list-channels", "--team=t"])
        self.assertEqual(M.filter_args({"label": "a b"}, {"label"}), ["--label=a b"])
        for bad in ({"Config": "x"}, {"config": "x"}, {"query": "x"}, {"-x": "y"}, {"a": 3}, {"a": ["x"] * 11},
                    {"a": "x\x00"}, {"a": "x" * 201}, ["a"]):
            with self.subTest(bad=bad), self.assertRaises(M.ToolError):
                M.filter_args(bad)
        for bad in ({"lab": "x"}, {"nosuch": "a b"}):
            with self.subTest(bad=bad), self.assertRaises(M.ToolError):
                M.filter_args(bad, {"label"})

    def test_help_flags(self):
        text = "usage: x [--channel C] [--list-channels] [--config F] [--json]\n  --page-text P   a page (not --q)\n"
        self.assertEqual(M.help_flags(text), {"channel", "list-channels", "page-text"})

    def test_common(self):
        self.assertEqual(M.common_args({"query": "-x", "since": "3d", "include_deleted": True, "filters": {"a": "b"}}),
                         ["--query=-x", "--since=3d", "--include-deleted"])
        with self.assertRaises(M.ToolError):
            M.common_args({"sender": 5})

    def test_the_typed_arguments_are_the_search_parser_s(self):
        # the flags the tools set themselves exist in every app's parser (kakaotalk's is not opened here)
        from rawlayer.search import common
        import argparse
        p = argparse.ArgumentParser()
        common.add_search_arguments(p, ("hits", "count", "timeline", "senders"))
        flags = {s for a in p._actions for s in a.option_strings}
        for f in ("--query", "--sender", "--since", "--until", "--tz", "--mode", "--context", "--limit", "--width",
                  "--include-deleted", "--json"):
            self.assertIn(f, flags)


class Shaping(unittest.TestCase):
    def env(self, n=30, stale=False):
        rows = [{"id": i, "text": "t" * 400, "snippet": "s" * 50, "context": [{"text": "c" * 400, "snippet": "c"}]}
                for i in range(n)]
        return {"rows": rows, "fts": "f", "index": {"seq": 1, "ledger_seq": 2 if stale else 1, "stale": stale}}

    def browse(self, newest_first):
        rows = [{"id": i, "cursor": f"c{i}", "text": "t" * 400, "snippet": "s"} for i in range(30)]
        page = {"before": "c0", "after": "c29", "more_older": False if newest_first else None,
                "more_newer": None if newest_first else False}
        return {"mode": "browse", "rows": rows, "page": page}

    def test_texts_and_warnings(self):
        out = M.shape(self.env(2, stale=True), ["a note"])
        self.assertNotIn("text", out["rows"][0])
        self.assertEqual(out["rows"][0]["context"][0], {"snippet": "c"})
        self.assertEqual(out["warnings"][0], "a note")
        self.assertIn("behind the ledger", out["warnings"][1])
        self.assertNotIn("fts", out)
        self.assertIn("text", M.shape(self.env(2), [], full_text=True)["rows"][0])
        mail = M.shape({"rows": [{"text": "subject body", "body_text": "body", "subject": "subject"},
                                 {"text": "x", "body_text": ""}]}, [], full_text=True)["rows"]
        self.assertEqual(mail, [{"body_text": "body", "subject": "subject"}, {"text": "x", "body_text": ""}])
        bare = M.shape({"rows": [{"text": "x" * 500, "context": [{"text": "y" * 500}]}]}, [])["rows"][0]
        self.assertEqual(len(bare["snippet"]), 300)                 # a row's own text: clipped
        self.assertEqual(bare["context"][0], {"snippet": "y" * 500})    # a context row's: already cut to width

    def test_size_cap(self):
        out = M.shape(self.env(30), [], full_text=True, max_chars=6000)
        self.assertLessEqual(len(json.dumps(out, ensure_ascii=False)), 6000)
        self.assertGreater(out["truncated"]["contexts_dropped"], 0)
        self.assertGreater(out["truncated"]["rows_dropped"], 0)
        self.assertEqual(out["truncated"]["rows_returned"], len(out["rows"]))
        self.assertEqual(out["rows"][0]["id"], 0)                   # the best hits stay
        self.assertNotIn("truncated", M.shape(self.env(2), []))

    def test_a_newest_first_page_drops_its_oldest_rows(self):
        out = M.shape(self.browse(True), [], full_text=True, max_chars=6000)
        rows, page = out["rows"], out["page"]
        self.assertLessEqual(len(json.dumps(out, ensure_ascii=False)), 6000)
        self.assertEqual(rows[-1]["id"], 29)
        self.assertEqual((page["before"], page["after"], page["more_older"]), (rows[0]["cursor"], "c29", True))
        self.assertIn("filters.before", out["truncated"]["hint"])

    def test_an_after_page_drops_its_newest_rows(self):
        out = M.shape(self.browse(False), [], full_text=True, max_chars=6000)
        rows, page = out["rows"], out["page"]
        self.assertEqual(rows[0]["id"], 0)
        self.assertEqual((page["before"], page["after"], page["more_newer"]), ("c0", rows[-1]["cursor"], True))
        self.assertIn("filters.after", out["truncated"]["hint"])

    def test_a_clip_is_measured_as_json(self):
        text = "a\\b\n" * 15000                            # 60000 characters, 90000 as JSON
        out = M.shape({"mode": "thread", "rows": [{"id": 1, "text": text}]}, [], full_text=True)
        kept = out["rows"][0]["clipped"]["text"]["kept"]
        self.assertGreater(kept, 10000)                     # most of what fits, not the 80-character floor
        self.assertEqual(out["rows"][0]["text"][:-1], text[:kept])
        self.assertLessEqual(len(json.dumps(out, ensure_ascii=False)), M.MAX_CHARS)

    def test_one_long_row_is_clipped(self):
        out = M.shape({"mode": "thread", "rows": [{"id": 1, "text": "x" * 50000}]}, [], full_text=True,
                      max_chars=6000)
        self.assertEqual(len(out["rows"]), 1)
        self.assertTrue(out["rows"][0]["clipped"])
        self.assertTrue(out["truncated"]["texts_clipped"])
        self.assertLessEqual(len(json.dumps(out, ensure_ascii=False)), 6000)
