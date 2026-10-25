# -*- coding: utf-8 -*-
"""tools/agentkit.py: the templates rendered for both targets, the checkout's generated files against them, and
install / status / uninstall in a throwaway home whose `claude` CLI is a stand-in that edits that home's
.claude.json. Every round trip ends byte for byte where it started."""
import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools import agentkit as K

APPS = ["claudecode", "discord", "gmail", "kakaotalk", "mattermost", "notion"]
LEGACY = ("# llm-wiki-raw-layer\r\n\r\nRepo: `X:\\checkout`\r\n\r\n## Core rules\r\n\r\n- old rule\r\n\r\n"
          "# Other notes\r\n\r\n- keep me\r\n")
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
        for target in ("global", "repo"):
            vals = K.values(target, APPS)
            texts = [K.artifact(t, kind, vals) for t, kind, _rel in K.REPO_FILES]
            if target == "global":
                texts.append(K.block(vals))
            for text in texts:
                with self.subTest(target=target, head=text[:40]):
                    self.assertNotIn("{{", text)
                    self.assertNotIn("knowledge-base", text)
                    self.assertNotIn("knowledge-retriever", text)
                    self.assertIn("kakaotalk", text)
                    if target == "repo":
                        self.assertNotIn(K.PROJECT.as_posix(), text)
                        self.assertIsNone(re.search(r"[A-Za-z]:[\\/]Users", text))

    def test_frontmatter(self):
        vals = K.values("global", APPS)
        for template, kind, rel in K.REPO_FILES:
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

    def test_the_checkout_matches_the_templates(self):
        plan = K.repo_plan()
        self.assertEqual([(a, r) for a, r, _t in plan], [], "run `python -s -B tools/bootstrap.py agents sync-repo`")

    def test_missing_variable(self):
        with self.assertRaises(ValueError):
            K.render("rules.md", K.values("repo", APPS))


class Markers(unittest.TestCase):
    def test_block(self):
        text = f"a\n{K.BEGIN}\nx\n{K.END}\nb\n"
        start, end = K.find_block(text)
        self.assertEqual(text[start:end], f"{K.BEGIN}\nx\n{K.END}\n")
        for bad in (f"{K.BEGIN}\n{K.BEGIN}\n{K.END}\n", f"{K.END}\n{K.BEGIN}\n", f"{K.BEGIN}\nx\n"):
            with self.subTest(bad=bad), self.assertRaises(K.Refused):
                K.find_block(bad)

    def test_legacy_section(self):
        start, end = K.find_legacy(LEGACY)
        self.assertEqual(start, 0)
        self.assertTrue(LEGACY[end:].startswith("# Other notes"))
        fenced = "# llm-wiki-raw-layer\n```\n# not a heading\n```\nrest\n"
        self.assertEqual(K.find_legacy(fenced), (0, len(fenced)))
        self.assertIsNone(K.find_legacy("# llm-wiki-raw-layer (raw ledgers)\n"))
        text = f"# llm-wiki-raw-layer\nold\n{K.BEGIN}\nx\n{K.END}\n"
        self.assertEqual(K.find_legacy(text, K.find_block(text)), (0, text.index(K.BEGIN)))


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
        self.assertEqual(server["args"][3:], ["--apps", ",".join(APPS), "--named-only", "kakaotalk"])
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

    def test_legacy_adopt_and_restore(self):
        self.put(".claude/CLAUDE.md", LEGACY)
        self.put(".claude/agents/knowledge-retriever.md", "old agent\n")
        before = self.snapshot()
        rows, _m = self.kit().status()
        self.assertEqual(rows[".claude/CLAUDE.md (hand-made section)"], "legacy")
        self.assertEqual(rows[".claude/agents/knowledge-retriever.md"], "legacy")
        with self.assertRaises(K.Refused):
            self.kit().install()
        self.assertEqual(self.snapshot(), before)
        self.assertTrue(self.kit().install(dry_run=True, adopt=True))
        self.assertEqual(self.snapshot(), before)
        self.assertFalse((self.state / "manifest.json").exists())
        self.kit().install(adopt=True)
        text = (self.home / ".claude/CLAUDE.md").read_bytes().decode("utf-8")
        self.assertNotIn("old rule", text)
        self.assertTrue(text.endswith("\r\n# Other notes\r\n\r\n- keep me\r\n"))
        self.assertTrue(text.split(K.BEGIN)[1].startswith("\r\n"))      # the block takes the file's newlines
        self.assertFalse((self.home / ".claude/agents/knowledge-retriever.md").exists())
        rows, _m = self.kit().status()
        self.assertEqual(set(rows.values()), {"current"})
        self.kit().uninstall(restore=True)
        self.assertEqual(self.snapshot(), before)

    def test_uninstall_without_restore_leaves_the_rest(self):
        self.put(".claude/CLAUDE.md", LEGACY)
        self.kit().install(adopt=True)
        self.kit().uninstall()
        self.assertEqual((self.home / ".claude/CLAUDE.md").read_bytes().decode("utf-8"),
                         "# Other notes\r\n\r\n- keep me\r\n")

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

    def test_force_backs_up_what_it_replaces(self):
        self.put(".claude/agents/raw-retriever.md", "someone else's agent\n")
        self.add_foreign_server()
        before, servers = self.snapshot(), self.servers()
        with self.assertRaises(K.Refused):
            self.kit().install()
        self.kit().install(force=True)
        self.assertNotEqual(self.snapshot(), before)
        self.assertEqual(self.servers()[K.MCP_NAME]["command"], self.kit().python)
        self.kit().uninstall(restore=True)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.servers(), servers)

    def test_a_kept_item_keeps_its_backup(self):
        # --restore keeps a block or file changed by hand, and its backup with it: following the hint
        # (--restore --force) then still puts the original back
        self.put(".claude/CLAUDE.md", LEGACY)
        self.put(".claude/agents/raw-retriever.md", "someone else's agent\n")
        before = self.snapshot()
        self.kit().install(adopt=True, force=True)
        self.edit(".claude/CLAUDE.md", b"Privacy (strict)", b"Privacy (mine)")
        self.edit(".claude/agents/raw-retriever.md", b"You search", b"You, mine, search")
        _plan, kept = self.kit().uninstall(restore=True)
        self.assertEqual(len(kept), 2)
        kit = self.kit()
        kit.uninstall(restore=True, force=True)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(len([n for n in kit.notes if "is saved at" in n]), 2)     # the hand-changed copies

    def test_force_over_a_hand_changed_block_keeps_the_original(self):
        self.put(".claude/CLAUDE.md", LEGACY)
        before = self.snapshot()
        self.kit().install(adopt=True)
        self.edit(".claude/CLAUDE.md", b"Privacy (strict)", b"Privacy (mine)")
        self.kit().install(force=True)              # backs up the edited block for reference only
        self.kit().uninstall(restore=True)
        self.assertEqual(self.snapshot(), before)

    def test_backups_belong_to_one_install(self):
        # a plain uninstall forgets the backups of what it removes: a later install and --restore put back only
        # what that later install replaced
        agent = ".claude/agents/raw-retriever.md"
        self.put(agent, "first agent\n")
        self.kit().install(force=True)
        kit = self.kit()
        kit.uninstall()
        self.assertTrue(any("not put back" in n for n in kit.notes))
        self.put(agent, "second agent\n")
        self.kit().install(force=True)
        self.kit().uninstall(restore=True)
        self.assertEqual((self.home / agent).read_bytes(), b"second agent\n")
        self.put(".claude/CLAUDE.md", LEGACY)
        self.kit().install(adopt=True, force=True)
        self.kit().uninstall()
        rest = "# Other notes\r\n\r\n- keep me\r\n"
        self.assertEqual((self.home / ".claude/CLAUDE.md").read_bytes().decode("utf-8"), rest)
        self.kit().install(force=True)
        self.kit().uninstall(restore=True)                      # the old section does not come back
        self.assertEqual((self.home / ".claude/CLAUDE.md").read_bytes().decode("utf-8"), rest)

    def test_backups_in_the_same_instant_never_overwrite(self):
        agent = ".claude/agents/raw-retriever.md"
        self.put(agent, "the original\n")
        with mock.patch.object(K, "_stamp", lambda: "20260101T000000000000Z"):
            self.kit().install(force=True)
            self.edit(agent, b"You search", b"You, mine, search")
            self.kit().install(force=True)       # the edited copy: kept for reference only
            self.kit().uninstall(restore=True)
            self.assertEqual((self.home / agent).read_bytes(), b"the original\n")
            self.kit().install(force=True)       # the same key again, in the same instant
            self.kit().uninstall(restore=True)
        self.assertEqual((self.home / agent).read_bytes(), b"the original\n")
        saved = [p.read_bytes() for p in (self.state / "backups").rglob("*") if p.is_file()]
        self.assertEqual(len(saved), 3)
        self.assertEqual(saved.count(b"the original\n"), 2)

    def test_a_failed_add_puts_the_entry_back(self):
        self.add_foreign_server()
        servers = self.servers()
        self.failing = lambda argv: argv[:2] == ["mcp", "add-json"] and "other.exe" not in argv[5]
        with self.assertRaises(RuntimeError):
            self.kit().install(force=True)
        self.assertEqual(self.servers(), servers)               # removed, the add failed, put back
        self.assertIsNone(self.kit().manifest["mcp"])           # and not recorded as this tool's
        self.assertNotIn("mcp", self.kit().manifest["backups"])     # nor kept as an original it replaced

    def test_a_failed_restore_is_finished_by_the_next(self):
        self.add_foreign_server()
        servers = self.servers()
        self.kit().install(force=True)
        self.failing = lambda argv: argv[:2] == ["mcp", "add-json"]
        with self.assertRaises(RuntimeError):
            self.kit().uninstall(restore=True)
        self.assertEqual(self.servers(), {})
        self.failing = None
        self.assertEqual(self.kit().status()[0][f"mcp {K.MCP_NAME}"], "absent")
        self.kit().uninstall(restore=True)
        self.assertEqual(self.servers(), servers)

    def test_the_installed_interpreter_is_kept(self):
        py = self.state / "py" / "python.exe"
        py.parent.mkdir()
        py.write_bytes(b"")
        self.kit(python=str(py)).install()
        rows, _m = self.kit().status()                          # run by another interpreter
        self.assertEqual(rows[f"mcp {K.MCP_NAME}"], "current")
        self.calls.clear()
        self.assertEqual(self.kit().install(), [])
        self.assertEqual(self.calls, [])
        with mock.patch.object(K, "_out"):
            self.assertEqual(K.main(["status"], kit=self.kit()), 0)
        py.unlink()                                            # a removed interpreter: install repoints
        self.assertEqual(self.kit().status()[0][f"mcp {K.MCP_NAME}"], "stale")
        self.kit().install()
        self.assertEqual(self.servers()[K.MCP_NAME]["command"], self.kit().python)
        self.assertNotEqual(self.kit().python, str(py))

    def test_files_that_are_not_utf8(self):
        agent = ".claude/agents/raw-retriever.md"
        ansi = "남의 에이전트\r\n".encode("cp949")
        self.put(agent, ansi)
        with self.assertRaises(K.Refused):
            self.kit().install()
        self.assertTrue(self.kit().install(dry_run=True, force=True))
        self.kit().install(force=True)
        self.kit().uninstall(restore=True)
        self.assertEqual((self.home / agent).read_bytes(), ansi)   # back byte for byte
        self.put(".claude/CLAUDE.md", "# 내 규칙\r\n".encode("cp949"))
        before = self.snapshot()
        kit = self.kit()
        self.assertEqual(kit.status()[0][f"{K.CLAUDE_MD} (block)"], "unreadable")
        self.assertIn("not UTF-8", kit.problems[0])
        with self.assertRaises(K.Refused):
            self.kit().install()
        self.assertEqual(self.snapshot(), before)

    def test_an_add_that_landed_before_an_interrupt_is_recorded(self):
        def landed_then_interrupted(argv):
            self.claude(argv)
            if argv[:2] == ["mcp", "add-json"] and "other.exe" not in argv[5]:
                raise KeyboardInterrupt
            return 0
        self.add_foreign_server()
        servers = self.servers()
        kit = K.Kit(home=self.home, state=self.state, claude=landed_then_interrupted, found=APPS)
        with self.assertRaises(KeyboardInterrupt):
            kit.install(force=True)
        self.assertEqual(self.kit().status()[0][f"mcp {K.MCP_NAME}"], "current")
        self.assertIsNotNone(self.kit().manifest["mcp"])
        self.kit().uninstall(restore=True)
        self.assertEqual(self.servers(), servers)               # the original, not this tool's entry

    def test_an_add_cut_off_by_a_kill_is_settled_by_the_next_run(self):
        py = self.state / "py" / "python.exe"
        py.parent.mkdir()
        py.write_bytes(b"")
        self.add_foreign_server()
        servers = self.servers()
        self.kit(python=str(py)).install(force=True)
        path = self.state / "manifest.json"

        def killed():                                       # the manifest a kill during the add leaves
            m = json.loads(path.read_text(encoding="utf-8"))
            m["mcp_pending"], m["mcp"] = m["mcp"], None
            path.write_text(json.dumps(m), encoding="utf-8")
        killed()                                            # the add landed
        py.unlink()                                         # and the next run wants another interpreter
        self.assertEqual(self.kit().status()[0][f"mcp {K.MCP_NAME}"], "stale")
        self.kit().uninstall(restore=True)
        self.assertEqual(self.servers(), servers)           # the original back, nothing of this tool's left
        self.claude(["mcp", "remove", "--scope", "user", K.MCP_NAME])
        self.kit().install()
        killed()
        self.claude(["mcp", "remove", "--scope", "user", K.MCP_NAME])  # the add never landed
        self.add_foreign_server()                           # and the owner registers one of their own
        self.assertEqual(self.kit().status()[0][f"mcp {K.MCP_NAME}"], "foreign")

    def test_a_failed_add_over_a_stale_entry_keeps_it(self):
        py = self.state / "py" / "python.exe"
        py.parent.mkdir()
        py.write_bytes(b"")
        self.kit(python=str(py)).install()
        servers = self.servers()
        other = str(Path(tempfile.gettempdir()) / "python.exe")
        self.failing = lambda argv: argv[:2] == ["mcp", "add-json"] and json.loads(argv[5])["command"] == other
        with self.assertRaises(RuntimeError):
            self.kit(python=other).install()
        self.assertEqual(self.servers(), servers)               # put back
        self.assertEqual(self.kit().manifest["mcp"]["command"], str(py))     # and still recorded as this tool's
        self.assertEqual(self.kit(python=other).status()[0][f"mcp {K.MCP_NAME}"], "stale")
        self.failing = None
        self.kit().uninstall()
        self.assertEqual(self.servers(), {})

    def test_a_cut_off_replacement_that_landed_is_settled(self):
        py1, py2 = self.state / "py1" / "python.exe", self.state / "py2" / "python.exe"
        for py in (py1, py2):
            py.parent.mkdir()
            py.write_bytes(b"")
        self.kit(python=str(py1)).install()
        spec = self.kit(python=str(py2)).mcp_spec()
        self.claude(["mcp", "remove", "--scope", "user", K.MCP_NAME])  # install --python py2, killed right after
        self.claude(["mcp", "add-json", "--scope", "user", K.MCP_NAME, json.dumps(spec)])     # its add landed
        path = self.state / "manifest.json"
        m = json.loads(path.read_text(encoding="utf-8"))
        m["mcp_pending"] = spec                             # over the recorded py1 entry
        path.write_text(json.dumps(m), encoding="utf-8")
        kit = self.kit()
        self.assertEqual(kit.python, str(py2))
        self.assertEqual(kit.status()[0][f"mcp {K.MCP_NAME}"], "current")
        self.kit().uninstall()
        self.assertEqual(self.servers(), {})

    def test_a_failed_add_and_put_back_keep_the_original(self):
        self.add_foreign_server()
        servers = self.servers()
        self.failing = lambda argv: argv[:2] == ["mcp", "add-json"]
        with self.assertRaises(RuntimeError):
            self.kit().install(force=True)
        self.assertNotIn(K.MCP_NAME, self.servers())
        self.assertIn("mcp", self.kit().manifest["backups"])     # the only copy of the original
        self.failing = None
        self.kit().uninstall(restore=True)
        self.assertEqual(self.servers(), servers)

    def test_a_hand_removed_block_forgets_what_it_replaced(self):
        self.put(".claude/CLAUDE.md", LEGACY)
        self.kit().install(adopt=True)
        mine = "# Other notes\r\n\r\n- keep me\r\n- another rule"     # the block removed by hand, a line added
        self.put(".claude/CLAUDE.md", mine)
        kit = self.kit()
        kit.install()
        self.assertTrue(any("removed from" in n for n in kit.notes))
        self.kit().uninstall(restore=True)
        self.assertEqual((self.home / ".claude/CLAUDE.md").read_bytes().decode("utf-8"), mine)

    def test_a_dropped_final_newline_is_not_a_change(self):
        self.kit().install()
        for rel in (".claude/CLAUDE.md", ".claude/skills/raw-search/SKILL.md"):    # an editor trims the last newline
            p = self.home / rel
            p.write_bytes(p.read_bytes().rstrip(b"\n"))
        self.assertEqual(set(self.kit().status()[0].values()), {"current"})
        self.assertEqual(self.kit().install(), [])
        self.kit().uninstall()
        self.assertEqual(self.snapshot(), {})

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

    def test_broken_markers_refused(self):
        self.put(".claude/CLAUDE.md", f"{K.BEGIN}\n{K.BEGIN}\n{K.END}\n")
        before = self.snapshot()
        with self.assertRaises(K.Refused):
            self.kit().install()
        self.assertEqual(self.snapshot(), before)

    def test_no_server_in_the_checkout(self):
        self.server.unlink()
        with self.assertRaises(K.Refused):
            self.kit().install()
        self.assertEqual(self.snapshot(), {})
        self.assertEqual(self.calls, [])


class Cli(Home):
    def test_exit_codes_and_masked_output(self):
        out, err = [], []
        with mock.patch.object(K, "_out", out.append), mock.patch.object(K, "_err", err.append):
            self.assertEqual(K.main(["status"], kit=self.kit()), 3)
            self.assertEqual(K.main(["install", "--dry-run"], kit=self.kit()), 0)
            self.assertEqual(self.snapshot(), {})
            self.assertEqual(K.main(["install"], kit=self.kit()), 0)
            self.assertEqual(K.main(["status"], kit=self.kit()), 0)
            self.put(".claude/agents/knowledge-retriever.md", "old\n")
            self.assertEqual(K.main(["install"], kit=self.kit()), 2)
            self.assertEqual(K.main(["uninstall"], kit=self.kit()), 0)
        self.assertTrue(any("would create" in x for x in out))
        self.assertTrue(any("refused" in x for x in err))

    def test_a_failing_cli_says_why(self):
        failed = subprocess.CompletedProcess([], 1, b"", b"progress\nError: the reason\n")
        with mock.patch.object(K.shutil, "which", lambda _name: "claude.exe"), \
                mock.patch.object(K.subprocess, "run", lambda *a, **k: failed), \
                self.assertRaisesRegex(RuntimeError, r"add-json llm_wiki_raw failed \(rc=1\): Error: the reason$"):
            K._claude_cli(["mcp", "add-json", "--scope", "user", K.MCP_NAME, "{}"])

    def test_notes_survive_a_failed_step(self):
        self.kit().install()
        self.edit(".claude/CLAUDE.md", b"Privacy (strict)", b"Privacy (mine)")
        self.failing = lambda argv: argv[:2] == ["mcp", "remove"]
        out, err = [], []
        with mock.patch.object(K, "_out", out.append), mock.patch.object(K, "_err", err.append):
            self.assertEqual(K.main(["uninstall", "--force"], kit=self.kit()), 1)
        self.assertTrue(any("is saved at" in x for x in out))  # the removed hand-changed block: where it is
        self.assertIn("failed", err[0])

    def test_an_interrupt_is_reported_not_raised(self):
        def interrupted(argv):
            raise KeyboardInterrupt
        err = []
        kit = K.Kit(home=self.home, state=self.state, claude=interrupted, found=APPS)
        with mock.patch.object(K, "_out"), mock.patch.object(K, "_err", err.append):
            self.assertEqual(K.main(["install"], kit=kit), 1)
        self.assertIn("interrupted", err[0])

    def test_diff_is_masked(self):
        printed = []
        with mock.patch("builtins.print", lambda *a, **k: printed.append(" ".join(map(str, a)))):
            self.assertEqual(K.main(["install", "--dry-run"], kit=self.kit()), 0)
        text = "\n".join(printed)
        self.assertIn("+++ <HOME>/.claude/CLAUDE.md", text)
        self.assertNotIn(str(Path.home()), text)
