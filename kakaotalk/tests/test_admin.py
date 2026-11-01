# -*- coding: utf-8 -*-
"""admin.py send's outcome classification (the client and frida are never touched: guards, the room scan,
_attach, _collect and the state writes are mocked). A failure before the injected script's [CALLED] marker
is a refusal (exit 2); one after it may follow a write (failed_after_write, exit 1); --dry-run never
advances the counter; the room refusals happen before anything is attached.

    python -s -B -m unittest discover -s kakaotalk/tests -t .
"""
import os
import unittest
from types import SimpleNamespace
from unittest import mock

if os.name == "nt":
    from kakaotalk import admin
    from tools.archiveio import FetchError

VT = 0x2000000
TPL = {"msgId": 0x1000, "roomVtable": VT, "sender_id": 7, "template_hex": "00" * 0x1D0, "texthdr_hex": "00" * 32}


@unittest.skipIf(os.name != "nt", "admin.py loads kernel32 at import")
class TestSend(unittest.TestCase):
    def run_send(self, lines, rooms=None, dry_run=False, attach_error=None):
        self.attached, self.writes = [], []

        def attach(pid, source):
            self.attached.append(source)
            if attach_error:
                raise attach_error
            return None, None, list(lines)

        def collect(got, prefixes, timeout):
            return next((l for l in got if l.startswith(prefixes)), None), list(got)

        a = SimpleNamespace(template="t.json", state="s.json", chat_id="0x10", message_file="m.txt",
                            dry_run=dry_run)
        with mock.patch.object(admin, "guards", lambda: (1, None)), \
                mock.patch.object(admin, "read_template", lambda p: TPL), \
                mock.patch.object(admin, "read_message", lambda p: "hi"), \
                mock.patch.object(admin, "find_rooms", lambda proc, cid: {0x100: VT} if rooms is None else rooms), \
                mock.patch.object(admin, "read_state", lambda p: {}), \
                mock.patch.object(admin, "write_state", lambda p, v: self.writes.append(dict(v))), \
                mock.patch.object(admin, "_attach", attach), \
                mock.patch.object(admin, "_collect", collect), \
                mock.patch.object(admin, "_teardown", lambda s, sc: None):
            return admin.cmd_send(a)

    def test_the_marker_precedes_the_builder_call(self):
        js = admin.SEND_JS
        self.assertLess(js.index("send('[CALLED]')"), js.index("f(ROOM, outer, 0)"))

    def test_fail_before_the_marker_is_refused(self):
        body, code = self.run_send(["[FAIL] Error: room vtable mismatch"])
        self.assertEqual((body["status"], code), ("refused", 2))
        self.assertEqual(self.writes, [{"msg_id_next": 0x1201}])      # at-most-once: persisted before the call

    def test_fail_after_the_marker_is_failed_after_write(self):
        body, code = self.run_send(["[READY]", "[CALLED]", "[FAIL] builder ret=0"])
        self.assertEqual((body["status"], code), ("failed_after_write", 1))

    def test_timeout_after_the_marker_is_failed_after_write(self):
        body, code = self.run_send(["[READY]", "[CALLED]"])
        self.assertEqual((body["status"], code), ("failed_after_write", 1))

    def test_an_attach_or_load_error_may_follow_a_write(self):
        """script.load() runs the script up to the client's send call: a frida error there is not a refusal."""
        body, code = self.run_send([], attach_error=RuntimeError("transport lost"))
        self.assertEqual((body["status"], body["detail"], code), ("failed_after_write", "RuntimeError", 1))
        with self.assertRaises(FetchError):
            self.run_send([], attach_error=FetchError("frida_missing"))
        with self.assertRaises(RuntimeError):
            self.run_send([], attach_error=RuntimeError("x"), dry_run=True)

    def test_ok_is_sent(self):
        body, code = self.run_send(["[READY]", "[CALLED]", "[OK]", '[WINDOW-END] {"err": 0, "msg": ""}'])
        self.assertEqual((body["status"], code), ("sent", 0))

    def test_dry_run_does_not_persist_the_counter(self):
        body, code = self.run_send(["[READY]", "[DRY] no builder call"], dry_run=True)
        self.assertEqual((body["status"], code), ("dry_run", 0))
        self.assertEqual(self.writes, [])

    def test_room_refusals_attach_nothing(self):
        for rooms, category in (({}, "no_room"), ({0x100: VT, 0x200: VT}, "ambiguous_room"),
                                ({0x100: VT + 4}, "no_room")):
            with self.assertRaises(FetchError) as cm:
                self.run_send([], rooms=rooms)
            self.assertEqual(cm.exception.category, category)
            self.assertEqual((self.attached, self.writes), ([], []))


if __name__ == "__main__":
    unittest.main()
