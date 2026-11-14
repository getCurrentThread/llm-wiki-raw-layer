# -*- coding: utf-8 -*-
"""admin.py's offline behaviour (the client and frida are never touched: guards, the room scan, _attach,
_collect and the state writes are mocked; the state lock itself is real, on a temporary folder). The version
table: every row complete, the raw JS templates carrying no addresses or offsets (they are spliced in from
the table), an unknown FileVersion refused. send's
outcome classification: a failure before the injected script's [CALLED] marker is a refusal (exit 2); one
after it may follow a write (failed_after_write, exit 1); --dry-run never advances the counter; a missing or
invalid state file, or a state lock held by a concurrent send, refuses before anything is attached; only the
target room's WRITE response decides — errCode 0 sent, non-zero server_rejected, nothing attributable
unconfirmed (exit 1, with outcome_basis). calibrate's capture validation: exact 64-bit chat ids (a JS double
would round ids above 2^53) and the plain-room gate. listen takes each reply's msgId from the same counter
under the same lock and reports why it stopped.
"""
import contextlib
import io
import json
import os
import re
import sqlite3
import struct
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

if os.name == "nt":
    from kakaotalk import admin
    from appkit.archive import FetchError

VT = 0x2000000
SPEC = admin.VERSION_SPECS["26.9.1.5339"]
TPL = {"chatId": 0x10, "msgId": 0x1000, "roomVtable": VT, "sender_id": 7,
       "template_hex": "00" * 0x1D0, "texthdr_hex": "00" * 32}
CAP = {"chatId": str(2 ** 53 + 1), "msgId": 5, "senderLo": 7, "senderHi": 0, "roomVtable": VT,
       "room180": 10, "template_hex": "00" * 0x1D0, "texthdr_hex": "00" * 32}
SPEC_KEYS = frozenset({
    "builder", "builder_prologue", "reshandler", "reshandler_prologue", "loco_global", "loco_state_off",
    "loco_logged_in", "sentinel", "rdata_lo", "rdata_hi", "code_lo", "code_hi", "msg_size",
    "plain_no_extra", "builder_msgouter_off", "resh_err_off", "resh_msg_off", "msgouter_size",
    "msgouter_hi_off", "off_chat_id", "off_sender_lo", "off_sender_hi", "off_msg_id", "off_msg_pad",
    "off_text", "off_supplement", "off_extra", "off_extra_gate", "off_room_holder", "off_holder_chat",
    "text_hdr_pre", "text_hdr_bytes", "text_len_off", "text_block",
})


@unittest.skipIf(os.name != "nt", "admin.py loads kernel32 at import")
class TestVersionTable(unittest.TestCase):
    def test_every_row_is_complete(self):
        for version, spec in admin.VERSION_SPECS.items():
            with self.subTest(version=version):
                self.assertEqual(set(spec), SPEC_KEYS)     # nothing missing, no unused extras
                for key in SPEC_KEYS:
                    if key.endswith("_prologue"):
                        self.assertTrue(spec[key] is None
                                        or (isinstance(spec[key], str) and re.fullmatch(r"(?:[0-9a-f]{2}|\?\?)(?: (?:[0-9a-f]{2}|\?\?))*", spec[key])))
                    else:
                        self.assertIsInstance(spec[key], int)

    def test_the_raw_templates_carry_no_addresses_or_offsets(self):
        for name in ("CALIBRATE_JS", "SEND_JS", "LISTEN_JS", "OBSERVE_JS", "REPLY_OBSERVE_JS",
                     "EVENTS_OBSERVE_JS"):
            with self.subTest(name=name):
                self.assertEqual(re.findall(r"0x[0-9a-fA-F]+", getattr(admin, name)), [])

    def test_the_rendered_js_gets_the_tables_values(self):
        for version, spec in admin.VERSION_SPECS.items():
            with self.subTest(version=version):
                cal = admin._render(admin.CALIBRATE_JS, spec)
                send = admin._render(admin.SEND_JS, spec, room_vtable=VT, room=0x100, dry=0, msg_id=5,
                                     text='"hi"', chat_id="0x10", template_hex="00", texthdr_hex="00",
                                     window_ms=1)
                listen = admin._render(admin.LISTEN_JS, spec, prefix='"!!"', owner_lo=7, maxpayload=200)
                for name, js in (("calibrate", cal), ("send", send), ("listen", listen)):
                    self.assertNotIn("%(", js)
                    self.assertIn("ptr(%#x)" % spec["builder"], js)
                self.assertIn("clone.add(%#x).writeU64(chatId)" % spec["off_chat_id"], send)
                self.assertIn("holder.add(%#x).readU64().equals(CHAT_ID)" % spec["off_holder_chat"], send)
                self.assertIn("inner.add(%#x).readU32() !== PLAIN" % spec["off_extra_gate"], listen)

    def test_an_unknown_client_version_refuses(self):
        with self.assertRaises(FetchError) as cm:
            admin.spec_for("27.0.0.0")
        self.assertEqual(cm.exception.category, "version_mismatch")
        self.assertIs(admin.spec_for("26.9.1.5339"), SPEC)

    def test_check_names_the_supported_versions(self):
        with tempfile.TemporaryDirectory() as folder, \
                mock.patch.object(admin, "guards", lambda: (1, None, SPEC)), \
                mock.patch.object(admin, "read_template", lambda p, spec: TPL), \
                mock.patch.object(admin, "read_state", lambda p: {"msg_id_next": 0x1200}), \
                mock.patch.object(admin, "client_version", lambda: "26.9.1.5339"):
            body, code = admin.cmd_check(SimpleNamespace(template="t.json",
                                                         state=os.path.join(folder, "s.json")))
        self.assertEqual(code, 0)
        self.assertEqual((body["version"], body["supported_versions"]), ("26.9.1.5339", ["26.9.1.5339"]))


@unittest.skipIf(os.name != "nt", "admin.py loads kernel32 at import")
class TestSend(unittest.TestCase):
    def run_send(self, lines, rooms=None, dry_run=False, attach_error=None, state=None, state_dir=None,
                 real_state=False):
        """One send against mocked guards/rooms/attach; the state lock runs for real on a temporary folder
        (or on `state_dir`, a folder the test already holds the lock on). real_state: leave read_state and
        write_state unmocked, so a missing or invalid state file is refused by the real reader."""
        self.attached, self.writes = [], []

        def attach(pid, source):
            self.attached.append(source)
            if attach_error:
                raise attach_error
            return None, None, list(lines), []

        def collect(got, prefixes, timeout, stop=None):
            return next((l for l in got if l.startswith(prefixes)), None), list(got)

        own = None
        if state_dir is None:
            own = tempfile.TemporaryDirectory()
            state_dir = own.name
        if state is None:
            state = {"msg_id_next": 0x1200}
        try:
            a = SimpleNamespace(template="t.json", state=os.path.join(state_dir, "s.json"), chat_id=0x10,
                                message_file="m.txt", dry_run=dry_run)
            state_patches = [] if real_state else \
                [mock.patch.object(admin, "read_state", lambda p: dict(state)),
                 mock.patch.object(admin, "write_state", lambda p, v: self.writes.append(dict(v)))]
            with contextlib.ExitStack() as stack:
                for patch in [mock.patch.object(admin, "guards", lambda: (1, None, SPEC)),
                              mock.patch.object(admin, "read_template", lambda p, spec: TPL),
                              mock.patch.object(admin, "read_message", lambda p: "hi"),
                              mock.patch.object(admin, "find_rooms",
                                                lambda proc, cid, spec: {0x100: VT} if rooms is None else rooms),
                              mock.patch.object(admin, "_attach", attach),
                              mock.patch.object(admin, "_collect", collect),
                              mock.patch.object(admin, "_teardown", lambda s, sc: None)] + state_patches:
                    stack.enter_context(patch)
                return admin.cmd_send(a)
        finally:
            if own is not None:
                own.cleanup()

    def test_the_marker_precedes_the_builder_call(self):
        js = admin.SEND_JS
        self.assertLess(js.index("send('[CALLED]')"), js.index("f(ROOM, outer, 0)"))

    def test_chat_ids_stay_exact_across_the_js_bridge(self):
        cal = admin._render(admin.CALIBRATE_JS, SPEC)
        send = admin._render(admin.SEND_JS, SPEC, room_vtable=VT, room=0x100, dry=0, msg_id=5, text='"hi"',
                             chat_id="0x10", template_hex="00", texthdr_hex="00", window_ms=1)
        listen = admin._render(admin.LISTEN_JS, SPEC, prefix='"!!"', owner_lo=7, maxpayload=200)
        self.assertIn("chatId: inner.add(%#x).readU64().toString()" % SPEC["off_chat_id"], cal)
        self.assertIn("holder.add(%#x).readU64().equals(CHAT_ID)" % SPEC["off_holder_chat"], send)
        self.assertNotIn("* 0x100000000", cal + send + listen)   # the rounding doubles are gone

    def test_fail_before_the_marker_is_refused(self):
        body, code = self.run_send(["[FAIL] Error: room vtable mismatch"])
        self.assertEqual((body["status"], code), ("refused", 2))
        self.assertEqual(self.writes, [{"msg_id_next": 0x1201}])      # at-most-once: persisted before the call

    def test_outcome_after_the_marker(self):
        window = '[WINDOW-END] {"err": 0, "msg": "", "room": "16"}'   # 0x10: this room's own response
        for lines, status, code in (
                (["[READY]", "[CALLED]", "[FAIL] builder ret=0"], "failed_after_write", 1),
                (["[READY]", "[CALLED]"], "failed_after_write", 1),                    # a timeout
                (["[READY]", "[CALLED]", "[OK]", window], "sent", 0)):
            with self.subTest(lines=lines):
                body, code_got = self.run_send(lines)
                self.assertEqual((body["status"], code_got), (status, code))

    def test_only_the_target_rooms_response_decides(self):
        for resp, status, err, code in (
                ('{"err": 0, "msg": "", "room": "16"}', "sent", 0, 0),            # this chat: accepted
                ('{"err": -100, "msg": "x", "room": "16"}', "server_rejected", -100, 1),
                ('{"err": 0, "msg": "", "room": "17"}', "unconfirmed", None, 1),  # another room's only
                ('{"err": -100, "msg": "x", "room": "17"}', "unconfirmed", None, 1)):
            with self.subTest(resp=resp):
                body, code_got = self.run_send(["[READY]", "[CALLED]", "[OK]", "[WINDOW-END] " + resp])
                basis = None if status in ("sent", "server_rejected") else \
                    ("other_room_response" if '"17"' in resp else None)
                self.assertEqual((body["status"], body["err_code"], body.get("outcome_basis"), code_got),
                                 (status, err, basis, code))

    def test_no_matching_response_is_unconfirmed(self):
        for lines in (["[READY]", "[CALLED]", "[OK]"],                    # the window ended empty
                      ["[READY]", "[CALLED]", "[OK]", "[WINDOW-END] no response"]):
            with self.subTest(lines=lines):
                body, code = self.run_send(lines)
                self.assertEqual((body["status"], body["outcome_basis"], body["err_code"], code),
                                 ("unconfirmed", "no_response", None, 1))
                self.assertNotIn("log_id", body)        # nothing is claimed without a matching response

    def test_log_id_comes_exact_or_not_at_all(self):
        big = str(2 ** 53 + 7)               # an exact 64-bit id, beyond a JS double's exact integers
        for resp, extra in (
                ('{"err": 0, "msg": "", "room": "16", "logId": "%s", "sendAt": "1762000000123"}' % big,
                 {"log_id": big, "sent_at": "1762000000123"}),
                ('{"err": 0, "msg": "", "room": "16", "logId": "9007199254740993"}',   # 2^53 + 1, exact
                 {"log_id": "9007199254740993"}),
                ('{"err": 0, "msg": "", "room": "16", "sendAt": 1762000000123}',      # a plain int is fine
                 {"sent_at": "1762000000123"}),
                ('{"err": 0, "msg": "", "room": "16"}', {}),                          # absent -> absent
                ('{"err": 0, "msg": "", "room": "16", "logId": "9007199254740992.5"}', {}),  # not exact
                ('{"err": 0, "msg": "", "room": "16", "logId": 9007199254740992}', {}),      # a JS number
                ('{"err": 0, "msg": "", "room": "17", "logId": "%s"}' % big, {}),    # another room's
                ('{"err": 0, "msg": "", "room": "16", "logId": "-5", "sendAt": "0"}', {})):
            with self.subTest(resp=resp):
                body, code = self.run_send(["[READY]", "[CALLED]", "[OK]", "[WINDOW-END] " + resp])
                if '"17"' in resp:                     # only another room answered: unknown, nothing claimed
                    self.assertEqual((body["status"], code), ("unconfirmed", 1))
                    self.assertNotIn("log_id", body)
                    self.assertNotIn("sent_at", body)
                    continue
                self.assertEqual((code, body["status"]), (0, "sent"))
                for key in ("log_id", "sent_at"):
                    self.assertEqual(body.get(key), extra.get(key))   # absent fields stay absent keys

    def test_an_attach_or_load_error_may_follow_a_write(self):
        """script.load() runs the script up to the client's send call: a frida error there is not a refusal."""
        body, code = self.run_send([], attach_error=RuntimeError("transport lost"))
        self.assertEqual((body["status"], body["detail"], code), ("failed_after_write", "RuntimeError", 1))
        with self.assertRaises(FetchError):
            self.run_send([], attach_error=FetchError("frida_missing"))
        with self.assertRaises(RuntimeError):
            self.run_send([], attach_error=RuntimeError("x"), dry_run=True)

    def test_dry_run_does_not_persist_the_counter(self):
        body, code = self.run_send(["[READY]", "[DRY] no builder call"], dry_run=True)
        self.assertEqual((body["status"], code), ("dry_run", 0))
        self.assertEqual(self.writes, [])

    def test_a_missing_or_invalid_state_is_refused(self):
        for content in (None, '{"msg_id_next": "5"}', "{not json"):   # the real reader: missing -> invalid -> {}
            with self.subTest(content=content), tempfile.TemporaryDirectory() as folder:
                if content is not None:
                    Path(folder, "s.json").write_text(content, encoding="utf-8")
                with self.assertRaises(FetchError) as cm:
                    self.run_send(["[READY]"], state_dir=folder, real_state=True)
                self.assertEqual(cm.exception.category, "bad_state")
                self.assertEqual((self.attached, self.writes), ([], []))

    def test_a_send_holding_the_state_lock_refuses_busy(self):
        with tempfile.TemporaryDirectory() as folder:
            with admin.locked(Path(folder)):                  # a concurrent send is mid-flight
                with self.assertRaises(FetchError) as cm:
                    self.run_send([], state_dir=folder)
                self.assertEqual(cm.exception.category, "state_busy")
        self.assertEqual((self.attached, self.writes), ([], []))

    def test_room_refusals_attach_nothing(self):
        for rooms, category in (({}, "no_room"), ({0x100: VT, 0x200: VT}, "ambiguous_room"),
                                ({0x100: VT + 4}, "no_room")):
            with self.assertRaises(FetchError) as cm:
                self.run_send([], rooms=rooms)
            self.assertEqual(cm.exception.category, category)
            self.assertEqual((self.attached, self.writes), ([], []))


@unittest.skipIf(os.name != "nt", "admin.py loads kernel32 at import")
class TestCalibrate(unittest.TestCase):
    def test_exact_64bit_chat_ids_parse_without_rounding(self):
        self.assertEqual(admin._validated(dict(CAP), SPEC), 2 ** 53 + 1)     # a JS double would round this id

    def test_a_malformed_capture_refuses(self):
        for broken in ({"chatId": "9007199254740992.5"}, {"chatId": "0x10"}, {"chatId": None},
                       {"msgId": 0}, {"senderHi": 1}, {"roomVtable": 0x400000},
                       {"template_hex": "00"}, {"texthdr_hex": "zz" * 32}):
            with self.subTest(broken=broken):
                with self.assertRaises(FetchError) as cm:
                    admin._validated({**CAP, **broken}, SPEC)
                self.assertEqual(cm.exception.category, "capture_failed")

    def test_a_capture_that_is_not_a_plain_room_refuses(self):
        for room180 in (30, None):
            with self.subTest(room180=room180):
                with self.assertRaises(FetchError) as cm:
                    admin._validated({**CAP, "room180": room180}, SPEC)
                self.assertEqual(cm.exception.category, "not_plain_room")


@unittest.skipIf(os.name != "nt", "admin.py loads kernel32 at import")
class TestListen(unittest.TestCase):
    class Clock:                              # listen's loop and id handshake, without real waiting
        def __init__(self, interrupt=False):
            self.t, self.interrupt = 1000.0, interrupt

        def time(self):
            return self.t

        def sleep(self, seconds):
            if self.interrupt:
                raise KeyboardInterrupt
            self.t += seconds

    def run_listen(self, lines, detached=(), lifetime=1, clock=None, post=None, state=None):
        self.writes = []
        script = mock.Mock()
        if post is not None:
            script.post.side_effect = post
        with tempfile.TemporaryDirectory() as folder, \
                mock.patch.object(admin, "guards", lambda: (1, None, SPEC)), \
                mock.patch.object(admin, "read_template", lambda p, spec: TPL), \
                mock.patch.object(admin, "read_state",
                                  lambda p: {"msg_id_next": 5} if state is None else state), \
                mock.patch.object(admin, "write_state", lambda p, v: self.writes.append(dict(v))), \
                mock.patch.object(admin, "_attach", lambda pid, src: (None, script, lines, list(detached))), \
                mock.patch.object(admin, "_teardown", lambda s, sc: None), \
                mock.patch.object(admin, "time", clock or self.Clock()), \
                mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            a = SimpleNamespace(template="t.json", state=os.path.join(folder, "s.json"),
                                prefix="!!", lifetime=lifetime)
            body, code = admin.cmd_listen(a)
        return body, code, out.getvalue(), script

    def test_stop_reasons(self):
        for lifetime, clock, detached, reason in (
                (1, self.Clock(), (), "lifetime"),
                (0, self.Clock(interrupt=True), (), "interrupt"),
                (1, self.Clock(), ("connection-terminated",), "detached")):
            with self.subTest(reason=reason):
                body, code, _out, _script = self.run_listen([], lifetime=lifetime, clock=clock,
                                                            detached=detached)
                self.assertEqual((body["stopped_by"], body["commands"], code), (reason, 0, 0))

    def test_listen_needs_the_state_send_uses(self):
        with self.assertRaises(FetchError) as cm:
            self.run_listen([], state={})
        self.assertEqual(cm.exception.category, "bad_state")

    def test_each_reply_takes_its_msgid_from_the_shared_counter(self):
        lines = ["[CMD] hello", "[TAKE-ID] 1"]

        def posted(message):                  # the JS side: the id arrives, the builder runs, the lock ends
            lines.extend(("[OK] msgId=0x5", "[ID-DONE] %d" % message["seq"]))

        body, code, out, script = self.run_listen(lines, post=posted)
        self.assertEqual((body["stopped_by"], body["commands"], code), ("lifetime", 1, 0))
        self.assertEqual(self.writes, [{"msg_id_next": 6}])          # persisted before the builder call
        script.post.assert_called_once_with({"type": "id", "seq": 1, "msgId": 5})
        events = [json.loads(line)["event"] for line in out.splitlines()]
        self.assertEqual(events, ["[CMD] hello", "[OK] msgId=0x5"])  # the id handshake itself is silent


@unittest.skipIf(os.name != "nt", "admin.py loads kernel32 at import")
class TestObserve(unittest.TestCase):
    def test_the_reply_capture_hook_renders_from_the_table(self):
        js = admin._render(admin.REPLY_OBSERVE_JS, SPEC)
        self.assertNotIn("%(", js)
        self.assertIn("ptr(%#x)" % SPEC["builder"], js)
        self.assertIn("if (xp !== SENTINEL)", js)             # the plain path's sentinel extra pointer

    def test_the_reply_capture_writes_its_file_and_reports_shapes(self):
        cap_obj = {"chatId": "9007199254740993", "msgId": 5, "gate": 26, "textUtf8": 3,
                   "extra": {"ptr": 4251080, "hdr": "00" * 16, "len": 91, "text": '{"src_logId": 39}'},
                   "template_hex": "00", "texthdr_hex": "00"}
        cap = "[CAP] " + json.dumps(cap_obj)
        with tempfile.TemporaryDirectory() as folder, \
                mock.patch.object(admin, "guards", lambda: (1, None, SPEC)), \
                mock.patch.object(admin, "_attach", lambda pid, src: (None, None, [cap], [])), \
                mock.patch.object(admin, "_collect",
                                  lambda got, prefixes, timeout, stop=None:
                                  (next((l for l in got if l.startswith(prefixes)), None), list(got))), \
                mock.patch.object(admin, "_teardown", lambda s, sc: None):
            body, code = admin._observe_reply(SimpleNamespace(template=os.path.join(folder, "tpl.json")))
            written = json.loads(Path(body["written"]).read_text(encoding="utf-8"))
        self.assertEqual((code, body["status"], body["gate"], body["extra_len"]), (0, "ok", 26, 91))
        self.assertIn("src_logId", body["extra"])            # the owner's allowed real-value output
        self.assertEqual((written["chatId"], written["msgId"], written["gate"]),
                         (2 ** 53 + 1, 5, 26))               # exact 64-bit, never through a double
        self.assertEqual(written["extra_text"], body["extra"])

    def test_an_unfired_reply_hook_reports_what_was_seen(self):
        lines = ["[ARMED] REPLY once by hand in the UI now; admin sends nothing"]   # 62 chars: saw truncates
        with tempfile.TemporaryDirectory() as folder, \
                mock.patch.object(admin, "guards", lambda: (1, None, SPEC)), \
                mock.patch.object(admin, "_attach", lambda pid, src: (None, None, list(lines), [])), \
                mock.patch.object(admin, "_collect",
                                  lambda got, prefixes, timeout, stop=None: (None, list(got))), \
                mock.patch.object(admin, "_teardown", lambda s, sc: None):
            body, code = admin._observe_reply(SimpleNamespace(template=os.path.join(folder, "tpl.json")))
        self.assertEqual((code, body["status"]), (2, "no_observation"))
        self.assertEqual(body["saw"], [l[:60] for l in lines])

    def test_the_observation_hook_renders_from_the_table(self):
        js = admin._render(admin.OBSERVE_JS, SPEC, peek=64)
        self.assertNotIn("%(", js)
        self.assertIn("ptr(%#x)" % SPEC["reshandler"], js)
        self.assertIn("send('[OBS] ' + JSON.stringify(out))", js)

    def test_an_unfired_hook_reports_what_was_seen(self):
        lines = ["[ARMED] send ONE message by hand now; admin sends nothing"]
        with mock.patch.object(admin, "guards", lambda: (1, None, SPEC)), \
                mock.patch.object(admin, "read_template", lambda p, spec: TPL), \
                mock.patch.object(admin, "_attach", lambda pid, src: (None, None, list(lines), [])), \
                mock.patch.object(admin, "_collect",
                                  lambda got, prefixes, timeout, stop=None: (None, list(got))), \
                mock.patch.object(admin, "_teardown", lambda s, sc: None):
            body, code = admin.cmd_observe(SimpleNamespace(template="t.json", state=None))
        self.assertEqual((code, body["status"]), (2, "no_observation"))
        self.assertEqual(body["saw"], lines)      # armed-but-never-fired, told apart from a load error

    def test_observation_output_masks_the_owners_ids(self):
        chat, sender = 0xAB000000CD, 4242
        secrets = {s for s in (chat, sender, chat >> 32, chat & 0xFFFFFFFF) if s}
        obs = {"room": str(chat), "regs": {"edx": {"u32": [sender, 5], "u64": [str(chat), "1762000000123"]}},
               "args": [{"esp": 8, "u32": sender, "peek": {"u32": [sender, chat & 0xFFFFFFFF],
                                                           "u64": [str(chat), "9007199254740993"]}}]}
        masked = admin._masked(obs, secrets)
        self.assertEqual(masked["room"], "<id>")
        self.assertEqual(masked["regs"]["edx"]["u32"], ["<id>", 5])      # timestamps and candidates pass
        self.assertEqual(masked["regs"]["edx"]["u64"], ["<id>", "1762000000123"])
        self.assertEqual(masked["args"][0]["u32"], "<id>")
        self.assertEqual(masked["args"][0]["peek"]["u32"], ["<id>", "<id>"])
        self.assertEqual(masked["args"][0]["peek"]["u64"], ["<id>", "9007199254740993"])


@unittest.skipIf(os.name != "nt", "admin.py loads kernel32 at import")
class TestRoomResolution(unittest.TestCase):
    ROOMS = [("16", "가족 채팅방"), ("17", "Work Room"), ("18", ""), ("19", "100% Room")]

    def resolve(self, fragment, rooms=None):
        """resolve_room against a synthetic index in a temporary data root (the real index is never opened):
        search's index helpers are pointed at it by patching load_data_root and default_config."""
        own = tempfile.TemporaryDirectory()
        try:
            path = Path(own.name, "index", "kakaotalk.sqlite")
            path.parent.mkdir(parents=True)
            con = sqlite3.connect(path)
            con.execute("CREATE TABLE rooms(chat_id TEXT PRIMARY KEY, title TEXT NOT NULL, n INTEGER NOT NULL, "
                        "senders INTEGER NOT NULL, first_ts INTEGER, last_ts INTEGER, n_deleted INTEGER NOT NULL,"
                        " n_gone INTEGER NOT NULL) WITHOUT ROWID")
            con.executemany("INSERT INTO rooms(chat_id, title, n, senders, n_deleted, n_gone) "
                            "VALUES (?, ?, 1, 1, 0, 0)", self.ROOMS if rooms is None else rooms)
            con.commit()
            con.close()
            with mock.patch("rawlayer.search.index.load_data_root", lambda cfg: Path(own.name)), \
                    mock.patch("rawlayer.management.base.default_config", lambda app: "unused-cfg"):
                return admin.resolve_room(fragment)
        finally:
            own.cleanup()

    def test_a_fragment_resolves_through_the_synthetic_index(self):
        self.assertEqual(self.resolve("가족"), 16)                  # substring, any position
        self.assertEqual(self.resolve("WORK"), 17)                 # case-insensitive
        self.assertEqual(self.resolve("100%"), 19)                 # the wildcard is escaped, not a pattern
        self.assertEqual(self.resolve("채팅"), 16)                  # one match among the titled rooms

    def test_zero_or_many_matches_refuse(self):
        for fragment, category in (("없는 방", "room_not_found"), ("oo", "ambiguous_room_title"),
                                   ("", "bad_room_fragment"), ("   ", "bad_room_fragment")):
            with self.subTest(fragment=fragment):
                with self.assertRaises(FetchError) as cm:
                    self.resolve(fragment)
                self.assertEqual(cm.exception.category, category)

    def test_a_missing_index_refuses(self):
        own = tempfile.TemporaryDirectory()
        try:
            with mock.patch("rawlayer.search.index.load_data_root", lambda cfg: Path(own.name)), \
                    mock.patch("rawlayer.management.base.default_config", lambda app: "unused-cfg"):
                with self.assertRaises(FetchError) as cm:
                    admin.resolve_room("anything")
            self.assertEqual(cm.exception.category, "index_missing")
        finally:
            own.cleanup()

    def test_main_takes_exactly_one_of_chat_id_and_room(self):
        for argv, category in (
                (["rooms", "--template", "t.json", "--chat-id", "0x10", "--room", "x"], "chat_id_exclusive"),
                (["rooms", "--template", "t.json"], "chat_id_required"),
                (["send", "--template", "t.json", "--message-file", "m.txt"], "chat_id_required")):
            with self.subTest(argv=argv):
                guards = mock.Mock()
                with mock.patch.object(admin, "data_path", lambda v: Path(v)), \
                        mock.patch.object(admin, "guards", guards), \
                        mock.patch("sys.stdout", new_callable=io.StringIO) as out:
                    code = admin.main(argv)
                self.assertEqual(code, 2)
                self.assertEqual(json.loads(out.getvalue())["status"], category)
                guards.assert_not_called()          # refused before anything touched the client

    def test_main_resolves_room_before_guards(self):
        own = tempfile.TemporaryDirectory()
        try:
            path = Path(own.name, "index", "kakaotalk.sqlite")
            path.parent.mkdir(parents=True)
            con = sqlite3.connect(path)
            con.execute("CREATE TABLE rooms(chat_id TEXT PRIMARY KEY, title TEXT NOT NULL, n INTEGER NOT NULL, "
                        "senders INTEGER NOT NULL, first_ts INTEGER, last_ts INTEGER, n_deleted INTEGER NOT NULL,"
                        " n_gone INTEGER NOT NULL) WITHOUT ROWID")
            con.execute("INSERT INTO rooms(chat_id, title, n, senders, n_deleted, n_gone) "
                        "VALUES ('16', '가족 채팅방', 1, 1, 0, 0)")
            con.commit()
            con.close()
            seen = {}

            def rooms_cmd(a):
                seen["chat_id"] = a.chat_id
                return {"status": "ok"}, 0

            with mock.patch("rawlayer.search.index.load_data_root", lambda cfg: Path(own.name)), \
                    mock.patch("rawlayer.management.base.default_config", lambda app: "unused-cfg"), \
                    mock.patch.object(admin, "data_path", lambda v: Path(v)), \
                    mock.patch.object(admin, "COMMANDS", {**admin.COMMANDS, "rooms": rooms_cmd}), \
                    mock.patch("sys.stdout", new_callable=io.StringIO):
                self.assertEqual(admin.main(["rooms", "--template", "t.json", "--room", "가족"]), 0)
            self.assertEqual(seen["chat_id"], 16)   # an int, resolved in main before the command runs
        finally:
            own.cleanup()


@unittest.skipIf(os.name != "nt", "admin.py loads kernel32 at import")
class TestEventsDiscovery(unittest.TestCase):
    VT = 0x500000                                     # a synthetic vtable address
    ENTRIES = [0x1000, SPEC["code_lo"] + 0x1000, 0x2000000, SPEC["code_lo"] + 0x2000,
               SPEC["code_hi"] - 0x10]                # 3 code-range candidates survive the filter

    class FakeProc:
        def __init__(self, entries):
            self.entries = entries

        def u32(self, address):
            return {SPEC["loco_global"]: 0x300000, 0x300000: TestEventsDiscovery.VT}.get(address)

        def read(self, address, size):
            if address == TestEventsDiscovery.VT and size == 4 * admin.EVENTS_CANDIDATE_SLOTS:
                blob = self.entries + [0] * (admin.EVENTS_CANDIDATE_SLOTS - len(self.entries))
                return struct.pack("<%dI" % len(blob), *blob)
            return None

    class Clock:                              # the full-window wait, without real waiting
        def __init__(self):
            self.t = 1000.0

        def time(self):
            return self.t

        def sleep(self, seconds):
            self.t += seconds

        def strftime(self, fmt, ts):
            return "armed"

        def localtime(self, ts=None):
            return ts

    def run_events(self, lines, entries=None):
        proc = self.FakeProc(self.ENTRIES if entries is None else entries)
        with mock.patch.object(admin, "guards", lambda: (1, proc, SPEC)), \
                mock.patch.object(admin, "read_template", lambda p, spec: TPL), \
                mock.patch.object(admin, "_attach", lambda pid, src: (None, None, list(lines), [])), \
                mock.patch.object(admin, "time", self.Clock()), \
                mock.patch.object(admin, "_teardown", lambda s, sc: None):
            return admin._observe_events(SimpleNamespace(template="t.json"))

    def test_the_discovery_hook_reports_counts_times_and_masked_shapes(self):
        shape = {"args": [{"esp": 4, "u32": 7,
                           "peek": {"u32": [7], "u64": [str(TPL["chatId"]), "9007199254740993"]}}],
                 "room_match": True, "ecx_room_match": False}
        lines = ["[ARMED] send ONE message from the PHONE now; admin sends nothing",
                 "[FIRE] " + json.dumps({"slot": 1, "t": 120, "shape": shape}),
                 "[FIRE] " + json.dumps({"slot": 1, "t": 61000}),           # a recurring fire: count only
                 "[FIRE] " + json.dumps({"slot": 1, "t": 121000}),
                 "[FIRE] " + json.dumps({"slot": 2, "t": 400, "shape": shape})]
        body, code = self.run_events(lines)
        self.assertEqual((code, body["status"], body["hooked"]), (0, "ok", 3))
        self.assertEqual(body["vtable_slots"], admin.EVENTS_CANDIDATE_SLOTS)
        self.assertEqual(body["armed_at"], "armed")
        self.assertEqual(len(body["candidates"]), 3)   # code addresses are printed: they are not ids
        by_slot = {f["slot"]: f for f in body["fired"]}
        self.assertEqual((by_slot[1]["count"], by_slot[1]["times"]), (3, [120, 61000, 121000]))
        self.assertEqual(len(by_slot[1]["shapes"]), 1)  # arguments read on the first entries only
        peeked = by_slot[1]["shapes"][0]["args"][0]["peek"]["u64"]
        self.assertEqual(peeked[0], "<id>")             # the room id, masked
        self.assertEqual(peeked[1], "9007199254740993")
        source = admin._render(admin.EVENTS_OBSERVE_JS, SPEC, room_chat="0x10",
                               cands="ptr(0x1000)", peek=64)
        self.assertNotIn("%(", source)
        self.assertIn("uint64('0x10')", source)        # the known room id, as the exact-compare lens
        self.assertIn("CANDS = [ptr(0x1000)]", source)

    def test_no_code_candidates_refuse_before_attaching(self):
        attached = []
        with mock.patch.object(admin, "guards",
                               lambda: (1, self.FakeProc([0x1000, 0x2000000]), SPEC)), \
                mock.patch.object(admin, "read_template", lambda p, spec: TPL), \
                mock.patch.object(admin, "time", self.Clock()), \
                mock.patch.object(admin, "_attach",
                                  lambda pid, src: attached.append(src) or (None, None, [], [])):
            with self.assertRaises(FetchError) as cm:
                admin._observe_events(SimpleNamespace(template="t.json"))
        self.assertEqual(cm.exception.category, "no_candidates")
        self.assertEqual(attached, [])                 # refused before anything was hooked

    def test_no_fire_reports_no_observation(self):
        body, code = self.run_events(["[ARMED] send ONE message from the PHONE now; admin sends nothing"])
        self.assertEqual((code, body["status"], body["fired"]), (2, "no_observation", []))


@unittest.skipIf(os.name != "nt", "admin.py loads kernel32 at import")
class TestClient(unittest.TestCase):
    def test_client_pid_tells_two_clients_apart_from_none(self):
        for out, result in ((b"4242", 4242), (b"4242\n4243", "ambiguous_client"),
                            (b"", "client_not_running"), (b"pid", "client_not_running")):
            with self.subTest(out=out), mock.patch.object(admin, "_powershell", lambda cmd: out):
                if isinstance(result, int):
                    self.assertEqual(admin.client_pid(), result)
                else:
                    with self.assertRaises(FetchError) as cm:
                        admin.client_pid()
                    self.assertEqual(cm.exception.category, result)


if __name__ == "__main__":
    unittest.main()
