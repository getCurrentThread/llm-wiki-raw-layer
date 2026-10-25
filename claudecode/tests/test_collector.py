"""Synthetic collector contracts; no owner sources are read."""
import datetime as dt
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from claudecode import collector as cc
from tools.archiveio import CollectorError


class CollectorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.source = Path(self.tmp.name) / "source"
        self.root = Path(self.tmp.name) / "archive"
        self.source.mkdir()

    def entry(self, ident="u1", content="synthetic prompt", **values):
        obj = {"type": "user", "sessionId": "session", "uuid": ident,
               "timestamp": "2026-01-01T00:00:00.000Z", "cwd": "synthetic-project",
               "gitBranch": "main", "message": {"content": content}}
        obj.update(values)
        return obj

    def write(self, rows, rel="projects/project/session.jsonl", append=False):
        path = self.source / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("ab" if append else "wb") as fh:
            for row in rows:
                fh.write(json.dumps(row).encode() + b"\n")
        return path

    def sync(self, **kwargs):
        return cc.sync(self.root, self.source, **kwargs)

    def manifest(self):
        return json.loads((self.root / "manifest.json").read_text())

    def rows(self, cid="session"):
        return [row for rel in self.manifest()["containers"][cid]["files"]
                for row in json.loads((self.root / rel).read_text())["rows"]]

    def test_skip_rules_and_complete_invalid_lines(self):
        rows = [self.entry("type", type="system"), self.entry("meta", isMeta=True),
                self.entry("compact", isCompactSummary=True),
                self.entry("result", [{"type": "tool_result", "content": "hidden"}]),
                self.entry("thinking", [{"type": "thinking", "thinking": "hidden"}]),
                self.entry("redacted", [{"type": "redacted_thinking", "data": "hidden"}]),
                self.entry("image", [{"type": "image", "source": "hidden"}]),
                self.entry("empty", ""), self.entry("missing", None),
                self.entry("no-message", message=None), self.entry("empty-blocks", []),
                self.entry("bad-content", 42)]
        rows += [self.entry(str(i), " \t" + p + "hidden") for i, p in enumerate(cc.PREFIXES)]
        rows += [[], self.entry("keep")]
        path = self.write(rows)
        with path.open("ab") as fh:
            fh.write(b"not json\n\xff\n")
        stats = self.sync()
        self.assertEqual(stats["errors"], 0)
        self.assertEqual(stats["lines_bad"], 2)
        self.assertEqual([r["uuid"] for r in self.rows()], ["keep"])
        self.assertEqual(self.rows()[0]["n"], len(rows) - 1)

    def test_text_tools_and_exact_row_fields(self):
        content = [{"type": "text", "text": "one"}, {"type": "thinking", "thinking": "secret"},
                   {"type": "redacted_thinking", "data": "secret"},
                   {"type": "tool_result", "content": "secret"}, {"type": "image", "source": "secret"},
                   {"type": "text", "text": "two"}]
        tools = [("Bash", {"description": "run", "command": "x" * 201}, "run | " + "x" * 200),
                 ("PowerShell", {"command": "y" * 201}, "y" * 200),
                 ("Bash", {"description": "only"}, "only"),
                 ("PowerShell", {}, ""),
                 ("Read", {"file_path": "", "path": "p" * 201, "query": "ignored"}, "p" * 200),
                 ("Other", {}, "")]
        content += [{"type": "tool_use", "name": name, "input": args} for name, args, _ in tools]
        self.write([self.entry(content=content, type="assistant", parentUuid="parent",
                               message={"content": content, "model": "synthetic-model"})])
        self.sync()
        row = self.rows()[0]
        self.assertEqual(row["text"], "one\n\ntwo")
        self.assertEqual(row["tools"], [{"name": n, "summary": s} for n, _, s in tools])
        self.assertEqual(row["model"], "synthetic-model")
        self.assertEqual(row["parent"], "parent")
        self.assertEqual(set(row), {"session", "agent", "uuid", "parent", "n", "ts", "role", "cwd",
                                    "branch", "model", "text", "tools", "from"})
        self.assertNotIn("secret", json.dumps(row))

    def test_string_and_tool_only_and_tool_priority(self):
        for key in ("file_path", "path", "pattern", "url", "query", "description", "prompt"):
            self.assertEqual(cc._tool({"name": "Find", "input": {key: "z" * 201}})["summary"], "z" * 200)
        self.write([self.entry(), self.entry("tool", [{"type": "tool_use", "name": "Read", "input": {}}])])
        self.sync()
        self.assertEqual(len(self.rows()), 2)
        self.assertEqual(self.rows()[1]["text"], "")

    def test_partial_append_unchanged_full_and_deletion(self):
        path = self.write([self.entry()])
        pending = json.dumps(self.entry("u2")).encode()
        with path.open("ab") as fh:
            fh.write(pending)
        self.assertEqual(self.sync()["rows_written"], 1)
        original_read = Path.read_bytes
        def guarded_read(path):
            if path == self.source / "projects/project/session.jsonl":
                raise AssertionError("unchanged source read")
            return original_read(path)
        with patch.object(Path, "read_bytes", guarded_read):
            stats = self.sync()
        self.assertEqual((stats["files_changed"], stats["partitions_written"]), (0, 0))
        self.assertEqual(self.sync(full=True)["rows_written"], 0)
        with path.open("ab") as fh:
            fh.write(b"\n")
        self.assertEqual(self.sync()["rows_written"], 1)
        self.write([self.entry("u2"), self.entry("u3")], append=True)
        self.assertEqual(self.sync()["rows_written"], 1)
        before = self.manifest()
        path.unlink()
        self.assertEqual(self.sync()["files_gone"], 1)
        after = self.manifest()
        self.assertEqual(before["files"], after["files"])
        self.assertEqual(before["containers"], after["containers"])
        self.assertEqual([r["uuid"] for r in self.rows()], ["u1", "u2", "u3"])

    def test_subagent(self):
        path = self.write([self.entry()], "projects/project/session/subagents/agent-a.jsonl")
        path.with_suffix(".meta.json").write_text(json.dumps({"agentType": "research", "description": "synthetic"}))
        self.sync()
        container = self.manifest()["containers"]["session.agent-a"]
        self.assertEqual((container["kind"], container["session"], container["agent"], container["project"]),
                         ("subagent", "session", "agent-a", "project"))
        self.assertEqual((container["agent_type"], container["description"]), ("research", "synthetic"))
        self.assertEqual(self.rows("session.agent-a")[0]["agent"], "agent-a")

    def test_workflow_agents_merge_copies_and_skip_journal(self):
        base = "projects/project/session/subagents/workflows"
        path = self.write([self.entry("w1")], f"{base}/wf_1/agent-b.jsonl")
        path.with_suffix(".meta.json").write_text(json.dumps({"agentType": "worker", "description": "synthetic"}))
        self.write([self.entry("w1"), self.entry("w2")], f"{base}/wf_2/agent-b.jsonl")
        self.write([self.entry("journal")], f"{base}/wf_1/journal.jsonl")
        stats = self.sync()
        man = self.manifest()
        self.assertEqual((stats["files_seen"], stats["errors"]), (2, 0))
        self.assertEqual(sorted(man["containers"]), ["session.agent-b"])
        container = man["containers"]["session.agent-b"]
        self.assertEqual((container["kind"], container["session"], container["agent"], container["project"]),
                         ("subagent", "session", "agent-b", "project"))
        self.assertEqual(sorted(r["uuid"] for r in self.rows("session.agent-b")), ["w1", "w2"])
        self.assertEqual(container["agent_type"], "worker")

    def test_append_during_read_keeps_before_fingerprint(self):
        path = self.write([self.entry()])
        before = path.stat()
        original_read = Path.read_bytes

        def growing_read(source):
            data = original_read(source)
            if source == path:
                self.write([self.entry("u2")], append=True)
                with path.open("ab") as fh:
                    fh.write(json.dumps(self.entry("u3")).encode())
                data = original_read(source)
            return data

        with patch.object(Path, "read_bytes", growing_read):
            stats = self.sync()
        self.assertEqual(stats["errors"], 0)
        self.assertEqual(stats["rows_written"], 2)
        fingerprint = self.manifest()["files"][path.relative_to(self.source).as_posix()]
        self.assertEqual((fingerprint["size"], fingerprint["mtime_ns"]),
                         (before.st_size, before.st_mtime_ns))
        stats = self.sync()
        self.assertEqual((stats["files_changed"], stats["rows_written"], stats["errors"]), (1, 0, 0))
        with path.open("ab") as fh:
            fh.write(b"\n")
        self.assertEqual(self.sync()["rows_written"], 1)
        self.assertEqual([row["uuid"] for row in self.rows()], ["u1", "u2", "u3"])

    def test_invalid_json_line_is_counted_without_error(self):
        path = self.write([self.entry()])
        with path.open("ab") as fh:
            fh.write(b"not json\n")
        self.write([self.entry("u2")], append=True)
        stats = self.sync()
        self.assertEqual((stats["lines_bad"], stats["errors"]), (1, 0))
        self.assertEqual([row["uuid"] for row in self.rows()], ["u1", "u2"])

    def test_invalid_transcript_keys_and_timestamps_are_bad_lines(self):
        invalid = [self.entry(uuid=value) for value in (None, "", "bad:id", "bad\nkey", 1, [], False)]
        invalid += [self.entry(sessionId=value) for value in (None, "", "bad:id", "bad\x00key", 1)]
        invalid += [self.entry(timestamp=value) for value in
                    (None, "", "invalid", "2026-01-01T00:00:00", 1)]
        for key in ("uuid", "sessionId", "timestamp"):
            obj = self.entry()
            del obj[key]
            invalid.append(obj)
        self.write(invalid + [self.entry("valid")])
        stats = self.sync()
        self.assertEqual((stats["lines_bad"], stats["errors"], stats["rows_written"]),
                         (len(invalid), 0, 1))
        self.assertEqual([row["uuid"] for row in self.rows()], ["valid"])
        self.assertEqual(self.sync()["lines_bad"], 0)

    def test_invalid_history_sessions_are_bad_lines(self):
        invalid = [{"sessionId": value, "timestamp": 1, "display": "synthetic"}
                   for value in (None, "", "bad:id", "bad\nkey", 1, [], False)]
        invalid.append({"timestamp": 1, "display": "synthetic"})
        self.write(invalid + [{"sessionId": "orphan", "timestamp": 1, "display": "synthetic"}],
                   "history.jsonl")
        stats = self.sync(clock=2 * cc.DAY_MS)
        self.assertEqual((stats["lines_bad"], stats["errors"], stats["rows_written"]),
                         (len(invalid), 0, 1))
        self.assertEqual([row["session"] for row in self.rows("history")], ["orphan"])

    def test_subagent_optional_metadata_failures_do_not_block_rows(self):
        original_read = Path.read_text
        for name, data in (("missing", None), ("invalid-json", "not JSON"),
                           ("invalid-encoding", b"\xff"), ("list", "[]"), ("null", "null"),
                           ("string", '"synthetic"'), ("unreadable", "{}"), ("vanished", "{}")):
            with self.subTest(name=name):
                path = self.write([self.entry()], f"projects/project/session/subagents/{name}.jsonl")
                meta = path.with_suffix(".meta.json")
                if data is not None:
                    meta.write_bytes(data if isinstance(data, bytes) else data.encode())

                def read_text(source, *args, **kwargs):
                    if source == meta and name == "unreadable":
                        raise PermissionError("synthetic")
                    if source == meta and name == "vanished":
                        raise FileNotFoundError("synthetic")
                    return original_read(source, *args, **kwargs)

                with patch.object(Path, "read_text", read_text):
                    stats = self.sync()
                self.assertEqual((stats["errors"], stats["lines_bad"], stats["rows_written"]), (0, 0, 1))
                container = self.manifest()["containers"][f"session.{name}"]
                self.assertEqual((container["agent_type"], container["description"]), ("", ""))
                self.assertEqual(len(self.rows(f"session.{name}")), 1)

    def test_history_backfill_and_manifest_session_exclusion(self):
        clock = 2 * cc.DAY_MS
        self.write([self.entry()])
        self.write([self.entry(sessionId="sub")], "projects/project/sub/subagents/agent.jsonl")
        def history(session, timestamp):
            return {"sessionId": session, "timestamp": timestamp, "project": "synthetic",
                    "display": "old prompt", "pastedContents": {"secret": "never archive"}}
        self.write([history("session", 1), history("sub", 1), history("young", clock - 1),
                    history("boundary", clock - cc.DAY_MS), history("orphan", 1)], "history.jsonl")
        self.sync(clock=clock)
        rows = self.rows("history")
        self.assertEqual([r["session"] for r in rows], ["orphan"])
        self.assertEqual((rows[0]["uuid"], rows[0]["n"], rows[0]["ts"], rows[0]["role"]),
                         ("h1.orphan", 1, "1970-01-01T00:00:00.001Z", "prompt"))
        self.assertNotIn("pastedContents", json.dumps(rows))
        (self.source / "projects/project/session.jsonl").unlink()
        self.write([history("session", 2)], "history.jsonl", append=True)
        self.sync(clock=clock)
        self.assertEqual(len(self.rows("history")), 1)

    def test_limit_and_stats_privacy(self):
        self.write([self.entry()], "projects/project/a.jsonl")
        self.write([self.entry()], "projects/project/b.jsonl")
        stats = self.sync(max_items=1)
        self.assertTrue(stats["limited"])
        self.assertEqual(stats["files_changed"], 1)
        self.assertEqual(stats["files_seen"], 2)
        self.assertEqual(set(stats), {"status", "files_seen", "files_changed", "files_gone", "rows_written",
                                      "partitions_written", "errors", "lines_bad", "limited"})
        self.assertTrue(all(isinstance(v, (int, bool)) for k, v in stats.items() if k != "status"))
        self.assertEqual(self.sync(max_items=1)["rows_written"], 1)

    def test_config(self):
        for source in ("", "relative", str(self.source / "missing"), None):
            with self.subTest(source=source), self.assertRaisesRegex(CollectorError, "^source_missing$"):
                cc.sync_config(SimpleNamespace(cc_claude_dir=source), auth_only=True)
        cfg = SimpleNamespace(cc_claude_dir=str(self.source), sources={"cc_archive": self.root})
        self.assertEqual(cc.sync_config(cfg, auth_only=True),
                         {"status": "local_ready", "authentication_required": False})
        self.assertFalse(self.root.exists())
        self.assertEqual(cc.sync_config(cfg)["status"], "ok")

    def test_read_failure_and_vanish(self):
        path = self.write([self.entry()])
        with patch.object(Path, "read_bytes", side_effect=PermissionError("private path")):
            stats = self.sync()
        self.assertEqual(stats["errors"], 1)
        self.assertEqual(self.manifest()["containers"], {})
        with patch.object(Path, "read_bytes", side_effect=FileNotFoundError("private path")):
            stats = self.sync()
        self.assertEqual((stats["files_gone"], stats["errors"]), (1, 0))
        self.sync()
        previous = self.manifest()["containers"]
        self.write([self.entry("new")], append=True)
        with patch.object(Path, "read_bytes", side_effect=PermissionError("private path")):
            self.sync()
        self.assertEqual(previous, self.manifest()["containers"])

    def test_invalid_container(self):
        path = self.write([self.entry()])
        with patch.object(cc, "_sources", return_value=iter([(path, "bad:id", "session", "bad:id", "", "project")])):
            stats = self.sync()
        self.assertEqual(stats["errors"], 1)
        self.assertEqual(self.manifest()["containers"], {})

    def test_archive_exceptions_are_categories_only(self):
        with patch.object(cc.archiveio, "load", side_effect=OSError("synthetic private path")):
            with self.assertRaisesRegex(CollectorError, "^archive_error$"):
                self.sync()
        with patch.object(cc.archiveio, "load", side_effect=CollectorError("bad_archive")):
            with self.assertRaisesRegex(CollectorError, "^bad_archive$"):
                self.sync()

    def test_partition_bound_and_monotonic_clock(self):
        self.write([self.entry(str(i)) for i in range(10001)])
        self.assertEqual(self.sync()["partitions_written"], 2)
        container = self.manifest()["containers"]["session"]
        files = container["files"]
        self.assertEqual(files, sorted(set(files)))
        self.assertEqual([json.loads((self.root / f).read_text())["rowCount"] for f in files], [10000, 1])
        future = "29990101T000000000000Z"
        old = self.root / files[-1]
        new = old.with_name(future + ".json")
        old.rename(new)
        man = self.manifest()
        man["containers"]["session"]["files"][-1] = new.relative_to(self.root).as_posix()
        (self.root / "manifest.json").write_text(json.dumps(man))
        self.write([self.entry("next")], append=True)
        self.sync()
        self.assertGreater(Path(self.manifest()["containers"]["session"]["files"][-1]).stem, future)


if __name__ == "__main__":
    unittest.main()
