# -*- coding: utf-8 -*-
"""manage.py schedule (rawlayer/schedule.py): an app's AppConfig.schedule, the Task Scheduler definition, and install /
status / run / remove against a stand-in for schtasks.exe (FakeTasks). Nothing here touches the machine's task store:
the stand-in keeps the registered definitions in memory and reads them back the way Task Scheduler does (its own
element order, values equal to the schema defaults dropped). The toy app plays a project's app "toy" (Case)."""
import contextlib
import datetime
import io
import os
import re
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest import mock

from rawlayer import management, registry, schedule
from rawlayer.capture import precheck
from rawlayer.capture.config import ConfigError
from tests.core import toyfix as T
from tests.core.toyapp import apps as toyapps

GOOD = {"task": "\\toy-folder\\toy-capture", "start": "01:14", "every_hours": 3, "limit_hours": 2,
        "description": "toy: collect every 3 h"}
NO_RUN = "267011"                       # 0x41303: the task has not run yet
TOYS = {"toy": T.TOY, "toy2": T.TOY2}    # the apps a Case project discovers, by package
# the synthetic paths below (never created) are built on the temp folder: no drive, no user folder assumed
TMP = Path(tempfile.gettempdir())
# the temp folder's drive (or "/"): no space in it, so a path on it is one list2cmdline leaves unquoted
ROOT = Path(TMP.anchor)
WINDOWS = os.name == "nt"               # drive-letter case and Windows' argv parser


def run(*argv, entry=schedule.main):
    """entry(argv) -> (exit code, stdout, stderr); a path in argv is passed as its text."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = entry([os.fspath(a) for a in argv])
    return rc, out.getvalue(), err.getvalue()


def respelled(path):
    """`path` (text) as a prompt that keeps a lower-case drive letter, say, spells it: the case of every letter
    changed, the drive letter's flipped. Windows takes it for the same folder."""
    return path[0].swapcase() + path[1:].upper()


def config_of(found):
    """registered_config()'s answer as text (None stays None): a path of either type compares by its text."""
    return None if found is None else os.fspath(found)


def read_back(xml_text):
    """What `schtasks /Query /XML` prints for a definition registered from `xml_text`: Task Scheduler's element order
    (Repetition before StartBoundary), elements equal to the schema default dropped, a few of its own added."""
    t = xml_text.replace("\r\n", "\n")
    rep = re.search(r"\n *<Repetition>.*?</Repetition>", t, re.S)
    if rep:
        t = t.replace(rep.group(0), "")
        t = t.replace("<CalendarTrigger>", "<CalendarTrigger>" + rep.group(0), 1)
    t = re.sub(r"\n *<Enabled>true</Enabled>", "", t)
    t = re.sub(r"\n *<MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>", "", t)
    t = re.sub(r"\n *<StopAtDurationEnd>false</StopAtDurationEnd>", "", t)
    t = t.replace("</Description>", "</Description>\n    <URI>\\x</URI>", 1)
    t = t.replace("</Settings>", "  <UseUnifiedSchedulingEngine>true</UseUnifiedSchedulingEngine>\n  </Settings>", 1)
    return t.replace("\n", "\r\r\n")


class FakeTasks:
    """schtasks.exe for one task store: /Create /XML, /Query [/XML | /FO CSV /V /NH], /Run, /Delete. `xml` is the
    definition of GOOD's task (the toy app's), `store` every registered one by name."""

    def __init__(self):
        self.calls, self.files, self.store = [], [], {}
        self.last_result = NO_RUN
        self.fail = {}                  # verb -> (rc, out, err)
        self.edit = None                # applied to the definition as it is registered

    @property
    def xml(self):
        return self.store.get(GOOD["task"].lower())

    @xml.setter
    def xml(self, value):
        if value is None:
            self.store.pop(GOOD["task"].lower(), None)
        else:
            self.store[GOOD["task"].lower()] = value

    def verbs(self):
        return [c[0] for c in self.calls]

    def __call__(self, *args):
        self.calls.append(args)
        verb = args[0]
        if verb in self.fail:
            return self.fail[verb]
        name = args[args.index("/TN") + 1].lower()
        if verb == "/Create":
            path = Path(args[args.index("/XML") + 1])
            data = path.read_bytes()
            self.files.append((path, data))
            self.store[name] = read_back(data.decode("utf-16"))
            if self.edit:
                self.store[name] = self.edit(self.store[name])
            return 0, "SUCCESS: created.\r\n", ""
        if name not in self.store and verb in ("/Query", "/Run", "/Delete"):
            return 1, "", "ERROR: The system cannot find the file specified.\r\r\n"
        if verb == "/Query" and "/XML" in args:
            return 0, self.store[name], ""
        if verb == "/Query":
            # the command line field keeps its quotes unescaped, as schtasks prints it
            return 0, (f'"HOST","{args[2]}","N/A","Ready","Interactive/Background","2026-10-05 \uc624\ud6c4 11:14:00",'
                       f'"{self.last_result}","N/A","{TMP / "py.exe"} -s -B "{TMP / "a b" / "manage.py"}" collect toy",'
                       f'"{TMP / "a b"}","d","Enabled","Disabled"\r\n'), ""
        if verb == "/Run":
            return 0, "SUCCESS: run.\r\n", ""
        if verb == "/Delete":
            del self.store[name]
            return 0, "SUCCESS: deleted.\r\n", ""
        raise AssertionError(f"unexpected schtasks call {args}")


class TestSettings(unittest.TestCase):
    def test_good_and_absent(self):
        self.assertEqual(schedule.parse(GOOD), schedule.Schedule(GOOD["task"], "01:14", 3, 2, GOOD["description"]))
        self.assertIsNone(schedule.parse(None))
        self.assertEqual(schedule.parse({k: v for k, v in GOOD.items() if k != "description"}).description, "")
        self.assertEqual(schedule.parse({**GOOD, "every_hours": 24}).every_hours, 24)

    def test_every_malformed_value_is_refused(self):
        bad = [("task", "name"), ("task", "\\"), ("task", "\\a\\"), ("task", '\\a"b'), ("task", "\\a/b"), ("task", 3),
               ("start", "1:14"), ("start", "24:00"), ("start", "01:60"), ("start", 114),
               ("every_hours", 0), ("every_hours", 25), ("every_hours", True), ("every_hours", "3"),
               ("every_hours", 3.0), ("limit_hours", 0), ("limit_hours", 73), ("limit_hours", False),
               ("description", 3), ("description", "a\nb"), ("description", "x" * 501)]
        for key, value in bad:
            with self.subTest(key=key, value=value), self.assertRaises(schedule.ScheduleError):
                schedule.parse({**GOOD, key: value})
        for value in ([], "x", {**GOOD, "other": 1}, {k: v for k, v in GOOD.items() if k != "start"}):
            with self.subTest(value=value), self.assertRaises(schedule.ScheduleError):
                schedule.parse(value)

    def test_describe(self):
        self.assertEqual(schedule.describe(schedule.parse(GOOD)), "every 3 h from 01:14 (local time), at most 2 h per run")
        self.assertTrue(schedule.describe(schedule.parse({**GOOD, "every_hours": 24})).startswith("daily at 01:14"))


class TestDefinition(unittest.TestCase):
    PY, PROJECT, CONFIG = str(TMP / "Py 3" / "python.exe"), str(TMP / "work dir" / "proj"), \
        str(TMP / "data root" / "config.json")
    # registered from the earlier layout of one project per folder (<folder>/manage.py collect --config <cfg>)
    OLD = f'-s -B "{Path(PROJECT, "toy", "manage.py")}" collect --config "{CONFIG}"'

    def xml(self, **kv):
        s = schedule.parse({**GOOD, **kv})
        return schedule.task_xml(s, self.PY, self.PROJECT, "toy", self.CONFIG, today=datetime.date(2026, 10, 5))

    def test_the_task_runs_collect_as_system(self):
        root = ET.fromstring(re.sub(r"^<\?xml[^>]*\?>", "", self.xml(description="a & <b>")))
        ns = {"t": schedule.NS}
        get = lambda p: root.findtext(p, namespaces=ns)  # noqa: E731
        self.assertEqual(get("t:Actions/t:Exec/t:Command"), self.PY)
        self.assertEqual(get("t:Actions/t:Exec/t:Arguments"),
                         f'-s -B "{Path(self.PROJECT, "manage.py")}" collect toy --config "{self.CONFIG}"')
        self.assertEqual(get("t:Actions/t:Exec/t:WorkingDirectory"), self.PROJECT)
        self.assertEqual(get("t:Principals/t:Principal/t:UserId"), "S-1-5-18")
        self.assertEqual(get("t:Principals/t:Principal/t:RunLevel"), "HighestAvailable")
        self.assertEqual(get("t:Triggers/t:CalendarTrigger/t:StartBoundary"), "2026-10-05T01:14:00")
        self.assertEqual(get("t:Triggers/t:CalendarTrigger/t:Repetition/t:Interval"), "PT3H")
        self.assertEqual(get("t:Triggers/t:CalendarTrigger/t:Repetition/t:Duration"), "P1D")
        self.assertEqual(get("t:Triggers/t:CalendarTrigger/t:ScheduleByDay/t:DaysInterval"), "1")
        self.assertEqual(get("t:Settings/t:ExecutionTimeLimit"), "PT2H")
        self.assertEqual(get("t:Settings/t:MultipleInstancesPolicy"), "IgnoreNew")
        self.assertEqual(get("t:Settings/t:StartWhenAvailable"), "true")
        self.assertEqual(get("t:Settings/t:DisallowStartIfOnBatteries"), "false")
        self.assertEqual(get("t:Settings/t:StopIfGoingOnBatteries"), "false")
        self.assertEqual(get("t:RegistrationInfo/t:Description"), "a & <b>")

    def test_once_a_day_has_no_repetition(self):
        f = schedule.fields(self.xml(every_hours=24))
        self.assertEqual((f["interval"], f["duration"], f["triggers"]), (None, None, ("CalendarTrigger",)))

    def test_read_back_matches_through_the_defaults(self):
        xml = self.xml()
        self.assertEqual(schedule.differences(read_back(xml), xml), [])
        other_day = read_back(xml).replace("2026-10-05T01:14:00", "2027-01-02T01:14:00+09:00")
        self.assertEqual(schedule.differences(other_day, xml), [])
        as_name = read_back(xml).replace("S-1-5-18", "SYSTEM")
        self.assertEqual(schedule.differences(as_name, xml), [])

    def test_every_difference_is_named(self):
        xml, back = self.xml(), read_back(self.xml())
        cases = {
            "interval": back.replace("PT3H", "PT4H"),
            "start": back.replace("T01:14:00", "T01:15:00"),
            "time_limit": back.replace("PT2H", "PT3H"),
            "arguments": back.replace(" collect toy --config", " capture toy --config"),
            "command": back.replace("Py 3", "Py 4"),
            "enabled": back.replace("</Settings>", "<Enabled>false</Enabled></Settings>"),
            "user": back.replace("S-1-5-18", "S-1-5-21-1"),
            "start_when_available": back.replace("<StartWhenAvailable>true", "<StartWhenAvailable>false"),
            "triggers": back.replace("</Triggers>", "<BootTrigger/></Triggers>"),
            "description": back.replace("toy: collect", "other"),
        }
        for name, changed in cases.items():
            with self.subTest(name=name):
                self.assertEqual(schedule.differences(changed, xml), [name])

    def test_registered_config(self):
        args = schedule.arguments(self.PROJECT, "toy", self.CONFIG)
        self.assertEqual(config_of(schedule.registered_config(self.PROJECT, "toy", args)), self.CONFIG)
        p, d = str(ROOT / "p"), str(ROOT / "d" / "config.json")
        plain = schedule.arguments(p, "toy", d)
        self.assertNotIn('"', plain)                            # the unquoted form, not `quoted` below again
        self.assertEqual(config_of(schedule.registered_config(p, "toy", plain)), d)
        # every path quoted (the retired script's spelling)
        quoted = f'-s -B "{Path(p, "manage.py")}" collect toy --config "{d}"'
        self.assertEqual(config_of(schedule.registered_config(p, "toy", quoted)), d)
        for foreign in ("", "-s -B other.py", schedule.arguments(str(TMP / "elsewhere"), "toy", self.CONFIG), None,
                        args.replace("-s -B", "-S -B"), args.replace(" collect ", " capture "), args + " --x",
                        schedule.arguments(self.PROJECT, "toy2", self.CONFIG), self.OLD):
            with self.subTest(args=foreign):
                self.assertIsNone(schedule.registered_config(self.PROJECT, "toy", foreign))

    @unittest.skipUnless(WINDOWS, "drive-letter case and Windows' argv parser")
    def test_registered_config_as_windows_reads_it(self):
        """The same folder spelled another way (a prompt that keeps a lower-case drive letter), and white space
        Windows' argv parser does not split on, left unquoted by list2cmdline."""
        args = schedule.arguments(self.PROJECT, "toy", self.CONFIG)
        self.assertEqual(config_of(schedule.registered_config(respelled(self.PROJECT), "toy", args)), self.CONFIG)
        for ws in ("\u3000", "\u00a0", "\u2003"):
            with self.subTest(ws=hex(ord(ws))):
                project, config = str(ROOT / f"a{ws}b"), str(ROOT / f"d{ws}e" / "config.json")
                registered = schedule.arguments(project, "toy", config)
                self.assertNotIn('"', registered)               # left unquoted, as the case says
                self.assertEqual(config_of(schedule.registered_config(project, "toy", registered)), config)

    def test_the_earlier_layout_is_recognised(self):
        """One project per folder (`<folder>/manage.py collect --config <cfg>`, 6 tokens): install replaces it."""
        self.assertTrue(schedule.old_layout(self.OLD))
        for other in (schedule.arguments(self.PROJECT, "toy", self.CONFIG), "", None, "-s -B x.py collect --config c",
                      self.OLD.replace(" collect ", " capture ")):
            with self.subTest(args=other):
                self.assertFalse(schedule.old_layout(other))

    @unittest.skipUnless(WINDOWS, "drive-letter case")
    def test_paths_compare_as_windows_does(self):
        xml = self.xml()
        py_dir = str(Path(self.PY).parent)
        lower = xml.replace(self.PROJECT, respelled(self.PROJECT)).replace(py_dir, py_dir.lower())
        self.assertNotEqual(lower, xml)
        self.assertEqual(schedule.differences(read_back(lower), xml), [])
        flags = xml.replace("-s -B", "-S -B")                    # a flag's case is not a path's
        self.assertEqual(schedule.differences(read_back(flags), xml), ["arguments"])

    def test_result_text(self):
        self.assertEqual(schedule.result_text("0"), ("0 (ok)", True))
        self.assertEqual(schedule.result_text(NO_RUN), ("0x00041303 (has not run yet)", True))
        self.assertEqual(schedule.result_text("267009"), ("0x00041301 (running now)", True))
        self.assertEqual(schedule.result_text("75"), ("75 (capture busy, or an origin skipped)", False))
        self.assertEqual(schedule.result_text("-2147024894")[0][:10], "0x80070002")
        self.assertFalse(schedule.result_text("-2147024894")[1])
        self.assertEqual(schedule.result_text("n/a"), ("n/a", False))

    def test_output_decoding(self):
        text = "\uc624\ud6c4 ok"
        self.assertEqual(schedule._text(text.encode("utf-8")), text)
        self.assertEqual(schedule._text(text.encode("utf-16")), text)
        self.assertIsInstance(schedule._text(b"\xb0\xa1\xff"), str)

    def test_python_problem(self):
        with tempfile.TemporaryDirectory(prefix="llmw-py-") as tmp:
            d = Path(tmp)
            (d / "python.exe").write_bytes(b"")
            self.assertIsNone(schedule.python_problem(d / "python.exe"))
            self.assertIn("not an existing file", schedule.python_problem(d / "none.exe"))
            self.assertIn("not an existing file", schedule.python_problem("python.exe"))

    @unittest.skipUnless(WINDOWS, "a Microsoft Store alias")
    def test_python_problem_store_alias(self):
        with tempfile.TemporaryDirectory(prefix="llmw-py-") as tmp:
            store = Path(tmp) / "WindowsApps"
            store.mkdir()
            (store / "python.exe").write_bytes(b"")
            self.assertIn("Store alias", schedule.python_problem(store / "python.exe"))


class TestTaskLog(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="llmw-sched-", dir=T.TEST_PARENT))
        self.addCleanup(T.force_rmtree, self.root)
        (self.root / "reports").mkdir()

    def write(self, text, name="task.log", mode="w"):
        with (self.root / "reports" / name).open(mode, encoding="utf-8", newline="\n") as fh:
            fh.write(text)

    def lines(self, info):
        return [ln for ln, _step, _rc in info["steps"]]

    RUN1 = ("run\n2026-10-05T01:00:00Z step=capture rc=0 seconds=9\n  capture done run=1\n"
            "2026-10-05T01:00:09Z step=index rc=0 seconds=1\n  stderr: x step=capture rc=0 seconds=1\n")
    RUN2 = ("run\n2026-10-05T04:00:00Z step=capture rc=75 seconds=8\n  capture done run=2\n"
            "2026-10-05T04:00:08Z step=index rc=0 seconds=2\n  index one=1\n")

    def test_newest_run_and_only_its_step_lines(self):
        self.assertIsNone(schedule.last_run(self.root))
        self.write(self.RUN1 + self.RUN2)
        info = schedule.last_run(self.root)
        self.assertEqual(self.lines(info), ["2026-10-05T04:00:00Z step=capture rc=75 seconds=8",
                                            "2026-10-05T04:00:08Z step=index rc=0 seconds=2"])
        self.assertEqual([rc for _ln, _s, rc in info["steps"]], [75, 0])
        self.assertEqual((info["complete"], info["newer_started"]), (True, False))
        self.write("run\n", mode="a")
        info = schedule.last_run(self.root)
        self.assertEqual((len(info["steps"]), info["complete"], info["newer_started"]), (2, True, True))
        self.write("")
        self.assertEqual(schedule.last_run(self.root), {"steps": [], "complete": False, "newer_started": False})

    def test_a_run_without_its_last_step_is_incomplete(self):
        self.write(self.RUN1 + "run\n2026-10-05T04:00:00Z step=capture rc=0 seconds=8\n  capture done run=2\n")
        info = schedule.last_run(self.root)
        self.assertEqual(self.lines(info), ["2026-10-05T04:00:00Z step=capture rc=0 seconds=8"])
        self.assertEqual((info["complete"], info["newer_started"]), (False, False))

    def test_right_after_a_rollover_the_old_log_is_read(self):
        self.write(self.RUN1, name="task.log.1")
        self.write("run\n")
        info = schedule.last_run(self.root)
        self.assertEqual(self.lines(info)[0], "2026-10-05T01:00:00Z step=capture rc=0 seconds=9")
        self.assertEqual((info["complete"], info["newer_started"]), (True, True))
        (self.root / "reports" / "task.log").unlink()
        self.assertTrue(schedule.last_run(self.root)["complete"])

    def test_an_unreadable_log_is_not_none(self):
        self.write(self.RUN1)
        with mock.patch.object(schedule, "_runs", _raise(PermissionError())):
            self.assertEqual(schedule.last_run(self.root), {"error": "PermissionError"})


class Case(T.ToyCase):
    """The toy app installed as the project's only app "toy" (discovered, selected, loaded on its own: the toy
    registry), a toy config fit for the task, and the machine replaced: the toy app's schedule = GOOD, an elevated
    Windows prompt, a data root precheck that passes, schtasks.exe = self.tasks, a core that matches its sums."""
    STANDARD = False

    def setUp(self):
        super().setUp()
        self.fx.set_cfg(toy_system=True)
        self.tasks = FakeTasks()
        self.elevated = True
        for target, kw in ((schedule, {"_schedule_value": lambda app: GOOD, "_windows": lambda: True,
                                       "elevated": lambda: self.elevated, "schtasks": self.tasks}),
                           (registry, {"discover": lambda: ["toy"], "select": lambda app: registry.get(),
                                       "app_registry": lambda app: registry.Registry([TOYS[app]])}),
                           (precheck, {"check": lambda data_root: None})):
            for name, value in kw.items():
                p = mock.patch.object(target, name, value)
                p.start()
                self.addCleanup(p.stop)

    def install(self, *extra):
        return run("install", "toy", "--config", self.fx.config_path, *extra)


class TestInstall(Case):
    def test_registers_reads_back_and_starts(self):
        rc, out, err = self.install("--run-now")
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(self.tasks.verbs(), ["/Create", "/Query", "/Run"])
        create = self.tasks.calls[0]
        self.assertEqual((create[1:3], create[-1]), (("/TN", GOOD["task"]), "/F"))
        path, data = self.tasks.files[0]
        self.assertEqual(data[:2], b"\xff\xfe")                  # UTF-16 with a BOM, as the declaration says
        self.assertFalse(path.exists())                          # the definition file is removed
        root = ET.fromstring(re.sub(r"^<\?xml[^>]*\?>", "", data.decode("utf-16")))
        get = lambda tag: root.findtext(f"{{{schedule.NS}}}Actions/{{{schedule.NS}}}Exec/{{{schedule.NS}}}{tag}")  # noqa: E731
        project = str(T.REPO.resolve())                          # by real path, as SYSTEM will see it
        # abspath, not Path.absolute(): schedule normalises '..' in the interpreter's path without resolving links
        self.assertEqual(get("Command"), os.path.abspath(sys.executable))
        self.assertEqual(get("Arguments"), schedule.arguments(project, "toy", self.fx.config_path.resolve()))
        self.assertEqual(get("WorkingDirectory"), project)
        self.assertEqual(self.tasks.calls[2], ("/Run", "/TN", GOOD["task"]))
        self.assertIn("as registered", out)

    def test_without_run_now_nothing_starts(self):
        self.assertEqual(self.install()[0], 0)
        self.assertEqual(self.tasks.verbs(), ["/Create", "/Query"])

    def test_dry_run_prints_the_definition_and_needs_no_elevation(self):
        self.elevated = False
        rc, out, _err = self.install("--dry-run")
        self.assertEqual((rc, self.tasks.calls), (0, []))
        s = schedule.parse(GOOD)
        expected = schedule.task_xml(s, os.path.abspath(sys.executable), T.REPO.resolve(), "toy",
                                     self.fx.config_path.resolve())
        self.assertEqual(schedule.differences(out, expected), [])
        self.assertEqual(SystemExit, type(_raises(lambda: run("install", "toy", "--config", "x", "--dry-run",
                                                              "--run-now"))))

    def test_without_an_app_every_scheduled_app_installs_in_a_process_of_its_own(self):
        children = []

        def child(argv, **kw):
            children.append((argv, kw))
            return mock.Mock(returncode=0)
        with mock.patch.object(schedule.subprocess, "run", child):
            rc, out, _err = run("install", "--dry-run")
        self.assertEqual(rc, 0)
        self.assertIn("schedule: == toy", out)
        self.assertEqual([a[4:] for a, _kw in children], [["schedule", "install", "toy", "--dry-run"]])
        self.assertEqual([os.fspath(a) for a in children[0][0][:4]],         # the child's argv, as it receives it
                         [sys.executable, "-s", "-B", str(T.REPO / "manage.py")])
        with mock.patch.object(schedule, "_schedule_value", lambda app: None):
            rc, _out, err = run("install")
        self.assertEqual(rc, 2)
        self.assertIn("no app has a schedule", err)
        rc, _out, err = run("install", "--config", self.fx.config_path)
        self.assertEqual(rc, 2)
        self.assertIn("--config belongs to one app", err)
        self.assertEqual(run("install", "nope")[0], 2)
        self.assertEqual(self.tasks.calls, [])

    def test_refusals_register_nothing(self):
        with tempfile.TemporaryDirectory(prefix="llmw-py-") as tmp:
            store = Path(tmp) / "WindowsApps"
            store.mkdir()
            (store / "python.exe").write_bytes(b"")
            cases = {
                "not elevated": (lambda: setattr(self, "elevated", False), (), "elevated prompt"),
                "app check": (lambda: self.fx.set_cfg(toy_system=False), (), "app toy: toy_system must be true"),
                "config": (lambda: self.fx.config_path.unlink(), (), "config refused (config unreadable)"),
                "precheck": (lambda: self.patch(precheck, "check", _raise(ConfigError("data root ACL entry"))), (),
                             "config refused (data root ACL entry)"),
                "python missing": (None, ("--python", Path(tmp) / "none.exe"), "not an existing file"),
                "store alias": (None, ("--python", store / "python.exe"), "Store alias"),
                "no schedule": (lambda: self.patch(schedule, "_schedule_value", lambda app: None), (),
                                "toy has no schedule"),
                "malformed": (lambda: self.patch(schedule, "_schedule_value", lambda app: {"task": 1}), (),
                              "toy: schedule"),
                "not windows": (lambda: self.patch(schedule, "_windows", lambda: False), (), "Windows"),
                "hook result": (lambda: self.patch(toyapps.ToyConfig, "task_checks", lambda s, c: "x"), (),
                                "must return a list of strings"),
                "hook raising": (lambda: self.patch(toyapps.ToyConfig, "task_checks", _raise(ValueError())), (),
                                 "app toy: task_checks failed (error ValueError)"),
                "precheck failing": (lambda: self.patch(precheck, "check", _raise(RuntimeError("rc=1"))), (),
                                     "could not be checked (error RuntimeError)"),
            }
            if not WINDOWS:
                del cases["store alias"]                # a Microsoft Store alias is a Windows path
            for name, (arrange, extra, message) in cases.items():
                with self.subTest(case=name), contextlib.ExitStack() as stack:
                    self.stack = stack
                    self.fx.write_config(toy_system=True)
                    self.elevated = True
                    if arrange:
                        arrange()
                    rc, _out, err = self.install(*extra)
                    self.assertEqual(rc, 2)
                    self.assertIn(message, err)
                    self.assertNotIn("/Create", self.tasks.verbs())

    def patch(self, target, name, value):
        self.stack.enter_context(mock.patch.object(target, name, value))

    def test_schtasks_failure_is_exit_1_and_leaves_no_file(self):
        self.tasks.fail["/Create"] = (1, "", "ERROR: Access is denied.\r\n")
        rc, _out, err = self.install("--run-now")
        self.assertEqual(rc, 1)
        self.assertIn("schtasks rc=1: ERROR: Access is denied.", err)
        self.assertEqual(self.tasks.verbs(), ["/Create"])
        self.assertFalse(Path(self.tasks.calls[0][self.tasks.calls[0].index("/XML") + 1]).exists())

    def test_a_definition_read_back_otherwise_is_exit_3(self):
        self.tasks.edit = lambda x: x.replace("PT3H", "PT4H")
        rc, _out, err = self.install("--run-now")
        self.assertEqual(rc, 3)
        self.assertIn("differs: interval", err)
        self.assertNotIn("/Run", self.tasks.verbs())
        self.tasks.edit, self.tasks.fail["/Query"] = None, (1, "", "ERROR: Access is denied.\r\n")
        self.assertEqual(self.install()[0], 3)

    def test_a_start_that_fails_is_exit_1(self):
        self.tasks.fail["/Run"] = (1, "", "ERROR: x\r\n")
        self.assertEqual(self.install("--run-now")[0], 1)


class TestStatus(Case):
    def status(self, *extra):
        return run("status", "toy", *extra)

    def test_not_registered(self):
        rc, out, _err = self.status()
        self.assertEqual(rc, 3)
        self.assertIn("is not registered, or not readable", out)
        self.assertIn("manage.py schedule install toy", out)

    def test_without_an_app_every_scheduled_app(self):
        self.install()
        self.tasks.last_result = "0"
        rc, out, _err = run("status")
        self.assertEqual(rc, 0)
        self.assertIn("definition: current", out)
        with mock.patch.object(schedule, "_schedule_value", lambda app: {"task": 1}):
            rc, _out, err = run("status")
        self.assertEqual(rc, 2)
        self.assertIn("toy: schedule", err)

    def test_a_task_of_the_earlier_layout(self):
        """Registered from one project per folder: status says so (install replaces it)."""
        self.install()
        self.tasks.last_result = "0"
        old = f'-s -B "{TMP / "p" / "toy" / "manage.py"}" collect --config "{TMP / "d" / "config.json"}"'
        self.tasks.xml = re.sub(r"<Arguments>.*?</Arguments>", lambda m: f"<Arguments>{old}</Arguments>",
                                self.tasks.xml)
        rc, out, _err = self.status()
        self.assertEqual(rc, 3)
        self.assertIn("registered from the earlier layout", out)
        self.assertIn("manage.py schedule install toy", out)

    def test_registered_and_current(self):
        self.install()
        for result, rc, shown in (("0", 0, "0 (ok)"), (NO_RUN, 0, "0x00041303 (has not run yet)"),
                                  ("267009", 0, "0x00041301 (running now)"), ("75", 3, "75 (capture busy"),
                                  ("-2147024894", 3, "0x80070002")):
            with self.subTest(result=result):
                self.tasks.last_result = result
                got, out, _err = self.status()
                self.assertEqual(got, rc)
                self.assertIn("definition: current", out)
                self.assertIn(f"last result: {shown}", out)
                self.assertIn("state: Ready", out)

    def test_differences_against_settings_and_options(self):
        self.install()
        self.tasks.last_result = "0"
        rc, out, _err = self.status("--config", self.fx.base / "other.json")
        self.assertEqual(rc, 3)
        self.assertIn("differs from toy's schedule and the options given: arguments", out)
        self.assertIn("differs from toy's schedule and the options given: command",
                      self.status("--python", self.fx.base / "py.exe")[1])
        with mock.patch.object(schedule, "_schedule_value", lambda app: {**GOOD, "limit_hours": 1}):
            self.assertIn("differs from toy's schedule: time_limit", self.status()[1])
        self.tasks.xml = self.tasks.xml.replace("</Settings>", "<Enabled>false</Enabled></Settings>")
        rc, out, _err = self.status()
        self.assertEqual(rc, 3)
        self.assertIn("(DISABLED)", out)

    def test_a_foreign_action(self):
        self.install()
        self.tasks.xml = re.sub(r"<Arguments>.*?</Arguments>", "<Arguments>other.py</Arguments>", self.tasks.xml)
        rc, out, _err = self.status()
        self.assertEqual(rc, 3)
        self.assertIn("not this project's", out)

    def test_run_info_unavailable(self):
        self.install()
        real = self.tasks.__call__

        def no_csv(*args):
            return (0, "unexpected\r\n", "") if "/FO" in args else real(*args)
        with mock.patch.object(schedule, "schtasks", no_csv):
            rc, out, _err = self.status()
        self.assertEqual(rc, 3)
        self.assertIn("run info: unavailable", out)

    def task_log(self, text):
        reports = self.fx.data_root / "reports"
        reports.mkdir(parents=True, exist_ok=True)
        (reports / "task.log").write_text(text, encoding="utf-8", newline="\n")

    def test_task_log_of_the_registered_config(self):
        self.install()
        self.tasks.last_result = "0"
        self.assertIn("task.log: none yet", self.status()[1])
        self.task_log("run\n2026-10-05T01:00:00Z step=capture rc=0 seconds=9\n  capture done run=1\n"
                      "2026-10-05T01:00:09Z step=index rc=0 seconds=1\n  index one=1\nrun\n")
        rc, out, _err = self.status()
        self.assertEqual(rc, 0)
        self.assertIn("task.log, previous run:\n", out)
        self.assertIn("    2026-10-05T01:00:00Z step=capture rc=0 seconds=9\n", out)
        self.assertIn("    2026-10-05T01:00:09Z step=index rc=0 seconds=1\n", out)
        self.assertIn("a newer run has started", out)
        self.assertNotIn("newest run", out)
        self.assertNotIn("capture done", out)
        self.assertNotIn("index one", out)
        self.task_log("run\n2026-10-05T01:00:00Z step=capture rc=0 seconds=9\n  capture done run=1\n")
        out = self.status()[1]
        self.assertIn("task.log, newest run, incomplete (no index step yet: still running, or stopped):\n", out)
        self.task_log("run\n2026-10-05T01:00:00Z step=capture rc=0 seconds=9\nrun\n")
        out = self.status()[1]
        self.assertIn("task.log, previous run, incomplete (stopped before its index step):\n", out)

    def test_a_failing_step_needs_attention_though_the_last_result_is_ok(self):
        """collect's exit code (Last Result) is capture's: an index that stops at a defect shows only in task.log."""
        self.install()
        self.tasks.last_result = "0"
        self.task_log("run\n2026-10-05T01:00:00Z step=capture rc=0 seconds=9\n"
                      "2026-10-05T01:00:09Z step=index rc=1 seconds=1\n  stderr: x\n")
        rc, out, _err = self.status()
        self.assertEqual(rc, 3)
        self.assertIn("step=index rc=1 seconds=1   <- not ok", out)

    def test_an_incomplete_run_is_labelled(self):
        self.install()
        self.tasks.last_result = "267009"
        self.task_log("run\n2026-10-05T01:00:00Z step=capture rc=0 seconds=9\n")
        rc, out, _err = self.status()
        self.assertEqual(rc, 0)
        self.assertIn("newest run, incomplete (no index step yet: still running, or stopped)", out)

    @unittest.skipUnless(WINDOWS, "drive-letter case")
    def test_another_spelling_of_the_project_is_the_same_task(self):
        """Installed from a prompt that kept a lower-case drive letter (or by the retired script, every path quoted),
        read from one that did not: the same task, still current."""
        self.install()
        self.tasks.last_result = "0"
        project = str(T.REPO.resolve())
        args = re.search(r"<Arguments>(.*?)</Arguments>", self.tasks.xml).group(1)
        other = args.replace(project, respelled(project))
        quoted = re.sub(r"(\S+manage\.py) collect toy --config (.+)$", r'"\1" collect toy --config "\2"', other)
        for spelled in (other, quoted):
            with self.subTest(args=spelled):
                self.tasks.xml = self.tasks.xml.replace(f"<Arguments>{args}</Arguments>",
                                                        f"<Arguments>{spelled}</Arguments>")
                rc, out, _err = self.status()
                self.assertEqual(rc, 0, out)
                self.assertIn("definition: current", out)
                args = spelled


class TestRunAndRemove(Case):
    def test_run(self):
        self.install()
        self.assertEqual(run("run")[0], 0)
        self.assertEqual(self.tasks.calls[-1], ("/Run", "/TN", GOOD["task"]))
        self.tasks.fail["/Run"] = (1, "", "ERROR: x\r\n")
        self.assertEqual(run("run")[0], 1)
        self.elevated = False
        n = len(self.tasks.calls)
        self.assertEqual(run("run")[0], 2)
        self.assertEqual(len(self.tasks.calls), n)

    def test_remove(self):
        self.install()
        self.elevated = False
        self.assertEqual(run("remove")[0], 2)
        self.elevated = True
        rc, out, _err = run("remove")
        self.assertEqual(rc, 0)
        self.assertEqual(self.tasks.calls[-1], ("/Delete", "/TN", GOOD["task"], "/F"))
        self.assertIn("removed", out)
        self.assertEqual(run("remove")[0], 1)                   # nothing left to remove
        self.assertEqual(run("status")[0], 3)


class TestManage(Case):
    def test_manage_py_dispatches_schedule(self):
        rc, out, _err = run("schedule", "status", entry=management.main)
        self.assertEqual(rc, 3)
        self.assertIn("not registered", out)

    def check(self, *argv):
        return run("check", *argv, entry=management.main)

    def test_check_reads_the_schedule(self):
        rc, out, _err = self.check()
        self.assertEqual(rc, 0, out)
        self.assertIn(f"toy: origins=toy_a,toy_b,toy_m search=yes schedule={GOOD['task']} every 3 h from 01:14", out)
        self.fx.set_cfg(toy_system=False)
        rc, out, _err = self.check("toy", "--config", self.fx.config_path)
        self.assertEqual(rc, 0)
        self.assertIn("config: ok", out)
        self.assertIn("note: schedule install would refuse this config: app toy: toy_system must be true", out)
        with mock.patch.object(schedule, "_schedule_value", lambda app: None):
            self.assertIn("schedule=none", self.check()[1])
        with mock.patch.object(schedule, "_schedule_value", lambda app: {**GOOD, "start": "25:00"}):
            rc, out, _err = self.check()
        self.assertEqual(rc, 1)
        self.assertIn("schedule=malformed (toy: schedule start: HH:MM)", out)

    def test_check_of_a_config(self):
        """A config the app refuses is exit 2; a single-app project may leave the app out; no config at the
        default place is not a problem (a machine that runs no capture)."""
        self.fx.set_cfg(toy_system="yes")
        rc, _out, err = self.check("--config", self.fx.config_path)
        self.assertEqual(rc, 2)
        self.assertIn("config refused", err)
        with mock.patch.object(T.paths, "DATA_PARENT", self.fx.base / "none"):
            rc, out, _err = self.check("toy")
        self.assertEqual(rc, 0)
        self.assertIn("config: none at", out)
        with mock.patch.object(registry, "discover", lambda: ["toy", "toy2"]):
            rc, _out, err = self.check("--config", self.fx.config_path)
        self.assertEqual(rc, 2)
        self.assertIn("--config belongs to one app", err)
        self.assertEqual(self.check("nope")[0], 2)

    def test_check_survives_a_broken_hook(self):
        """check and install read an app's task_checks through the same guard."""
        for hook, note in ((lambda s, c: None, "task_checks must return a list of strings"),
                           (lambda s, c: "xy", "task_checks must return a list of strings"),
                           (_raise(KeyError("k")), "task_checks failed (error KeyError)")):
            with self.subTest(note=note), mock.patch.object(toyapps.ToyConfig, "task_checks", hook):
                rc, out, _err = self.check("toy", "--config", self.fx.config_path)
                self.assertEqual(rc, 0)
                notes = [ln for ln in out.splitlines() if ln.startswith("note:")]
                self.assertEqual(notes, [f"note: schedule install would refuse this config: app toy: {note}"])

    def test_check_of_several_apps(self):
        """Two apps that register the same task, or start in the same minute."""
        with mock.patch.object(registry, "discover", lambda: ["toy", "toy2"]):
            rc, out, err = self.check()
            self.assertEqual(rc, 1)
            self.assertIn("toy and toy2 register the same task", err)
            other = {**GOOD, "task": "\\toy-folder\\other", "start": "04:14", "every_hours": 6}
            with mock.patch.object(schedule, "_schedule_value", lambda app: GOOD if app == "toy" else other):
                rc, out, err = self.check()
            self.assertEqual((rc, err), (0, ""))
            self.assertIn("note: schedule: toy and toy2 start at the same minute (04:14, 10:14, 16:14, 22:14)", out)

    def test_the_registry_wants_task_checks_to_take_the_config(self):
        from rawlayer import registry
        for hook in ([], lambda self: [], None):
            with self.subTest(hook=hook), mock.patch.object(toyapps.ToyConfig, "task_checks", hook), \
                    self.assertRaisesRegex(registry.RegistryError, "task_checks must be a method"):
                registry.Registry([T.TOY])


class TestProjectSettings(unittest.TestCase):
    def test_this_projects_schedules_are_well_formed(self):
        """Whatever this project's own apps say (no stand-ins here): no schedule, or a valid one."""
        for app in registry.discover():
            with self.subTest(app=app):
                s = schedule.task_settings(app)
                self.assertTrue(s is None or isinstance(s, schedule.Schedule))


def _raise(exc):
    def f(*_a, **_kw):
        raise exc
    return f


def _raises(fn):
    try:
        fn()
    except BaseException as e:      # noqa: BLE001 -- argparse exits
        return e
    return None


if __name__ == "__main__":
    unittest.main()
