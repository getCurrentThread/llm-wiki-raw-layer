# -*- coding: utf-8 -*-
"""KakaoTalk administration: scriptable message sending through the running Windows client's own Loco WRITE
path — the write-side counterpart of the read-only collector, mattermost admin.py's counterpart here. The
client is driven with frida, so session, TLS and sync handling stay entirely the client's own; no credential
is read, stored or printed (the app's key material is never touched).

A calibration template (JSON: captured client bytes that identify the owner's account, so it lives under the
data parent like a token file) is produced once by `calibrate` from ONE real send, in any plain-text room:

  {"v": 1, "captured_at": "...", "chatId": <int>, "msgId": <int>, "roomVtable": <int>, "sender_id": <int>,
   "template_hex": "<0x1d0 bytes of the message object>", "texthdr_hex": "<32 bytes of its string header>"}

  check      read-only: client version guard, login state, WRITE-builder prologue, template and id headroom
  calibrate  capture the owner's next send (one message, any plain-text room) into the template + state files
  rooms      read-only: list the TalkChatRoom objects a chatId has, vtable-annotated against the template
  send       send one message with no message of yours: a minimal clone whose every pointer the builder could
             dereference is replaced — supplement sentinel, no-extra (+0x180 = 10) path, its own text block
  listen     stay resident and, 0.3s after the owner sends a message starting with the prefix, send the same
             message prefix-stripped into the same room (the proven fresh-capture path)

`send` writes at most once: the message-id counter in the state file (seeded 0x200 past the calibrated id,
which the server accepts as an offset) is persisted before the client is called. Text comes from a file (as
mattermost `post`), is at most 200 UTF-8 bytes, and is never printed; neither are template bytes. Addresses
are locked to client 26.9.1.5339 (x86, image base 0x400000, Themida): a version or prologue mismatch refuses
before anything is touched. `send` and `listen` reach the rooms whose own messages carry no "extra" field
(+0x180 == 10 — the self-chat room and the plain rooms); rooms that need "extra" are not covered yet.
Delivery is judged by the client's WRITE response handler (errCode 0) and, ultimately, by the room — the
client renders received messages up to a minute late. A "server unstable" dialog right after a send means a
long-lived client session went stale, not a crash: restart the client and send again.
Exit codes: 0 ok, 1 a write happened but was rejected or failed after it, 2 refused before any write."""
import argparse
import ctypes
import json
import os
from pathlib import Path
import re
import struct
import subprocess
import sys
import time

from tools.archiveio import CollectorError, atomic, load, now

VERSION = "26.9.1.5339"
BUILDER = 0x017F9FF0                    # TalkChatRoom WRITE packet builder (thiscall: room, msgOuter, 0)
BUILDER_PROLOGUE = bytes.fromhex("558bec6aff68")
LOCO_GLOBAL = 0x02AF1008                 # *(&) = LocoManager; +0xC0 == 6 means logged in
SENTINEL = 0x0267D2AA                    # the client's empty-slot value for optional string members
RESHANDLER = 0x018046B0                  # TalkChatRoom::LocoCommand_WRITE_ResultHandler_v3
RDATA_LO, RDATA_HI = 0x01F00000, 0x02720000   # image .rdata range a room vtable must sit in
MSG_SIZE = 0x1D0                         # the message object the builder reads
TEXT_MAX_UTF8 = 200
CALIBRATE_WAIT = 180                     # seconds the owner has to send the calibration message
RESPONSE_WAIT = 12                       # seconds a send waits for the client's WRITE response

_HEX = re.compile(r"[0-9a-f]+\Z")
k32 = ctypes.windll.kernel32


def bad(category):
    raise CollectorError(category)


# ---------------------------------------------------------------------------------------------------------------- client

def _powershell(command):
    try:
        return subprocess.check_output(["powershell", "-NoProfile", "-Command", command], timeout=30).strip()
    except Exception:
        bad("client_not_running" if "ErrorAction Stop" in command else "version_unreadable")


def client_pid():
    if os.name != "nt":
        bad("windows_only")
    return int(_powershell("(Get-Process -Name KakaoTalk -ErrorAction Stop).Id"))


def client_version():
    return _powershell(
        "(Get-Item (Get-Process -Name KakaoTalk).MainModule.FileName).VersionInfo.FileVersion").decode()


class Proc:
    """Read-only memory access to the client, for the state, prologue and room scans."""

    def __init__(self, pid):
        self.h = k32.OpenProcess(0x0410, False, pid)      # PROCESS_QUERY_INFORMATION | PROCESS_VM_READ
        if not self.h:
            bad("process_unreadable")

    def read(self, address, size):
        buf = ctypes.create_string_buffer(size)
        got = ctypes.c_size_t(0)
        if k32.ReadProcessMemory(self.h, ctypes.c_void_p(address), buf, size, ctypes.byref(got)):
            return buf.raw[:got.value]
        return None

    def u32(self, address):
        r = self.read(address, 4)
        return struct.unpack("<I", r)[0] if r else None

    def regions(self):
        class MBI(ctypes.Structure):
            _fields_ = [("BaseAddress", ctypes.c_void_p), ("AllocationBase", ctypes.c_void_p),
                        ("AllocationProtect", ctypes.c_ulong), ("RegionSize", ctypes.c_size_t),
                        ("State", ctypes.c_ulong), ("Protect", ctypes.c_ulong), ("Type", ctypes.c_ulong)]
        addr, mbi = 0, MBI()
        while addr < 0x7FFF0000:
            if k32.VirtualQueryEx(self.h, ctypes.c_void_p(addr), ctypes.byref(mbi), ctypes.sizeof(mbi)) == 0:
                break
            base = mbi.BaseAddress or 0
            if mbi.State == 0x1000 and mbi.Protect in (2, 4, 8, 0x20, 0x40) and mbi.RegionSize:
                yield base, mbi.RegionSize, mbi.Type
            addr = base + mbi.RegionSize


def guards():
    """Everything read-only that must hold before the client is touched at all."""
    pid = client_pid()
    if client_version() != VERSION:
        bad("version_mismatch")
    proc = Proc(pid)
    lm = proc.u32(LOCO_GLOBAL)
    if not lm or proc.u32(lm + 0xC0) != 6:
        bad("not_logged_in")
    if proc.read(BUILDER, 6) != BUILDER_PROLOGUE:
        bad("builder_changed")
    return pid, proc


def find_rooms(proc, chat_id):
    """{room address: vtable} for the TalkChatRoom objects holding chatId: scan the id, then its holders'
    pointer sites (room = site - 0xF4), keeping sites whose object head is an rdata vtable."""
    needle = struct.pack("<Q", chat_id)
    holders = set()
    for base, size, _typ in proc.regions():
        blob = proc.read(base, size)
        if not blob:
            continue
        i = blob.find(needle)
        while i != -1:
            if i % 4 == 0:
                holders.add(base + i - 0x10)
            i = blob.find(needle, i + 1)
    rooms = {}
    sites = {struct.pack("<I", h): h for h in holders}
    for base, size, typ in proc.regions():
        if typ != 0x20000:            # private commit only
            continue
        blob = proc.read(base, size)
        if not blob:
            continue
        for site, _holder in sites.items():
            j = blob.find(site)
            while j != -1:
                if j % 4 == 0:
                    room = base + j - 0xF4
                    vt = proc.u32(room)
                    if vt is not None and RDATA_LO <= vt <= RDATA_HI:
                        rooms[room] = vt
                j = blob.find(site, j + 1)
    return rooms


# ----------------------------------------------------------------------------------------------------- template/state

def data_path(value):
    """The template and state files hold captured client bytes: absolute paths inside the data parent only."""
    from rawlayer.capture import paths, precheck
    path = Path(value)
    try:
        if not value or not path.is_absolute() \
                or not path.resolve().is_relative_to(Path(paths.DATA_PARENT).resolve()):
            raise CollectorError("unsafe_path")
        precheck.check(path.parent)
    except CollectorError:
        raise
    except Exception:
        bad("unsafe_path")
    return path


def read_template(path):
    try:
        data = load(path, None)
    except CollectorError:
        bad("bad_template")
    if not isinstance(data, dict) or data.get("v") != 1:
        bad("bad_template")
    if not isinstance(data.get("chatId"), int) or not 0 < data["chatId"] < 1 << 63:
        bad("bad_template")
    for key in ("msgId", "roomVtable", "sender_id"):
        if not isinstance(data.get(key), int) or not 0 < data[key] <= 0xFFFFFFFF:
            bad("bad_template")
    if not _HEX.match(data.get("template_hex") or "") or len(data["template_hex"]) != MSG_SIZE * 2 \
            or not _HEX.match(data.get("texthdr_hex") or "") or len(data["texthdr_hex"]) != 64:
        bad("bad_template")
    return data


def state_path(a):
    return Path(a.state) if a.state else Path(a.template).with_name("state.json")


def read_state(path):
    data = load(path, {})
    if not isinstance(data, dict) or not isinstance(data.get("msg_id_next"), int) or data["msg_id_next"] <= 0:
        return {}
    return data


def write_state(path, value):
    atomic(path.parent, path, value)


def read_message(path):
    try:
        message = Path(path).read_text(encoding="utf-8").rstrip()
    except (OSError, ValueError):
        bad("bad_message_file")
    if not message.strip():
        bad("empty_message")
    if len(message.encode("utf-8")) > TEXT_MAX_UTF8:
        bad("message_too_long")
    return message


# ---------------------------------------------------------------------------------------------------------- frida glue

def _attach(pid, source):
    try:
        import frida
    except ImportError:
        bad("frida_missing")
    session = frida.get_local_device().attach(pid)
    lines = []

    def on_message(m, _data):
        if m.get("type") == "send":
            lines.append(str(m["payload"]))
        elif m.get("type") == "error":
            lines.append("[SCRIPT-ERROR] " + str(m.get("description")))

    script = session.create_script(source)
    script.on("message", on_message)
    script.load()
    return session, script, lines


def _collect(lines, prefixes, timeout):
    """Wait until a line starting with one of `prefixes` arrives; return it and every line so far."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        for line in lines:
            if line.startswith(prefixes):
                return line, list(lines)
        time.sleep(0.5)
    return None, list(lines)


def _teardown(session, script):
    for step in (script.unload, session.detach):
        try:
            step()
        except Exception:
            pass


CALIBRATE_JS = r"""
var BUILDER = ptr(%d);
function hx(arr) {
    var s = [];
    for (var i = 0; i < arr.length; i++) s.push(('0' + arr[i].toString(16)).slice(-2));
    return s.join('');
}
var done = false;
Interceptor.attach(BUILDER, {
    onEnter: function () {
        if (done) return;
        done = true;
        try {
            var ec = this.context.ecx, esp = this.context.esp;
            var outer = esp.add(4).readPointer();
            var inner = outer.readPointer();
            var textPtr = inner.add(0x40).readPointer();
            var origLen = 0;
            while (textPtr.add(origLen).readU8() !== 0 && origLen < 512) origLen++;
            send('[CAP] ' + JSON.stringify({
                chatId: inner.add(0x18).readU32() + inner.add(0x1c).readU32() * 0x100000000,
                msgId: inner.add(0x38).readU32(),
                senderLo: inner.add(0x28).readU32(), senderHi: inner.add(0x2c).readU32(),
                roomVtable: ec.readU32(),
                room180: inner.add(0x180).readU32(),
                textUtf8: origLen,
                template_hex: hx(new Uint8Array(inner.readByteArray(0x1d0))),
                texthdr_hex: hx(new Uint8Array(textPtr.sub(16).readByteArray(32)))
            }));
        } catch (e) { send('[CAP-FAIL] ' + e); }
    }
});
send('[ARMED] send ONE message in any plain-text room now');
"""

SEND_JS = r"""
var BUILDER = ptr(%d), LOCO_GLOBAL = ptr(%d), RESHANDLER = ptr(%d);
var ROOM_VTABLE = ptr(%d), ROOM = ptr(%d);
var DRY = %d, MSG_ID = %d, TEXT = %s, CHAT_ID = uint64('%s');

// the client's own WRITE response handler: errCode 0 is the server's acceptance
var resp = null;
Interceptor.attach(RESHANDLER, {
    onEnter: function () {
        if (resp) return;
        try {
            var esp = this.context.esp;
            var err = esp.add(8).readS32();
            var msgp = esp.add(0xc).readPointer();
            var emsg = '';
            try { if (!msgp.isNull()) emsg = msgp.readCString(); } catch (e) {}
            var lo = 0;
            try { lo = this.context.ecx.add(0xf4).readPointer().add(0x10).readU32(); } catch (e2) {}
            resp = {err: err, msg: emsg.substring(0, 200), room: lo};
        } catch (e) { resp = {err: 'unreadable'}; }
    }
});

try {
    var pro = new Uint8Array(BUILDER.readByteArray(6));
    if (pro[0] !== 0x55 || pro[1] !== 0x8b || pro[2] !== 0xec) throw new Error('builder prologue mismatch');
    var lm = LOCO_GLOBAL.readPointer();
    if (lm.add(0xc0).readS32() !== 6) throw new Error('loco state != 6');
    if (!ROOM.readPointer().equals(ROOM_VTABLE)) throw new Error('room vtable mismatch');
    var holder = ROOM.add(0xf4).readPointer();
    var roomChat = holder.add(0x10).readU32() + holder.add(0x14).readU32() * 0x100000000;
    if (roomChat !== Number(CHAT_ID)) throw new Error('room chatId mismatch');

    var tpl = '%s';
    var tbytes = [];
    for (var k = 0; k < tpl.length; k += 2) tbytes.push(parseInt(tpl.substr(k, 2), 16));
    if (tbytes.length !== 0x1d0) throw new Error('template length');
    var clone = Memory.alloc(0x1d0);
    clone.writeByteArray(tbytes);
    // identity + the minimal-clone patches: every pointer the builder dereferences is replaced
    clone.add(0x18).writeU64(CHAT_ID);
    clone.add(0x38).writeU32(MSG_ID);
    clone.add(0x3c).writeU32(0);
    var tb = Memory.alloc(16 + 256);
    var hdr = '%s';
    var hbytes = [];
    for (var k = 0; k < hdr.length; k += 2) hbytes.push(parseInt(hdr.substr(k, 2), 16));
    tb.writeByteArray(hbytes);
    var ts = tb.add(16);
    ts.writeUtf8String(TEXT);
    ts.add(-8).writeU32(unescape(encodeURIComponent(TEXT)).length);
    clone.add(0x40).writePointer(ts);
    clone.add(0x6c).writeU32(0x0267D2AA);   // supplement: sentinel (builder skips append and release)
    clone.add(0x180).writeU32(10);          // no-extra path (g_str returns immediately)
    clone.add(0x17c).writeU32(0x0267D2AA);  // defensive; unread while +0x180 == 10
    // readback gate: nothing is sent unless every patch is in place
    if (clone.add(0x6c).readU32() !== 0x0267D2AA) throw new Error('readback supplement');
    if (clone.add(0x180).readU32() !== 10) throw new Error('readback extra gate');
    if (clone.add(0x38).readU32() !== MSG_ID) throw new Error('readback msgId');
    if (ts.add(-8).readU32() !== unescape(encodeURIComponent(TEXT)).length) throw new Error('readback length');
    if (ts.readUtf8String() !== TEXT) throw new Error('readback text');
    send('[READY]');

    if (DRY) { send('[DRY] no builder call'); }
    else {
        var outer = Memory.alloc(8);
        outer.writePointer(clone);
        outer.add(4).writeU32(0);
        var f = new NativeFunction(BUILDER, 'uint8', ['pointer', 'pointer', 'uint8'], 'thiscall');
        var ret = f(ROOM, outer, 0);
        send(ret === 1 ? '[OK]' : '[FAIL] builder ret=' + ret);
        setTimeout(function () {
            send('[WINDOW-END] ' + (resp ? JSON.stringify(resp) : 'no response'));
        }, %d);
    }
} catch (e) { send('[FAIL] ' + e); }
"""

LISTEN_JS = r"""
var BUILDER = ptr(%d), LOCO_GLOBAL = ptr(%d);
var PREFIX = %s, OWNER_LO = %d;
var MAXPAYLOAD = %d, FRESH_MS = 1200;
var lastSend = 0, inSend = false, myMsgIds = [];

function utf8len(s) { return unescape(encodeURIComponent(s)).length; }

function doSend(cap, payload) {
    try {
        if (Date.now() - cap.t > FRESH_MS) { send('[STALE] aborted'); return; }
        if (Date.now() - lastSend < 800) { setTimeout(function () { doSend(cap, payload); }, 150); return; }
        var lm = LOCO_GLOBAL.readPointer();
        if (lm.add(0xc0).readS32() !== 6) throw new Error('loco state != 6');
        var clone = Memory.alloc(0x1d0);
        var dv = new DataView(cap.arr.buffer);
        for (var o = 0; o < 0x1d0; o += 4) clone.add(o).writeU32(dv.getUint32(o, true));
        clone.add(0x38).writeU32(cap.msgId + 0x200);
        clone.add(0x3c).writeU32(0);
        var tb = Memory.alloc(16 + 256);
        var hdv = new DataView(cap.hdr.buffer);
        for (var h = 0; h < 32; h += 4) tb.add(h).writeU32(hdv.getUint32(h, true));
        var ts = tb.add(16);
        ts.writeUtf8String(payload);
        ts.add(-8).writeU32(utf8len(payload));
        clone.add(0x40).writePointer(ts);
        var outer = Memory.alloc(8);
        outer.writePointer(clone);
        outer.add(4).writeU32(0);
        var f = new NativeFunction(BUILDER, 'uint8', ['pointer', 'pointer', 'uint8'], 'thiscall');
        var mid = cap.msgId + 0x200;
        myMsgIds.push(mid); if (myMsgIds.length > 8) myMsgIds.shift();
        inSend = true;
        var ret;
        try { ret = f(cap.room, outer, 0); } finally { inSend = false; }
        lastSend = Date.now();
        send(ret === 1 ? '[OK] msgId=0x' + mid.toString(16) : '[FAIL] builder ret=' + ret);
    } catch (e) { send('[FAIL] ' + e); }
}

Interceptor.attach(BUILDER, {
    onEnter: function () {
        if (inSend) return;
        try {
            var ec = this.context.ecx, esp = this.context.esp;
            var inner = esp.add(4).readPointer().readPointer();
            var mid = inner.add(0x38).readU32();
            if (myMsgIds.indexOf(mid) !== -1) return;
            if (inner.add(0x28).readU32() !== OWNER_LO || inner.add(0x2c).readU32() !== 0) return;
            var tp = inner.add(0x40).readPointer();
            var slen = tp.add(-8).readU32();
            var txt = tp.readUtf8String(slen > 1024 ? 1024 : slen);
            if (txt === null || txt.indexOf(PREFIX) !== 0) return;
            var payload = txt.slice(PREFIX.length);
            if (payload.length === 0) { send('[SKIP] empty command'); return; }
            if (utf8len(payload) > MAXPAYLOAD) { send('[SKIP] payload too long'); return; }
            var cap = {
                room: ec, msgId: mid, t: Date.now(),
                arr: new Uint8Array(inner.readByteArray(0x1d0)),
                hdr: new Uint8Array(tp.sub(16).readByteArray(32))
            };
            send('[CMD] ' + payload);
            setTimeout(function () { doSend(cap, payload); }, 300);
        } catch (e) { send('[FAIL-HOOK] ' + e); }
    }
});
send('[ARMED] listener armed');
"""


# -------------------------------------------------------------------------------------------------------- commands

def cmd_check(a):
    pid, proc = guards()
    tpl = read_template(a.template)
    state = read_state(state_path(a))
    headroom = state.get("msg_id_next", 0) - tpl["msgId"]
    return {"status": "ok", "pid": pid, "version": VERSION, "loco": "logined", "builder": "ok",
            "template": {"chatId": hex(tpl["chatId"]), "msgId": hex(tpl["msgId"]),
                         "roomVtable": hex(tpl["roomVtable"]), "sender": "set"},
            "state": {"msg_id_next": hex(state["msg_id_next"]) if state else None,
                      "headroom": headroom if state else None}}, 0


def cmd_calibrate(a):
    pid, _proc = guards()
    session, script, lines = _attach(pid, CALIBRATE_JS % BUILDER)
    try:
        line, _ = _collect(lines, ("[CAP]", "[CAP-FAIL]"), CALIBRATE_WAIT + 10)
    finally:
        _teardown(session, script)
    if not line or not line.startswith("[CAP] "):
        bad("no_capture" if not line else "capture_failed")
    try:
        cap = json.loads(line[6:])
    except ValueError:
        bad("capture_failed")
    if cap.get("senderHi") != 0 or not 0 < cap.get("msgId", 0) <= 0xFFFFFFFF \
            or not RDATA_LO <= cap.get("roomVtable", 0) <= RDATA_HI \
            or not _HEX.match(cap.get("template_hex") or "") or len(cap["template_hex"]) != MSG_SIZE * 2 \
            or not _HEX.match(cap.get("texthdr_hex") or "") or len(cap["texthdr_hex"]) != 64:
        bad("capture_failed")
    template = {"v": 1, "captured_at": now(), "chatId": cap["chatId"], "msgId": cap["msgId"],
                "roomVtable": cap["roomVtable"], "sender_id": cap["senderLo"],
                "template_hex": cap["template_hex"], "texthdr_hex": cap["texthdr_hex"]}
    atomic(Path(a.template).parent, Path(a.template), template)
    spath = state_path(a)
    state = read_state(spath)
    state["msg_id_next"] = max(state.get("msg_id_next", 0), cap["msgId"] + 0x200)
    write_state(spath, state)
    return {"status": "ok", "chatId": hex(cap["chatId"]), "msgId": hex(cap["msgId"]),
            "senderId": hex(cap["senderLo"]), "room_kind": hex(cap.get("room180", 0)),
            "text_utf8_bytes": cap.get("textUtf8")}, 0


def _chat_id(value):
    try:
        chat_id = int(value, 0)
    except ValueError:
        bad("bad_chat_id")
    if not 0 < chat_id < 1 << 63:
        bad("bad_chat_id")
    return chat_id


def cmd_rooms(a):
    _pid, proc = guards()
    tpl = read_template(a.template)
    rooms = find_rooms(proc, _chat_id(a.chat_id))
    items = [{"room": hex(r), "vtable": hex(v), "matches_template": v == tpl["roomVtable"]}
             for r, v in sorted(rooms.items())]
    return {"status": "ok", "rooms": len(items),
            "matching": sum(i["matches_template"] for i in items), "items": items[:50]}, 0


def cmd_send(a):
    pid, proc = guards()
    tpl = read_template(a.template)
    message = read_message(a.message_file)
    chat_id = _chat_id(a.chat_id)
    rooms = find_rooms(proc, chat_id)
    matching = [r for r, v in rooms.items() if v == tpl["roomVtable"]]
    if not matching:
        bad("no_room")
    if len(matching) > 1:
        bad("ambiguous_room")
    spath = state_path(a)
    state = read_state(spath)
    msg_id = state.get("msg_id_next") or (tpl["msgId"] + 0x200)
    if not a.dry_run:                                   # at-most-once: persist before the client is called
        state["msg_id_next"] = msg_id + 1
        write_state(spath, state)
    source = SEND_JS % (BUILDER, LOCO_GLOBAL, RESHANDLER, tpl["roomVtable"], matching[0],
                        1 if a.dry_run else 0, msg_id, json.dumps(message), "0x%X" % chat_id,
                        tpl["template_hex"], tpl["texthdr_hex"], RESPONSE_WAIT * 1000)
    session, script, lines = _attach(pid, source)
    try:
        line, lines = _collect(lines, ("[WINDOW-END]", "[DRY]", "[FAIL]"), RESPONSE_WAIT + 25)
    finally:
        _teardown(session, script)
    failed = next((l for l in lines if l.startswith("[FAIL]")), None)
    if failed or not any(l.startswith(("[OK]", "[DRY]")) for l in lines):
        return {"status": "refused", "detail": (failed or "no result")[:200], "msgId": hex(msg_id)}, 2
    if a.dry_run:
        return {"status": "dry_run", "msgId": hex(msg_id), "utf8": len(message.encode("utf-8"))}, 0
    err_code, err_msg = None, None
    if line and line.startswith("[WINDOW-END] "):
        try:
            resp = json.loads(line[13:])
            err_code, err_msg = resp.get("err"), resp.get("msg")
        except ValueError:
            pass
    body = {"status": "sent", "msgId": hex(msg_id), "utf8": len(message.encode("utf-8")),
            "err_code": err_code}
    if err_msg:
        body["err_msg"] = err_msg
    if err_code not in (None, 0):                       # the write happened; the server refused it
        body["status"] = "server_rejected"
        return body, 1
    return body, 0


def cmd_listen(a):
    pid, _proc = guards()
    tpl = read_template(a.template)
    if not 1 <= len(a.prefix) <= 16:
        bad("bad_prefix")
    source = LISTEN_JS % (BUILDER, LOCO_GLOBAL, json.dumps(a.prefix), tpl["sender_id"], TEXT_MAX_UTF8)
    session, script, lines = _attach(pid, source)
    commands = 0
    try:
        deadline = time.time() + a.lifetime if a.lifetime > 0 else None
        seen = 0
        while True:
            time.sleep(1)
            for line in lines[seen:]:
                print(json.dumps({"app": "kakaotalk", "command": "listen", "event": line[:200]}), flush=True)
                if line.startswith("[CMD]"):
                    commands += 1
            seen = len(lines)
            if deadline and time.time() >= deadline:
                break
    except KeyboardInterrupt:
        pass
    finally:
        _teardown(session, script)                      # one unload, one detach — never kill the process
    return {"status": "stopped", "prefix": a.prefix, "commands": commands,
            "stopped_by": "lifetime" if a.lifetime > 0 else "interrupt"}, 0


COMMANDS = {"check": cmd_check, "calibrate": cmd_calibrate, "rooms": cmd_rooms,
            "send": cmd_send, "listen": cmd_listen}


def main(argv=None):
    p = argparse.ArgumentParser(prog="kakaotalk.admin", description="KakaoTalk administration (see the module doc)")
    sub = p.add_subparsers(dest="command", required=True)
    for name in COMMANDS:
        s = sub.add_parser(name)
        s.add_argument("--template", required=True,
                       help="calibration template, an absolute path inside the data parent")
        s.add_argument("--state", help="message-id state file (default: beside the template)")
    for name in ("rooms", "send"):
        sub.choices[name].add_argument("--chat-id", required=True, help="room chat id, e.g. 0x1234567890")
    s = sub.choices["send"]
    s.add_argument("--message-file", required=True, help="file holding the text to send (as admin post)")
    s.add_argument("--dry-run", action="store_true", help="patch and verify the clone, call nothing")
    s = sub.choices["listen"]
    s.add_argument("--prefix", default="!!", help="command prefix the listener reacts to")
    s.add_argument("--lifetime", type=int, default=0, help="seconds to stay resident (0: until Ctrl+C)")
    a = p.parse_args(sys.argv[1:] if argv is None else argv)
    try:
        a.template = data_path(a.template)   # inside the try: an unsafe path is JSON + exit 2, never a traceback
        if a.state:
            a.state = data_path(a.state)
        body, code = COMMANDS[a.command](a)
    except CollectorError as e:
        body, code = {"status": e.category}, 2
    except Exception:
        body, code = {"status": "error", "error_type": type(sys.exc_info()[1]).__name__}, 2
    print(json.dumps({"app": "kakaotalk", "command": a.command, **body}))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
