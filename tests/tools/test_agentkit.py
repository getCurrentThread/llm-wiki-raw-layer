# -*- coding: utf-8 -*-
"""tools/agentkit.py: the templates rendered, and
install / status / uninstall in a throwaway home whose `claude` CLI is a stand-in that edits that home's
.claude.json. Every round trip ends byte for byte where it started."""
import json
import re
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools import agentkit as K

APPS = ["claudecode", "discord", "gmail", "kakaotalk", "mattermost", "notion"]
FOREIGN = (f"# Mine\r\n\r\n{K.BEGIN}\r\n- old rule\r\n{K.END}\r\n# Other notes\r\n\r\n- keep me\r\n")  # a block not ours
REST = "# Mine\r\n\r\n# Other notes\r\n\r\n- keep me\r\n"                   # FOREIGN without its block
OTHER = "\ufeff# Mine\n\n- a rule of my own"               # a BOM, no newline at the end


class Home(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="llmw-home-"))
        self.state = Path(tempfile.mkdtemp(prefix="llmw-state-"))
        for d in (self.home, self.state):
            self.addCleanup(shutil.rmtree, d, True)
        self.server = self.state / "mcp_rawsearch.py"
        self.server.write_text("", encoding="utf-8")
        patcher = mock.patch.object(K, "MCP_SERVER", self.server)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.calls = []
        self.failing = None                # argv -> True: the stand-in CLI fails that call (rc 1)

    def claude(self, argv):
        """The stand-in CLI: mcp add-json / remove on <home>/.claude.json."""
        self.calls.append(argv)
        if self.failing and self.failing(argv):
            return 1
        p = self.home / ".claude.json"
        d = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
        servers = d.setdefault("mcpServers", {})
        if argv[:2] == ["mcp", "add-json"]:
            servers[argv[4]] = json.loads(argv[5])
        elif argv[:2] == ["mcp", "remove"]:
            servers.pop(argv[4], None)
        p.write_text(json.dumps(d), encoding="utf-8")
        return 0

    def kit(self, python=None):
        return K.Kit(home=self.home, state=self.state, python=python, claude=self.claude, found=APPS)

    def snapshot(self):
        """Every file and folder under home but .claude.json (the stand-in CLI's)."""
        return {p.relative_to(self.home).as_posix(): (p.read_bytes() if p.is_file() else None)
                for p in self.home.rglob("*") if p.name != ".claude.json"}

    def servers(self):
        return json.loads((self.home / ".claude.json").read_text(encoding="utf-8"))["mcpServers"]

    def put(self, rel, text):
        p = self.home / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(text if isinstance(text, bytes) else text.encode("utf-8"))

    def edit(self, rel, old, new):
        p = self.home / rel
        p.write_bytes(p.read_bytes().replace(old, new))

    def add_foreign_server(self):
        self.claude(["mcp", "add-json", "--scope", "user", K.MCP_NAME, json.dumps({"type": "stdio",
                                                                                   "command": "other.exe"})])
        self.calls.clear()


class Rendering(unittest.TestCase):
    def test_no_leftovers(self):
        vals = K.values(APPS)
        for text in [K.artifact(t, kind, vals) for t, kind, _rel in K.GLOBAL_FILES] + [K.block(vals)]:
            with self.subTest(head=text[:40]):
                self.assertNotIn("{{", text)
                self.assertNotIn("knowledge-base", text)
                self.assertNotIn("knowledge-retriever", text)
                self.assertIn("kakaotalk", text)

    def test_frontmatter(self):
        vals = K.values(APPS)
        for template, kind, rel in K.GLOBAL_FILES:
            with self.subTest(rel=rel):
                text = K.artifact(template, kind, vals)
                m = re.match(r"---\n(.*?)\n---\n", text, re.S)
                self.assertIsNotNone(m)
                keys = []
                for ln in m.group(1).splitlines():
                    key, _sep, value = ln.partition(": ")
                    keys.append(key)
                    self.assertNotIn(": ", value, f"{key}: a plain YAML scalar cannot hold ': '")
                    self.assertNotRegex(value, r"^[\[\]{}&*!|>'\"%@`#,?-]")
                    self.assertNotIn(" #", value)
                self.assertEqual(keys, [k for k in K.FRONT[kind] if k in keys])
                self.assertIn("description", keys)
                name = rel.split("/")[-2] if rel.endswith("SKILL.md") else rel.split("/")[-1][:-3]
                self.assertIn(f"name: {name}\n", text)


class Markers(unittest.TestCase):
    def test_block(self):
        text = f"a\n{K.BEGIN}\nx\n{K.END}\nb\n"
        start, end = K.find_block(text)
        self.assertEqual(text[start:end], f"{K.BEGIN}\nx\n{K.END}\n")
        for bad in (f"{K.BEGIN}\n{K.BEGIN}\n{K.END}\n", f"{K.END}\n{K.BEGIN}\n", f"{K.BEGIN}\nx\n"):
            with self.subTest(bad=bad), self.assertRaises(K.Refused):
                K.find_block(bad)

class InstallUninstall(Home):
    def test_fresh_home_round_trip(self):
        rows, _m = self.kit().status()
        self.assertEqual(set(rows.values()), {"absent"})
        plan = self.kit().install()
        self.assertEqual(len(plan), 5)                      # the block, three files, the MCP server
        rows, mismatch = self.kit().status()
        self.assertFalse(mismatch)
        self.assertEqual(set(rows.values()), {"current"})
        self.assertEqual(self.kit().install(), [])          # idempotent
        server = self.servers()[K.MCP_NAME]
        self.assertEqual(server["args"][3:], ["--apps", ",".join(APPS)])
        self.kit().uninstall()
        self.assertEqual(self.snapshot(), {})               # the folders it created are gone too
        self.assertEqual(self.servers(), {})
        self.assertEqual([c[:2] for c in self.calls], [["mcp", "add-json"], ["mcp", "remove"]])

    def test_an_existing_file_is_kept_byte_for_byte(self):
        self.put(".claude/CLAUDE.md", OTHER)
        self.put(".claude/agents/mine.md", "mine\n")
        before = self.snapshot()
        self.kit().install()
        raw = (self.home / ".claude/CLAUDE.md").read_bytes()
        self.assertTrue(raw.startswith(OTHER.encode("utf-8")))
        self.assertIn(K.BEGIN.encode("utf-8"), raw)
        self.kit().uninstall()
        self.assertEqual(self.snapshot(), before)

    def test_a_foreign_block_is_refused(self):
        self.put(".claude/CLAUDE.md", FOREIGN)
        before = self.snapshot()
        self.assertEqual(self.kit().status()[0][f"{K.CLAUDE_MD} (block)"], "foreign")
        for dry in (True, False):
            with self.subTest(dry_run=dry), self.assertRaises(K.Refused):
                self.kit().install(dry_run=dry)
        self.assertEqual(self.snapshot(), before)
        self.assertFalse((self.state / "manifest.json").exists())
        self.put(".claude/CLAUDE.md", REST)                 # the owner takes it out by hand
        self.kit().install()
        text = (self.home / ".claude/CLAUDE.md").read_bytes().decode("utf-8")
        self.assertTrue(text.startswith(REST))
        self.assertTrue(text.split(K.BEGIN)[1].startswith("\r\n"))      # the block takes the file's newlines
        self.kit().uninstall()
        self.assertEqual((self.home / ".claude/CLAUDE.md").read_bytes(), REST.encode("utf-8"))

    def test_hand_changes_are_kept(self):
        self.kit().install()
        skill = self.home / ".claude/skills/raw-search/SKILL.md"
        skill.write_bytes(skill.read_bytes() + b"\nmine\n")
        doc = self.home / ".claude/CLAUDE.md"
        doc.write_bytes(doc.read_bytes().replace(b"Privacy (strict)", b"Privacy (mine)"))
        rows, _m = self.kit().status()
        self.assertEqual(rows[".claude/skills/raw-search/SKILL.md"], "modified")
        self.assertEqual(rows[".claude/CLAUDE.md (block)"], "modified")
        with self.assertRaises(K.Refused):
            self.kit().install()
        _plan, kept = self.kit().uninstall()
        self.assertEqual(len(kept), 2)
        self.assertTrue(skill.exists())
        self.assertIn(b"Privacy (mine)", doc.read_bytes())
        self.assertFalse((self.home / ".claude/agents/raw-retriever.md").exists())

    def test_foreign_items_are_refused(self):
        self.put(".claude/agents/raw-retriever.md", "남의 에이전트\r\n".encode("cp949"))    # not even UTF-8
        self.add_foreign_server()
        before, servers = self.snapshot(), self.servers()
        with self.assertRaises(K.Refused):
            self.kit().install()
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.servers(), servers)
        self.kit().uninstall()                              # not ours: left alone
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.servers(), servers)

    def test_a_claude_md_that_is_not_utf8(self):
        self.put(".claude/CLAUDE.md", "# 내 규칙\r\n".encode("cp949"))
        before = self.snapshot()
        kit = self.kit()
        self.assertEqual(kit.status()[0][f"{K.CLAUDE_MD} (block)"], "unreadable")
        self.assertIn("not UTF-8", kit.problems[0])
        with self.assertRaises(K.Refused):
            self.kit().install()
        self.assertEqual(self.snapshot(), before)

    def test_a_failed_add_puts_the_entry_back(self):
        self.kit().install()
        servers, recorded = self.servers(), self.kit().manifest["mcp"]
        other = str(Path(tempfile.gettempdir()) / "python.exe")
        self.failing = lambda argv: argv[:2] == ["mcp", "add-json"] and json.loads(argv[5])["command"] == other
        with self.assertRaises(RuntimeError):
            self.kit(python=other).install()
        self.assertEqual(self.servers(), servers)               # removed, the add failed, put back
        self.assertEqual(self.kit().manifest["mcp"], recorded)  # still this tool's entry

    def test_an_older_manifest_still_loads(self):
        """A manifest from the version with backups (backups, mcp_pending, block.replaces) works as it is."""
        self.kit().install()
        m = json.loads((self.state / "manifest.json").read_text(encoding="utf-8"))
        m["backups"] = {"mcp": {"type": "stdio", "command": "other.exe"}}
        m["mcp_pending"] = {"type": "stdio", "command": "other.exe"}
        m["block"]["replaces"] = "a1b2"
        (self.state / "manifest.json").write_text(json.dumps(m), encoding="utf-8")
        self.assertEqual(set(self.kit().status()[0].values()), {"current"})
        self.assertEqual(self.kit().install(), [])
        self.kit().uninstall()
        self.assertEqual(self.snapshot(), {})
        self.assertEqual(self.servers(), {})
        self.assertNotIn("backups", json.loads((self.state / "manifest.json").read_text(encoding="utf-8")))

    def test_a_new_interpreter_makes_the_server_stale(self):
        self.kit().install()
        other = str(Path(tempfile.gettempdir()) / "python.exe")
        rows, _m = self.kit(python=other).status()
        self.assertEqual(rows[f"mcp {K.MCP_NAME}"], "stale")
        self.kit(python=other).install()
        self.assertEqual([c[:2] for c in self.calls[-2:]], [["mcp", "remove"], ["mcp", "add-json"]])
        self.assertEqual(self.servers()[K.MCP_NAME]["command"], other)

    def test_a_template_change_makes_the_files_stale(self):
        self.kit().install()
        kit = self.kit()
        kit.vals = dict(kit.vals, APPS="discord, gmail")
        rows, _m = kit.status()
        self.assertEqual(rows[".claude/CLAUDE.md (block)"], "stale")
        self.assertEqual(rows[".claude/agents/raw-retriever.md"], "stale")
        kit.install()
        self.assertEqual(set(kit.status()[0].values()), {"current"})

    def test_project_mismatch(self):
        self.kit().install()
        m = json.loads((self.state / "manifest.json").read_text(encoding="utf-8"))
        m["project"] = str(Path(tempfile.gettempdir()) / "elsewhere")
        (self.state / "manifest.json").write_text(json.dumps(m), encoding="utf-8")
        self.assertTrue(self.kit().status()[1])


class Cli(Home):
    def test_exit_codes(self):
        with mock.patch.object(K, "_out"), mock.patch.object(K, "_err"):
            self.assertEqual(K.main(["status"], kit=self.kit()), 3)
            self.assertEqual(K.main(["install", "--dry-run"], kit=self.kit()), 0)
            self.assertEqual(self.snapshot(), {})
            self.assertEqual(K.main(["install"], kit=self.kit()), 0)
            self.assertEqual(K.main(["status"], kit=self.kit()), 0)
            self.assertEqual(K.main(["uninstall"], kit=self.kit()), 0)
            self.put(".claude/agents/raw-retriever.md", "someone else's agent\n")
            self.assertEqual(K.main(["install"], kit=self.kit()), 2)

    def test_diff_is_masked(self):
        printed = []
        with mock.patch("builtins.print", lambda *a, **k: printed.append(" ".join(map(str, a)))):
            self.assertEqual(K.main(["install", "--dry-run"], kit=self.kit()), 0)
        text = "\n".join(printed)
        self.assertIn("+++ <HOME>/.claude/CLAUDE.md", text)
        self.assertNotIn(str(Path.home()), text)
