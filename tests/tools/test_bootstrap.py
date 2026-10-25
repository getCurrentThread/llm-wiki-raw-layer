# -*- coding: utf-8 -*-
"""tools/bootstrap.py: the config value rule, the allowed keys and the write format, `next` (the llm.md step an
app's state leads to), checkpoints, masking, and kakaotalk refused by every command. No app is selected here: the
config commands run against a stand-in app (Fake), and doctor's report is built from synthetic probes."""
import contextlib
import io
import json
import os
import re
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from rawlayer import conf
from rawlayer.capture import paths
from rawlayer.registry import Option
from tools import bootstrap as B
from tools.archiveio import boolean, text

SECRET = "s3cr3t-Value-That-Must-Not-Print"


def run(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = B.main([os.fspath(a) for a in argv])
    return rc, out.getvalue(), err.getvalue()


def _source(value):
    if value not in ("api", "desktop"):
        raise ValueError("fk_source")
    return value


def fake_app():
    """A stand-in AppConfig: what the config commands read of one (label, source_keys, options)."""
    return SimpleNamespace(label="fake", source_keys=("fk_archive",),
                           options=(Option("fk_sync", False, boolean), Option("fk_token_file", "", text),
                                    Option("fk_source", "api", _source)),
                           setup_defaults=lambda cfg, note: None)


class Temp(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="llmw-boot-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)


class ValueRule(unittest.TestCase):
    def test_json_or_string(self):
        for raw, want in (("true", True), ("false", False), ("null", None), ("0", 0), ("-5", -5), ("12", 12),
                          ('["a", "b"]', ["a", "b"]), ('{"k": 1}', {"k": 1}), ("007", "007"), ("3.5", "3.5"),
                          ("-0", 0), ("C:\\x\\y", "C:\\x\\y"), ("api", "api"), ("", "")):
            with self.subTest(raw=raw):
                self.assertEqual(B.parse_value(raw), want)

    def test_string_flag_and_bad_json(self):
        self.assertEqual(B.parse_value("true", string=True), "true")
        self.assertEqual(B.parse_value("[x", string=True), "[x")
        with self.assertRaises(B.Refused):
            B.parse_value("[x")


class PathState(Temp):
    def test_states(self):
        f = self.tmp / "f.txt"
        f.write_text("x", encoding="utf-8")
        for v, kind, want in ((None, "any", "unset"), ("", "any", "unset"), (5, "any", "invalid"),
                              ("<abs dir>\\x", "any", "placeholder"), ("rel\\x", "any", "relative"),
                              (str(self.tmp / "none"), "any", "missing"), (str(f), "file", "there"),
                              (str(f), "dir", "wrong_kind"), (str(self.tmp), "file", "wrong_kind"),
                              (str(self.tmp), "dir", "there")):
            with self.subTest(v=v, kind=kind):
                self.assertEqual(B.path_state(v, kind), want)


class ClaudeCodeProbe(Temp):
    def test_local_source_folder_is_probed(self):
        from rawlayer import registry, schedule
        from rawlayer.capture.config import ConfigError

        source = self.tmp / "claude"
        source.mkdir()
        cfg = self.tmp / "config.json"
        cfg.write_text(json.dumps({"cc_claude_dir": str(source)}), encoding="utf-8")
        app = SimpleNamespace(source_keys=(), options=(Option("cc_claude_dir", "", text),))
        with mock.patch.object(registry, "select", return_value=SimpleNamespace(apps=[app])), \
                mock.patch.object(schedule, "task_settings", return_value=None), \
                mock.patch("rawlayer.management.base.default_config", return_value=cfg), \
                mock.patch("rawlayer.capture.config.Config", side_effect=ConfigError("synthetic")):
            self.assertEqual(B.probe("claudecode")["config"]["files"]["cc_claude_dir"], "there")
            source.rmdir()
            self.assertEqual(B.probe("claudecode")["config"]["files"]["cc_claude_dir"], "missing")
            source.write_text("synthetic", encoding="utf-8")
            self.assertEqual(B.probe("claudecode")["config"]["files"]["cc_claude_dir"], "wrong_kind")


class Masking(unittest.TestCase):
    def setUp(self):
        B._MASKS = None
        self.addCleanup(setattr, B, "_MASKS", None)

    def test_never_raises(self):
        with mock.patch("rawlayer.conf.settings", side_effect=RuntimeError("no profile")):
            self.assertEqual(B.mask(str(B.PROJECT)), "<PROJECT>")
            self.assertTrue(B.mask(str(Path.home() / "x")).startswith("<HOME>"))
        B._MASKS = None
        with mock.patch.object(B, "_roots", side_effect=RuntimeError("boom")):
            self.assertEqual(B.mask("C:\\anything"), "(output withheld: it could not be masked)")

    def test_paths_and_user(self):
        home = Path(getattr(conf.settings(), "HOME_DIR", None) or conf.user_home())
        self.assertEqual(B.mask(str(B.PROJECT / "manage.py")), "<PROJECT>" + os.sep + "manage.py")
        self.assertTrue(B.mask(str(Path(paths.DATA_PARENT) / "x")).startswith("<DATA_PARENT>"))
        escaped = json.dumps(str(B.PROJECT / "a"))
        self.assertNotIn(str(B.PROJECT).replace("\\", "\\\\"), B.mask(escaped))
        self.assertIn("<user>", B.mask(f"Z:\\elsewhere\\{home.name}\\f"))
        self.assertEqual(B.mask_obj({"k": [str(B.PROJECT)]}), {"k": ["<PROJECT>"]})
        self.assertEqual(B.mask_obj({"n": 3, "b": None}), {"n": 3, "b": None})


TASK = {"registered": True, "enabled": True, "definition": "current", "result_ok": True, "last_result": "0 (ok)"}


def probe(**over):
    """A doctor probe of a live gmail-like app, with `over` merged into its config (None removes a key)."""
    c = {"exists": True, "readable": True, "placeholders": [], "valid": True, "sync_key": "gm_sync", "sync": True,
         "source": None, "files": {"sources.gm_archive": "there", "gm_client_file": "there", "gm_token_file": "there"},
         "values_set": ["gm_client_file", "gm_token_file"], "task_problems": []}
    top = {k: over.pop(k) for k in ("task", "log", "checkpoints", "error") if k in over}
    c.update(over)
    p = {"config": c, "task": dict(TASK),
         "log": {"steps": [{"at": "t", "step": "capture", "rc": 0}, {"at": "t", "step": "index", "rc": 0}],
                 "complete": True, "newer_started": False}, "checkpoints": {}}
    p.update(top)
    return p


OK_INDEX = {"state": "ok", "total": 10}


class NextStep(unittest.TestCase):
    def test_claudecode_source_and_gates(self):
        self.assertEqual(B.KINDS["cc_claude_dir"], "dir")
        for gate in ("6.6", "6.6.1", "6.6.2", "6.6.3"):
            self.assertIn(gate, B.GATES)

    def step(self, app, p, idx=OK_INDEX):
        return B.next_step(app, p, idx)[0]

    def test_order(self):
        cases = [
            ("no app load", "gmail", {"error": "boom"}, OK_INDEX, "9"),
            ("probe timeout", "gmail", {"error": "x", "timeout": True}, OK_INDEX, "9"),
            ("no config", "gmail", {"config": {"exists": False}}, None, "5.1"),
            ("unreadable", "gmail", {"config": {"exists": True, "readable": False}}, None, "9"),
            ("placeholders", "gmail", probe(placeholders=["sources.gm_archive"]), None, "5.3"),
            ("refused", "gmail", probe(valid=False, refused="x"), None, "5.3"),
            ("client", "gmail", probe(files={"gm_client_file": "missing", "gm_token_file": "missing"}), None, "6.1.2"),
            ("token", "gmail", probe(files={"gm_client_file": "there", "gm_token_file": "unset"}), None, "6.1.5"),
            ("mm_url", "mattermost", probe(values_set=["mm_token_file"],
                                           files={"mm_token_file": "there"}), OK_INDEX, "6.4"),
            ("mm token", "mattermost", probe(values_set=["mm_url", "mm_token_file"],
                                             files={"mm_token_file": "missing"}), OK_INDEX, "6.4"),
            ("cc source missing", "claudecode", probe(files={"cc_claude_dir": "missing"}), None, "6.6"),
            ("cc source wrong kind", "claudecode", probe(files={"cc_claude_dir": "wrong_kind"}), None, "6.6"),
            ("cc source ready", "claudecode", probe(sync_key="cc_sync", files={"cc_claude_dir": "there"}),
             {"state": "missing"}, "5.5"),
            ("dc desktop", "discord", probe(source="desktop", files={"dc_desktop_path": "wrong_kind"}), None, "6.2"),
            ("dc api", "discord", probe(source="api", files={"dc_token_file": "missing"}), None, "6.2"),
            ("nt desktop", "notion", probe(source="desktop", files={"nt_desktop_path": "unset"}), None, "6.3"),
            ("nt api, no login", "notion", probe(source="api", files={}), {"state": "missing"}, "6.3"),
            ("nt api, login", "notion", probe(source="api", files={}, checkpoints={"5.4": {}}), {"state": "missing"},
             "5.5"),
            ("empty index", "gmail", probe(), {"state": "ok", "total": 0}, "6.1.7"),
            ("empty index, another card", "discord", probe(source="desktop", files={"dc_desktop_path": "there"}),
             {"state": "ok", "total": 0}, "5.5"),
            ("outdated", "gmail", probe(), {"state": "outdated"}, "9"),
            ("unreadable", "gmail", probe(), {"state": "unreadable"}, "9"),
            ("search error", "gmail", probe(), {"state": "error", "why": "x"}, "9"),
            ("genesis", "gmail", probe(sync=False), OK_INDEX, "6.1.7"),
            ("sync", "gmail", probe(sync=False, checkpoints={"genesis_complete": {}}), OK_INDEX, "6.1.8"),
            ("sync, another card", "mattermost", probe(sync=False, checkpoints={"genesis_complete": {}},
                                                       values_set=["mm_url", "mm_token_file"],
                                                       files={"mm_token_file": "there"}), OK_INDEX, "5.6"),
            ("nt api archive-only", "notion", probe(source="api", sync=False, files={},
                                                     checkpoints={"genesis_complete": {}},
                                                     task={"registered": False}), OK_INDEX, "5.7"),
            # live before this tool: section 8 records 5.10, which implies genesis (5.5 comes before 5.8)
            ("nt api live, 5.10", "notion", probe(source="api", sync=False, files={}, checkpoints={"5.10": {}},
                                                  task={"registered": "unknown"}), OK_INDEX, "8"),
            ("nt api live, nothing recorded", "notion", probe(source="api", sync=False, files={}), OK_INDEX, "5.5"),
            ("task checks", "gmail", probe(task_problems=["app gmail: x"]), OK_INDEX, "6.1.8"),
            ("task checks, another card", "notion", probe(source="desktop", files={"nt_desktop_path": "there"},
                                                          task_problems=["app notion: x"]), OK_INDEX, "5.6"),
            ("no task", "gmail", probe(task={"registered": False}), OK_INDEX, "5.7"),
            ("unknown, no run", "gmail", probe(task={"registered": "unknown"}, log=None), OK_INDEX, "5.7"),
            # manual collect and --capture runs write task.log too: they prove nothing about the task
            ("unknown, manual runs", "gmail", probe(task={"registered": "unknown"}), OK_INDEX, "5.7"),
            ("unknown, 5.10 recorded", "gmail", probe(task={"registered": "unknown"}, checkpoints={"5.10": {}}),
             OK_INDEX, "8"),
            ("disabled", "gmail", probe(task=dict(TASK, enabled=False)), OK_INDEX, "5.7"),
            ("definition differs", "gmail", probe(task=dict(TASK, definition="differs", differs=["start"])),
             OK_INDEX, "5.7"),
            ("another action", "gmail", probe(task=dict(TASK, definition="foreign")), OK_INDEX, "5.7"),
            ("no run info", "gmail", probe(task={k: v for k, v in TASK.items() if k not in ("result_ok",
                                                                                          "last_result")}),
             OK_INDEX, "9"),
            ("never ran", "gmail", probe(task=dict(TASK, never_ran=True)), OK_INDEX, "5.8"),
            ("running", "gmail", probe(task=dict(TASK, running=True)), OK_INDEX, "5.9"),
            ("bad result", "gmail", probe(task=dict(TASK, result_ok=False, last_result="2")), OK_INDEX, "9"),
            ("bad step", "gmail", probe(log={"steps": [{"at": "t", "step": "capture", "rc": 75}]}), OK_INDEX, "9"),
            ("stopped before index", "gmail", probe(log={"steps": [{"at": "t", "step": "capture", "rc": 0}],
                                                         "complete": False, "newer_started": False}), OK_INDEX, "9"),
            ("a newer run unfinished", "gmail", probe(
                task={"registered": "unknown"}, checkpoints={"5.10": {}},
                log={"steps": [{"at": "t", "step": "capture", "rc": 0}, {"at": "t", "step": "index", "rc": 0}],
                     "complete": True, "newer_started": True}), OK_INDEX, "9"),
            ("unreadable log", "gmail", probe(log={"error": "PermissionError"}), OK_INDEX, "9"),
            ("stale", "gmail", probe(), {"state": "stale", "total": 3}, "8"),
            ("live", "gmail", probe(), OK_INDEX, "8"),
        ]
        for name, app, p, idx, want in cases:
            with self.subTest(name):
                self.assertEqual(self.step(app, p, idx), want)

    def test_every_step_is_in_llm_md(self):
        doc = (B.PROJECT / "llm.md").read_text(encoding="utf-8")
        for g in B.GATES:
            num = ".".join(x for x in g.split(".") if x.isdigit())
            if num:
                with self.subTest(gate=g):
                    if any(s in doc for s in (f"**{num} ", f"**{num}**", f"### {num} ", f"## {num}. ")):
                        continue
                    # 6.<card>.<n>: numbered step <n> of a card whose steps are a numbered list
                    card, _, n = num.rpartition(".")
                    self.assertTrue(card.startswith("6.") and f"### {card} " in doc, g)
                    body = doc[doc.index(f"### {card} "):]
                    body = re.split(r"\n#{2,3} ", body, maxsplit=1)[0]
                    self.assertIn("\nSteps:\n", body, g)
                    self.assertIn(f"\n{n}. ", body[body.index("\nSteps:\n") + 7:], g)
        for by_source in B.NEEDS.values():
            for needs in by_source.values():
                for _k, kind, step, _w in needs:
                    self.assertIn(kind, ("file", "dir", "value", "checkpoint"))
                    self.assertIn(step, doc)

    def test_gates_are_listed_in_llm_md(self):
        # llm.md 5.0 lists every id `checkpoint mark` takes, literally
        doc = (B.PROJECT / "llm.md").read_text(encoding="utf-8")
        section = doc[doc.index("**5.0 "):doc.index("**5.1 ")]
        for g in B.GATES:
            with self.subTest(gate=g):
                self.assertIn(f"`{g}`", section)


class IndexState(unittest.TestCase):
    def test_states(self):
        env = {"total": 4, "updated_at": "u", "index": {"seq": 2, "ledger_seq": 3, "stale": True}}
        for result, want in (((0, json.dumps(env), ""), "stale"), ((0, "{", ""), "error"), ((75, "", ""), "busy"),
                             ((2, "", "search: no gmail index yet -- run collect"), "missing"),
                             ((2, "", "the gmail index was built by another version"), "outdated"),
                             ((2, "", "config file unreadable"), "not_configured"),
                             ((1, "", "something else"), "error")):
            with self.subTest(want=want), mock.patch.object(B, "_run", return_value=result):
                self.assertEqual(B.index_state("gmail")["state"], want)


class ConfigWrite(Temp):
    def test_format_as_setup_writes_it(self):
        cfg = {"v": 1, "data_root": "C:\\d\\x", "sources": {"k": "C:\\a\\b"}, "name": "한글"}
        ref = self.tmp / "ref.json"
        ref.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")   # setup's write
        p = self.tmp / "config.json"
        p.write_bytes(b'{"v": 1}')
        self.assertTrue(B.write_config(p, cfg))
        self.assertEqual(p.read_bytes(), ref.read_bytes())
        self.assertEqual((self.tmp / "config.json.prev").read_bytes(), b'{"v": 1}')
        self.assertFalse((self.tmp / "config.json.part").exists())
        self.assertFalse(B.write_config(p, dict(cfg)))          # unchanged: nothing written, .prev kept
        self.assertEqual((self.tmp / "config.json.prev").read_bytes(), b'{"v": 1}')

    def test_a_value_of_another_json_type_is_a_change(self):
        # 1 == True and 1 == 1.0 in Python: a refused 1 must still be replaceable by true
        p = self.tmp / "config.json"
        for old, new in (({"k": 1}, {"k": True}), ({"k": 1.0}, {"k": 1}), ({"k": 0}, {"k": False})):
            with self.subTest(old=old):
                p.write_text(json.dumps(old), encoding="utf-8")
                self.assertTrue(B.write_config(p, new))
                self.assertEqual(p.read_text(encoding="utf-8"), json.dumps(new, indent=2) + "\n")
        p.write_text('{"b": 1, "a": true}', encoding="utf-8")
        self.assertFalse(B.write_config(p, {"a": True, "b": 1}))      # another key order or layout is no change

    def test_keeps_accepted(self):
        # the real _keeps_accepted, over a stand-in Config that accepts a file holding "ok": true
        from rawlayer.capture import config as capture_config

        class Stub:
            def __init__(self, path):
                if json.loads(Path(path).read_text(encoding="utf-8")).get("ok") is not True:
                    raise capture_config.ConfigError("not ok")
        p = self.tmp / "config.json"
        with mock.patch.object(capture_config, "Config", Stub):
            p.write_text('{"ok": true}', encoding="utf-8")
            before = p.read_bytes()
            with self.assertRaises(B.Refused) as e:
                B.write_config(p, {"ok": False}, B._keeps_accepted(p))
            self.assertIn("config.json is unchanged", str(e.exception))
            self.assertEqual(p.read_bytes(), before)
            self.assertFalse((self.tmp / "config.json.part").exists())
            self.assertFalse((self.tmp / "config.json.prev").exists())
            self.assertTrue(B.write_config(p, {"ok": True, "more": 1}, B._keeps_accepted(p)))
            p.write_text('{"ok": false}', encoding="utf-8")            # not accepted yet: any step is written
            self.assertIsNone(B._keeps_accepted(p))
            self.assertTrue(B.write_config(p, {"ok": False, "more": 1}, B._keeps_accepted(p)))


class ConfigCommands(Temp):
    def setUp(self):
        super().setUp()
        self.app = fake_app()
        self.path = self.tmp / "config.json"
        self.cfg = {"v": 1, "impl": "x", "data_root": str(self.tmp), "sources": {"fk_archive": "<abs dir>"},
                    "fk_sync": False}
        self.path.write_text(json.dumps(self.cfg), encoding="utf-8")
        self.accept = None          # what _keeps_accepted returns: None while the config is not accepted yet
        for target, value in (("_app_config", lambda app: (self.app, self.path,
                                                           json.loads(self.path.read_text(encoding="utf-8")))),
                              ("_validate", lambda a, path, cfg: (0, ["config ok"])),
                              ("_keeps_accepted", lambda path: self.accept)):
            patcher = mock.patch.object(B, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def saved(self):
        return json.loads(self.path.read_text(encoding="utf-8"))

    def test_a_refusing_accept_keeps_the_old_file(self):
        # config set hands write_config what _keeps_accepted returns (ConfigWrite tests the real one)
        def refuse(part):
            self.assertTrue(part.name.endswith(".part"))
            raise B.Refused("the change would make the config refused (x); config.json is unchanged")
        self.accept = refuse
        before = self.path.read_bytes()
        rc, _out, err = run("config", "set", "fake", "fk_sync", "true")
        self.assertEqual(rc, 2)
        self.assertIn("config.json is unchanged", err)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse((self.tmp / "config.json.part").exists())
        self.assertFalse((self.tmp / "config.json.prev").exists())

    def test_set_keys(self):
        (self.tmp / "archive").mkdir()
        rc, out, err = run("config", "set", "fake", "sources.fk_archive", str(self.tmp / "archive"),
                           "fk_sync", "true", "fk_token_file", SECRET)
        self.assertEqual((rc, err), (0, ""))
        got = self.saved()
        self.assertEqual(got["sources"]["fk_archive"], str(self.tmp / "archive"))
        self.assertIs(got["fk_sync"], True)
        self.assertEqual(got["fk_token_file"], SECRET)
        self.assertNotIn(SECRET, out)
        self.assertNotIn(str(self.tmp), out)
        self.assertIn("fk_token_file set (string)", out)

    def test_stdin_object(self):
        with mock.patch("sys.stdin", io.StringIO(json.dumps({"fk_source": "desktop", "fk_sync": True}))):
            rc, _out, _err = run("config", "set", "fake", "-")
        self.assertEqual(rc, 0)
        self.assertEqual((self.saved()["fk_source"], self.saved()["fk_sync"]), ("desktop", True))

    def test_stdin_is_utf8_whatever_the_code_page(self):
        data = json.dumps({"fk_token_file": "개발 채널"}, ensure_ascii=False).encode("utf-8")
        with mock.patch("sys.stdin", io.TextIOWrapper(io.BytesIO(data), encoding="cp949")):
            rc, _out, err = run("config", "set", "fake", "-")
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(self.saved()["fk_token_file"], "개발 채널")
        before = self.path.read_bytes()
        with mock.patch("sys.stdin", io.TextIOWrapper(io.BytesIO("{\"fk_token_file\": \"개발\"}".encode("cp949")),
                                                      encoding="cp949")):
            rc, _out, err = run("config", "set", "fake", "-")
        self.assertEqual(rc, 2)
        self.assertIn("not UTF-8", err)
        self.assertEqual(self.path.read_bytes(), before)

    def test_refusals_write_nothing(self):
        before = self.path.read_bytes()
        (self.tmp / "file.txt").write_text("x", encoding="utf-8")
        for argv in (["fk_sync"], ["nope", "1"], ["sources.other", "C:\\x"], ["data_root", "C:\\x"],
                     ["fk_source", "carrier-pigeon"], ["fk_sync", "yes"], ["sources.fk_archive", "[1]"],
                     ["Bad Key", "1"], ["fk_sync", "[oops"], ["fk_token_file", "<DATA_ROOT>\\token.txt"],
                     ["sources.fk_archive", "relative\\archive"], ["sources.fk_archive", str(self.tmp / "none")],
                     ["sources.fk_archive", str(self.tmp / "file.txt")], ["fk_source", '["<x>"]']):
            with self.subTest(argv=argv):
                rc, out, err = run("config", "set", "fake", *argv)
                self.assertEqual(rc, 2, err)
                self.assertIn("refused", err)
                self.assertEqual(self.path.read_bytes(), before)

    def test_defaults(self):
        def defaults(cfg, note):
            if cfg["sources"]["fk_archive"].startswith("<"):
                cfg["sources"]["fk_archive"] = str(self.tmp / "archive")
                note("sources.fk_archive: set to its default")
        self.app.setup_defaults = defaults
        rc, out, _err = run("config", "defaults", "fake")
        self.assertEqual(rc, 0)
        self.assertIn("config: fake sources.fk_archive: set to its default", out)
        self.assertEqual(self.saved()["sources"]["fk_archive"], str(self.tmp / "archive"))
        self.assertTrue((self.tmp / "config.json.prev").exists())
        rc, out, _err = run("config", "defaults", "fake")
        self.assertEqual(rc, 0)
        self.assertIn("nothing to fill", out)

    def test_core_key_needs_force(self):
        rc, _out, _err = run("config", "set", "fake", "inputs_root", str(self.tmp), "--force")
        self.assertEqual(rc, 0)
        self.assertEqual(self.saved()["inputs_root"], str(self.tmp))

    def test_get_prints_states_only(self):
        self.cfg.update(fk_token_file=SECRET, fk_source="desktop", sources={"fk_archive": str(self.tmp / "a")})
        self.path.write_text(json.dumps(self.cfg), encoding="utf-8")
        rc, out, _err = run("config", "get", "fake", "--json")
        self.assertEqual(rc, 0)
        keys = json.loads(out)["keys"]
        self.assertEqual(keys["fk_token_file"], "set")
        self.assertEqual(keys["fk_source"], "desktop")
        self.assertEqual(keys["sources.fk_archive"], "path in the data root, missing")
        self.assertEqual(keys["data_root"], "the data root, there")
        self.assertEqual(keys["fk_sync"], "false")
        self.assertNotIn(SECRET, out)
        del self.cfg["fk_source"]
        self.cfg.update(fk_token_file=None)
        self.path.write_text(json.dumps(self.cfg), encoding="utf-8")
        keys = json.loads(run("config", "get", "fake", "--json")[1])["keys"]
        self.assertEqual(keys["fk_source"], "missing (the app's default applies)")
        self.assertTrue(keys["fk_token_file"].startswith("null (checked as given"))
        self.assertEqual(keys["inputs_root"], "missing")

    def test_describe_value(self):
        self.assertEqual(B.describe_value(B.MISSING, None, "fk_x", option=True), "missing (the app's default applies)")
        for v, want in ((B.MISSING, "missing"), (None, "null"), ("", "empty"), ("<x>", "placeholder"),
                        (["a", "b"], "list of 2"), (["<p>"], "list of 1 (placeholder)"), ({"a": 1}, "object of 1 key(s)"),
                        (SECRET, "set"), ("https://host.invalid", "set")):
            with self.subTest(v=v):
                self.assertEqual(B.describe_value(v, None, "fk_x"), want)
        self.assertEqual(B.describe_value("api", None, "fk_source"), "api")
        self.assertEqual(B.describe_value("Not-A-Word", None, "fk_source"), "set")


class Checkpoints(Temp):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(B, "_data_root", lambda app: self.tmp)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(B, "_check_app", lambda app: None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_mark_show_clear(self):
        rc, _o, _e = run("checkpoint", "mark", "gmail", "genesis_complete", "--field", "head_seq=4",
                         "--field", "limited=false")
        self.assertEqual(rc, 0)
        doc = json.loads((self.tmp / "reports" / B.CHECKPOINT).read_text(encoding="utf-8"))
        entry = doc["gates"]["genesis_complete"]
        self.assertEqual((entry["head_seq"], entry["limited"]), (4, False))
        self.assertEqual(B.read_checkpoints(self.tmp).keys(), {"genesis_complete"})
        run("checkpoint", "mark", "gmail", "5.7")
        rc, out, _e = run("checkpoint", "show", "gmail")
        self.assertEqual(rc, 0)
        self.assertLess(out.index("genesis_complete"), out.index(" 5.7 "))
        self.assertEqual(run("checkpoint", "clear", "gmail", "5.7")[0], 0)
        self.assertEqual(set(B.read_checkpoints(self.tmp)), {"genesis_complete"})
        self.assertEqual(run("checkpoint", "clear", "gmail", "5.7")[0], 2)
        self.assertEqual(run("checkpoint", "clear", "gmail")[0], 0)
        self.assertEqual(B.read_checkpoints(self.tmp), {})

    def test_vocabulary(self):
        for argv in (["mark", "gmail", "whatever"], ["mark", "gmail"], ["mark", "gmail", "5.5", "--field", "x=1"],
                     ["mark", "gmail", "5.5", "--field", "rc=abc"], ["mark", "gmail", "5.5", "--field", "rc"]):
            with self.subTest(argv=argv):
                self.assertEqual(run("checkpoint", *argv)[0], 2)
        self.assertFalse((self.tmp / "reports" / B.CHECKPOINT).exists())

    def test_unreadable_file_reads_as_none(self):
        (self.tmp / "reports").mkdir()
        (self.tmp / "reports" / B.CHECKPOINT).write_text("{", encoding="utf-8")
        self.assertEqual(B.read_checkpoints(self.tmp), {})


class OffLimits(unittest.TestCase):
    def test_every_command_refuses_kakaotalk(self):
        for argv in (["doctor", "--app", "kakaotalk"], ["config", "get", "kakaotalk"],
                     ["config", "set", "kakaotalk", "x", "1"], ["config", "defaults", "kakaotalk"],
                     ["checkpoint", "show", "kakaotalk"], ["checkpoint", "mark", "kakaotalk", "5.5"],
                     ["_probe", "kakaotalk"]):
            with self.subTest(argv=argv), mock.patch.object(B, "in_scope_apps",
                                                            return_value=(["gmail"], ["kakaotalk"])):
                rc, out, err = run(*argv)
                self.assertEqual(rc, 2)
                self.assertIn("off-limits", err)
                self.assertEqual(out, "")

    def test_unknown_app(self):
        with mock.patch.object(B, "in_scope_apps", return_value=(["gmail"], [])):
            rc, _out, err = run("config", "get", "nosuchapp")
        self.assertEqual(rc, 2)
        self.assertIn("no app", err)

    def test_discovery_leaves_it_out(self):
        apps, off = B.in_scope_apps()
        self.assertNotIn("kakaotalk", apps)
        self.assertTrue(set(off) <= set(B.OFF_LIMITS))


class DoctorReport(unittest.TestCase):
    def report(self):
        m = {"python": {"version": "3.12.1", "executable": "X", "flags_ok": True, "problem": None},
             "git": {"found": True, "version": "2.47.0", "machine_path": None},
             "pwsh": {"found": True, "version": "7.4.0", "ok": True},
             "checkout": {"under_profile": True, "work_tree": True, "changed_files": 0, "core": "ok"},
             "check": {"rc": 0, "lines": ["check: ok errors=0 refused=0"]},
             "data_parent": {"exists": True, "acl": "ok", "search": "ok", "in_work_tree": False}, "elevated": False}
        p = probe(files={"gm_client_file": "missing", "gm_token_file": "there"})
        p.update(index=OK_INDEX, next={"step": "6.1.3", "why": "w"},
                 task={"name": "\\t", "registered": "unknown"},
                 verify={"rc": 0, "busy": True, "differing_rows": None, "head_seq": None})
        return {"machine": m, "apps": {"gmail": p, "notion": {"error": "x", "next": {"step": "9", "why": "x"}},
                                       "discord": {"config": {"exists": False}, "next": {"step": "5.1", "why": "s"}}},
                "skipped": ["kakaotalk (off-limits)"], "attention": ["gmail 6.1.3: w"]}

    def test_text_report(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            B._print_doctor(self.report())
        text = out.getvalue()
        for want in ("doctor: gmail  next 6.1.3", "paths not there: gm_client_file (missing)", "registration unknown",
                     "ledger busy: not verified", "doctor: kakaotalk (off-limits): not checked",
                     "1 item(s) need attention"):
            self.assertIn(want, text)

    def test_an_app_that_cannot_be_probed_needs_attention(self):
        # with no config read, it counts anyway: doctor must not say ok
        machine = ({"python": {}, "git": {}, "pwsh": {}, "checkout": {}, "check": {}, "data_parent": {},
                    "elevated": False}, [])
        for probed in ({"app": "discord", "error": "the probe did not finish", "timeout": True},
                       {"app": "discord", "error": "unreadable probe output"}):
            with self.subTest(probed=probed), \
                    mock.patch.object(B, "in_scope_apps", return_value=(["discord"], [])), \
                    mock.patch.object(B, "machine", return_value=machine), \
                    mock.patch.object(B, "_probe_child", return_value=dict(probed)), \
                    mock.patch.object(B, "index_state", return_value=OK_INDEX):
                rc, out, _err = run("doctor", "--json")
            self.assertEqual(rc, 3)
            self.assertTrue(any(x.startswith("discord 9:") for x in json.loads(out)["attention"]))
        with mock.patch.object(B, "_run", return_value=(None, "", "")):
            self.assertTrue(B._probe_child("discord")["timeout"])
