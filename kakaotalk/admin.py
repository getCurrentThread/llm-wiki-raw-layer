# -*- coding: utf-8 -*-
"""KakaoTalk administration: message sending through the running Windows client's own Loco WRITE path, driven with
frida (the client keeps its session, TLS and sync; no credential or key material is read, stored or printed). Every
command posts only as the owner's own signed-in account on this machine, one message at a time.

  check | calibrate | rooms | send | listen      (kakaotalk/README.md, "Administration", documents each one)

Exit codes: 0 ok | 1 a write happened but did not succeed (server_rejected, unconfirmed, failed_after_write) |
2 refused before any write."""
import argparse
import contextlib
import ctypes
import json
import os
from pathlib import Path
import re
import struct
import subprocess
import sys
import time

from appkit.archive import FetchError, atomic, load, locked, now

VERSION_SPECS = {
    # One row per supported client FileVersion: every address, byte pattern and struct offset the scans and
    # the injected JS rely on, spliced into the templates by _render() — the raw JS holds no addresses or
    # offsets. A client that auto-updated past this table refuses version_mismatch.
    "26.9.1.5339": {
        # addresses (x86, image base 0x400000, Themida)
        "builder": 0x017F9FF0,               # TalkChatRoom WRITE packet builder (thiscall: room, msgOuter, 0)
        # prologue patterns: space-separated hex bytes, '??' a wildcard byte — a function-specific signature
        # is long enough (>= 32 bytes) to be unique; today's entries hold only the generic MSVC SEH prologue
        # (55 8b ec 6a ff 68 opens nearly every EH function) until one gated read fills the real bytes
        "builder_prologue": "55 8b ec 6a ff 68",
        "reshandler": 0x018046B0,            # TalkChatRoom::LocoCommand_WRITE_ResultHandler_v3
        "reshandler_prologue": None,         # not recorded offline: while None this check is skipped
        "loco_global": 0x02AF1008,           # *(&) = LocoManager
        "loco_state_off": 0xC0,              # LocoManager + this
        "loco_logged_in": 6,                 # ... == this means logged in
        "sentinel": 0x0267D2AA,              # the client's empty-slot value for optional string members
        "rdata_lo": 0x01F00000,              # image .rdata range a room vtable must sit in
        "rdata_hi": 0x02720000,
        "msg_size": 0x1D0,                   # the message object the builder reads
        "plain_no_extra": 10,                # the message-type gate value of a plain-text send (no "extra")
        # the two hooked functions' stack layouts, and the outer pair handed to the builder
        "builder_msgouter_off": 0x4,         # esp+this at the builder's entry: the msgOuter pointer
        "resh_err_off": 0x8,                 # esp+this at the WRITE result handler's entry: the errCode
        "resh_msg_off": 0xC,                 # esp+this: the error-message pointer
        "msgouter_size": 8,                  # the outer pair: {inner pointer, high word 0}
        "msgouter_hi_off": 0x4,
        # message-object offsets
        "off_chat_id": 0x18,                 # 64-bit chatId
        "off_sender_lo": 0x28,               # sender id, low and high word
        "off_sender_hi": 0x2C,
        "off_msg_id": 0x38,                  # msgId, and the word after it (zeroed in a clone)
        "off_msg_pad": 0x3C,
        "off_text": 0x40,                    # text pointer
        "off_supplement": 0x6C,              # supplement pointer -> sentinel
        "off_extra": 0x17C,                  # extra-string pointer -> sentinel (unread on the plain path)
        "off_extra_gate": 0x180,             # type gate: == plain_no_extra for plain text
        "off_room_holder": 0xF4,             # TalkChatRoom -> its chat-id holder pointer
        "off_holder_chat": 0x10,             # holder -> 64-bit chatId
        # the text header layout around a text pointer
        "text_hdr_pre": 16,                  # header bytes before the text
        "text_hdr_bytes": 32,                # the capture: the header plus the text's first bytes
        "text_len_off": -8,                  # the utf-8 length field, relative to the text
        "text_block": 272,                   # a clone's own text block: header + room for the max text
    },
}
TEXT_MAX_UTF8 = 200
CALIBRATE_WAIT = 180                     # seconds the owner has to send the calibration message
RESPONSE_WAIT = 12                       # seconds a send waits for the client's WRITE response

_HEX = re.compile(r"[0-9a-f]+\Z")
k32 = ctypes.windll.kernel32


def bad(category):
    raise FetchError(category)


# ---------------------------------------------------------------------------------------------------------------- client

def _powershell(command):
    try:
        return subprocess.check_output(["powershell", "-NoProfile", "-Command", command], timeout=30).strip()
    except Exception:
        bad("client_not_running" if "ErrorAction Stop" in command else "version_unreadable")


def client_pid():
    if os.name != "nt":
        bad("windows_only")
    ids = _powershell("(Get-Process -Name KakaoTalk -ErrorAction Stop).Id").split()
    if len(ids) > 1:
        bad("ambiguous_client")            # two clients: which one is signed in is not ours to guess
    if len(ids) != 1:
        bad("client_not_running")
    try:
        return int(ids[0])
    except ValueError:
        bad("client_not_running")


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
        while addr < 0xFFFF0000:        # x86 + LARGE_ADDRESS_AWARE: user space reaches 4 GB on 64-bit Windows
            if k32.VirtualQueryEx(self.h, ctypes.c_void_p(addr), ctypes.byref(mbi), ctypes.sizeof(mbi)) == 0:
                break
            base = mbi.BaseAddress or 0
            if mbi.State == 0x1000 and mbi.Protect in (2, 4, 8, 0x20, 0x40) and mbi.RegionSize:
                yield base, mbi.RegionSize, mbi.Type
            addr = base + mbi.RegionSize


def spec_for(version):
    """The version table's row for a client FileVersion — every address and offset this run uses — or
    FetchError("version_mismatch"): the client auto-updates past this table."""
    spec = VERSION_SPECS.get(version)
    if spec is None:
        bad("version_mismatch")
    return spec


def _pattern_len(pattern):
    """A prologue pattern's length in bytes: space-separated hex or '??' (wildcard) tokens."""
    return len(pattern.split())


def _pattern_match(blob, pattern):
    """True when `blob` matches the pattern: '??' tokens match any byte, every other token must equal."""
    for i, token in enumerate(pattern.split()):
        if token != "??" and (i >= len(blob) or blob[i] != int(token, 16)):
            return False
    return True


def guards():
    """Everything read-only that must hold before the client is touched at all."""
    pid = client_pid()
    spec = spec_for(client_version())      # an unknown FileVersion refuses here
    proc = Proc(pid)
    lm = proc.u32(spec["loco_global"])
    if not lm or proc.u32(lm + spec["loco_state_off"]) != spec["loco_logged_in"]:
        bad("not_logged_in")
    pro = proc.read(spec["builder"], _pattern_len(spec["builder_prologue"]))
    if not pro or not _pattern_match(pro, spec["builder_prologue"]):
        bad("builder_changed")
    if spec["reshandler_prologue"]:
        pro = proc.read(spec["reshandler"], _pattern_len(spec["reshandler_prologue"]))
        if not pro or not _pattern_match(pro, spec["reshandler_prologue"]):
            bad("reshandler_changed")
    return pid, proc, spec


def find_rooms(proc, chat_id, spec):
    """{room address: vtable} for the TalkChatRoom objects holding chatId: scan the id, then its holders'
    pointer sites (room = site - off_room_holder), keeping sites whose object head is an rdata vtable."""
    needle = struct.pack("<Q", chat_id)
    holders = set()
    for base, size, _typ in proc.regions():
        blob = proc.read(base, size)
        if not blob:
            continue
        i = blob.find(needle)
        while i != -1:
            if i % 4 == 0:
                holders.add(base + i - spec["off_holder_chat"])
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
                    room = base + j - spec["off_room_holder"]
                    vt = proc.u32(room)
                    if vt is not None and spec["rdata_lo"] <= vt <= spec["rdata_hi"]:
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
            raise FetchError("unsafe_path")
        precheck.check(path.parent)
    except FetchError:
        raise
    except Exception:
        bad("unsafe_path")
    return path


def read_template(path, spec):
    try:
        data = load(path, None)
    except FetchError:
        bad("bad_template")
    if not isinstance(data, dict) or data.get("v") != 1:
        bad("bad_template")
    if not isinstance(data.get("chatId"), int) or not 0 < data["chatId"] < 1 << 63:
        bad("bad_template")
    for key in ("msgId", "roomVtable", "sender_id"):
        if not isinstance(data.get(key), int) or not 0 < data[key] <= 0xFFFFFFFF:
            bad("bad_template")
    if not _HEX.match(data.get("template_hex") or "") or len(data["template_hex"]) != spec["msg_size"] * 2 \
            or not _HEX.match(data.get("texthdr_hex") or "") \
            or len(data["texthdr_hex"]) != spec["text_hdr_bytes"] * 2:
        bad("bad_template")
    return data


def state_path(a):
    return Path(a.state) if a.state else Path(a.template).with_name("state.json")


def read_state(path):
    try:
        data = load(path, {})
    except (FetchError, OSError, ValueError):        # unreadable or not JSON: an invalid state
        return {}
    if not isinstance(data, dict) or not isinstance(data.get("msg_id_next"), int) \
            or isinstance(data.get("msg_id_next"), bool) or data["msg_id_next"] <= 0:
        return {}
    live = data.get("live_msg_id_max")               # the newest live msgId seen: optional, dropped when
    if not isinstance(live, int) or isinstance(live, bool) or live < 0:   # not a plain non-negative int
        data.pop("live_msg_id_max", None)            # (the counter alone then decides the taken ids)
    return data


def _next_msg_id(state):
    """The id a send takes: the state counter, but never at or below the newest live msgId the listener
    has seen — the client's own counter could otherwise catch up with ours and collide."""
    return max(state["msg_id_next"], state.get("live_msg_id_max", 0) + 1)


def write_state(path, value):
    atomic(path.parent, path, value)


@contextlib.contextmanager
def state_lock(folder):
    """The state counter's lock (appkit.archive.locked on the state folder): one msgId taker at a time, the
    lock held from the read through the persist to the client call. A second taker refuses state_busy."""
    try:
        with locked(folder):
            yield
    except FetchError as e:
        if e.category == "collector_busy":
            bad("state_busy")
        raise


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
    """Attach to the client and load `source`. A failure while attaching or creating the script raises
    frida_attach_failed (FetchError: nothing ran, so every caller reads it as a refusal); only load() runs
    the script — up to and including the client's own send call — so its failures stay generic exceptions
    for the caller to classify (send: failed_after_write)."""
    try:
        import frida
    except ImportError:
        bad("frida_missing")
    session = None
    try:
        session = frida.get_local_device().attach(pid)
        script = session.create_script(source)
    except Exception:
        if session is not None:                  # a half-open session is closed, never left behind
            with contextlib.suppress(Exception):
                session.detach()
        bad("frida_attach_failed")
    lines, detached = [], []

    def on_message(m, _data):
        if m.get("type") == "send":
            lines.append(str(m["payload"]))
        elif m.get("type") == "error":
            lines.append("[SCRIPT-ERROR] " + str(m.get("description")))

    session.on("detached", lambda *a: detached.append(a[0] if a else "detached"))  # the client is gone: stop waiting
    script.on("message", on_message)
    script.load()
    return session, script, lines, detached


def _collect(lines, prefixes, timeout, stop=None):
    """Wait until a line starting with one of `prefixes` arrives; return it and every line so far. A stop()
    that turns true (the frida session detached) ends the wait with whatever lines there are."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        for line in lines:
            if line.startswith(prefixes):
                return line, list(lines)
        if stop is not None and stop():
            break
        time.sleep(0.5)
    return None, list(lines)


def _teardown(session, script):
    for step in (script.unload, session.detach):
        try:
            step()
        except Exception:
            pass


def _render(tpl, spec, **extra):
    """A JS template with the version table's row (plus per-call values) spliced in: the raw templates hold
    only %(name)s slots — every address, offset and byte pattern comes from the table."""
    return tpl % {"prologue_len": _pattern_len(spec["builder_prologue"]), **spec, **extra}


CALIBRATE_JS = r"""
var BUILDER = ptr(%(builder)#x), MSG_SIZE = %(msg_size)d;
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
            var outer = esp.add(%(builder_msgouter_off)#x).readPointer();
            var inner = outer.readPointer();
            var textPtr = inner.add(%(off_text)#x).readPointer();
            var origLen = 0;
            while (textPtr.add(origLen).readU8() !== 0 && origLen < 512) origLen++;
            send('[CAP] ' + JSON.stringify({
                chatId: inner.add(%(off_chat_id)#x).readU64().toString(),   // exact 64-bit: a JS double would round it
                msgId: inner.add(%(off_msg_id)#x).readU32(),
                senderLo: inner.add(%(off_sender_lo)#x).readU32(), senderHi: inner.add(%(off_sender_hi)#x).readU32(),
                roomVtable: ec.readU32(),
                room180: inner.add(%(off_extra_gate)#x).readU32(),
                textUtf8: origLen,
                template_hex: hx(new Uint8Array(inner.readByteArray(MSG_SIZE))),
                texthdr_hex: hx(new Uint8Array(textPtr.sub(%(text_hdr_pre)d).readByteArray(%(text_hdr_bytes)d)))
            }));
        } catch (e) { send('[CAP-FAIL] ' + e); }
    }
});
send('[ARMED] send ONE message in any plain-text room now');
"""

_CLONE_JS = r"""
// shared by send and listen: every message is a minimal clone whose every pointer the builder could
// dereference is replaced, gated by readback before anything is sent
var MSG_SIZE = %(msg_size)d, SENTINEL = %(sentinel)#x, PLAIN = %(plain_no_extra)d;
// every allocation handed to the client stays reachable for the script's remaining lifetime: Frida frees a
// Memory.alloc block once no JS handle reaches it, and a derived pointer (the text pointer the clone holds)
// keeps nothing alive. Bounded, so a long-lived listener drops only blocks well older than their sends.
var RETAIN = [];
function utf8len(s) { return unescape(encodeURIComponent(s)).length; }
function hx(arr) {
    var s = [];
    for (var i = 0; i < arr.length; i++) s.push(('0' + arr[i].toString(16)).slice(-2));
    return s.join('');
}
function hexBytes(t) {
    var a = [];
    for (var k = 0; k < t.length; k += 2) a.push(parseInt(t.substr(k, 2), 16));
    return a;
}
function retain() {
    for (var i = 0; i < arguments.length; i++) RETAIN.push(arguments[i]);
    if (RETAIN.length > 64) RETAIN.splice(0, RETAIN.length - 64);
}
function buildClone(baseHex, hdrHex, chatId, msgId, text) {
    var base = hexBytes(baseHex);
    if (base.length !== MSG_SIZE) throw new Error('template length');
    var clone = Memory.alloc(MSG_SIZE);
    clone.writeByteArray(base);
    clone.add(%(off_chat_id)#x).writeU64(chatId);       // identity
    clone.add(%(off_msg_id)#x).writeU32(msgId);
    clone.add(%(off_msg_pad)#x).writeU32(0);
    var tb = Memory.alloc(%(text_block)d);
    tb.writeByteArray(hexBytes(hdrHex));
    var ts = tb.add(%(text_hdr_pre)d);
    ts.writeUtf8String(text);
    ts.add(%(text_len_off)#x).writeU32(utf8len(text));
    clone.add(%(off_text)#x).writePointer(ts);
    clone.add(%(off_supplement)#x).writeU32(SENTINEL);  // supplement: sentinel (builder skips append and release)
    clone.add(%(off_extra_gate)#x).writeU32(PLAIN);     // no-extra path (g_str returns immediately)
    clone.add(%(off_extra)#x).writeU32(SENTINEL);       // defensive; unread while the gate says plain
    // readback gate: nothing is sent unless every patch is in place
    if (clone.add(%(off_supplement)#x).readU32() !== SENTINEL) throw new Error('readback supplement');
    if (clone.add(%(off_extra_gate)#x).readU32() !== PLAIN) throw new Error('readback extra gate');
    if (clone.add(%(off_msg_id)#x).readU32() !== msgId) throw new Error('readback msgId');
    if (ts.add(%(text_len_off)#x).readU32() !== utf8len(text)) throw new Error('readback length');
    if (ts.readUtf8String() !== text) throw new Error('readback text');
    retain(clone, tb);                                  // outlives this call: the client reads them
    return clone;
}
"""

SEND_JS = _CLONE_JS + r"""
var BUILDER = ptr(%(builder)#x), LOCO_GLOBAL = ptr(%(loco_global)#x), RESHANDLER = ptr(%(reshandler)#x);
var ROOM_VTABLE = ptr(%(room_vtable)#x), ROOM = ptr(%(room)#x);
var DRY = %(dry)d, MSG_ID = %(msg_id)d, TEXT = %(text)s, CHAT_ID = uint64('%(chat_id)s');

// the client's own WRITE response handler: errCode 0 is the server's acceptance, and only the target
// room's response is attributable — its full chat id, exact as a decimal string. Another room's, or an
// unreadable one, is tallied and never recorded, so it cannot hide the target's own response.
var resp = null, others = 0, unreadable = 0;
Interceptor.attach(RESHANDLER, {
    onEnter: function () {
        if (resp) return;
        try {
            var esp = this.context.esp;
            var err = esp.add(%(resh_err_off)#x).readS32();
            var msgp = esp.add(%(resh_msg_off)#x).readPointer();
            var emsg = '';
            try { if (!msgp.isNull()) emsg = msgp.readCString(); } catch (e) {}
            var room = '';
            try { room = this.context.ecx.add(%(off_room_holder)#x).readPointer().add(%(off_holder_chat)#x).readU64().toString(); } catch (e2) {}
            if (room === CHAT_ID.toString()) {
                resp = {err: err, msg: emsg.substring(0, 200), room: room};
                send('[RESP] ' + JSON.stringify(resp));   // immediate: the wait can end here
            } else if (room) others += 1;
            else unreadable += 1;
        } catch (e) { unreadable += 1; }
    }
});

try {
    var pro = hx(new Uint8Array(BUILDER.readByteArray(%(prologue_len)d)));
    var pat = '%(builder_prologue)s'.split(' ');
    for (var pi = 0; pi < pat.length; pi++) {          // '??' tokens match any byte
        if (pat[pi] !== '??' && pro.substr(pi * 2, 2) !== pat[pi]) throw new Error('builder prologue mismatch');
    }
    var lm = LOCO_GLOBAL.readPointer();
    if (lm.add(%(loco_state_off)#x).readS32() !== %(loco_logged_in)d) throw new Error('loco state != %(loco_logged_in)d');
    if (!ROOM.readPointer().equals(ROOM_VTABLE)) throw new Error('room vtable mismatch');
    var holder = ROOM.add(%(off_room_holder)#x).readPointer();
    if (!holder.add(%(off_holder_chat)#x).readU64().equals(CHAT_ID)) throw new Error('room chatId mismatch');   // exact 64-bit

    var clone = buildClone('%(template_hex)s', '%(texthdr_hex)s', CHAT_ID, MSG_ID, TEXT);
    send('[READY]');

    if (DRY) { send('[DRY] no builder call'); }
    else {
        var outer = Memory.alloc(%(msgouter_size)d);
        retain(outer);
        outer.writePointer(clone);
        outer.add(%(msgouter_hi_off)#x).writeU32(0);
        var f = new NativeFunction(BUILDER, 'uint8', ['pointer', 'pointer', 'uint8'], 'thiscall');
        send('[CALLED]');                   // from here on a failure may follow a write: never "refused"
        var ret = f(ROOM, outer, 0);
        send(ret === 1 ? '[OK]' : '[FAIL] builder ret=' + ret);
        setTimeout(function () {            // the maximum: a [RESP] above usually ends the wait sooner
            send('[WINDOW-END] ' + (resp ? JSON.stringify(resp) :
                (unreadable ? 'unreadable response' :
                    (others ? 'other room responses: ' + others : 'no response'))));
        }, %(window_ms)d);
    }
} catch (e) { send('[FAIL] ' + e); }
"""

LISTEN_JS = _CLONE_JS + r"""
var BUILDER = ptr(%(builder)#x), LOCO_GLOBAL = ptr(%(loco_global)#x);
var PREFIX = %(prefix)s, OWNER_LO = %(owner_lo)d;
var MAXPAYLOAD = %(maxpayload)d, FRESH_MS = 2000;
var lastSend = 0, inSend = false, myMsgIds = [], queue = [], reqSeq = 0, liveMax = 0;

function doSend(cap, payload) {
    try {
        if (Date.now() - cap.t > FRESH_MS) { send('[STALE] aborted'); return; }
        if (Date.now() - lastSend < 800) { setTimeout(function () { doSend(cap, payload); }, 150); return; }
        var lm = LOCO_GLOBAL.readPointer();
        if (lm.add(%(loco_state_off)#x).readS32() !== %(loco_logged_in)d) throw new Error('loco state != %(loco_logged_in)d');
        reqSeq += 1;                        // the msgId is not chosen here: Python takes it from the same
        queue.push({seq: reqSeq, cap: cap, payload: payload});   // state counter send uses, and posts it back
        send('[TAKE-ID] ' + reqSeq);
    } catch (e) { send('[FAIL] ' + e); }
}

// the id Python persisted for this call: build the gated clone (the shared builder) and call the builder
recv('id', function onId(msg) {
    recv('id', onId);
    var i;
    for (i = 0; i < queue.length; i++) if (queue[i].seq === msg.seq) break;
    if (i === queue.length) return;
    var job = queue.splice(i, 1)[0];
    try {
        if (msg.error) { send('[FAIL] ' + msg.error); }
        else if (Date.now() - job.cap.t > FRESH_MS) { send('[STALE] aborted'); }
        else {
            var clone = buildClone(job.cap.tpl, job.cap.hdr, job.cap.chatId, msg.msgId, job.payload);
            var outer = Memory.alloc(%(msgouter_size)d);
            retain(outer);
            outer.writePointer(clone);
            outer.add(%(msgouter_hi_off)#x).writeU32(0);
            var f = new NativeFunction(BUILDER, 'uint8', ['pointer', 'pointer', 'uint8'], 'thiscall');
            myMsgIds.push(msg.msgId); if (myMsgIds.length > 8) myMsgIds.shift();
            inSend = true;
            var ret;
            try { ret = f(job.cap.room, outer, 0); } finally { inSend = false; }
            lastSend = Date.now();
            send(ret === 1 ? '[OK] msgId=0x' + msg.msgId.toString(16) : '[FAIL] builder ret=' + ret);
        }
    } catch (e) { send('[FAIL] ' + e); }
    send('[ID-DONE] ' + msg.seq);           // Python releases the state lock here
});

Interceptor.attach(BUILDER, {
    onEnter: function () {
        if (inSend) return;
        try {
            var ec = this.context.ecx, esp = this.context.esp;
            var inner = esp.add(%(builder_msgouter_off)#x).readPointer().readPointer();
            var mid = inner.add(%(off_msg_id)#x).readU32();
            if (myMsgIds.indexOf(mid) !== -1) return;
            if (inner.add(%(off_sender_lo)#x).readU32() !== OWNER_LO || inner.add(%(off_sender_hi)#x).readU32() !== 0) return;
            if (mid > liveMax) { liveMax = mid; send('[LIVE-ID] ' + mid); }   // the client's own counter: Python
            // folds the maximum into the state so every taken id stays ahead of it
            if (inner.add(%(off_extra_gate)#x).readU32() !== PLAIN) { send('[SKIP] not plain'); return; }
            var tp = inner.add(%(off_text)#x).readPointer();
            var slen = tp.add(%(text_len_off)#x).readU32();
            var txt = tp.readUtf8String(slen > 1024 ? 1024 : slen);
            if (txt === null || txt.indexOf(PREFIX) !== 0) return;
            var payload = txt.slice(PREFIX.length);
            if (payload.length === 0) { send('[SKIP] empty command'); return; }
            if (utf8len(payload) > MAXPAYLOAD) { send('[SKIP] payload too long'); return; }
            var cap = {
                room: ec, t: Date.now(),
                chatId: inner.add(%(off_chat_id)#x).readU64(),
                tpl: hx(new Uint8Array(inner.readByteArray(MSG_SIZE))),
                hdr: hx(new Uint8Array(tp.sub(%(text_hdr_pre)d).readByteArray(%(text_hdr_bytes)d)))
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
    pid, proc, spec = guards()
    try:
        tpl = read_template(a.template, spec)
    except FetchError as e:
        if e.category != "bad_template":     # a template not there yet: check still reports the client
            raise
        tpl = None
    state = read_state(state_path(a))
    known = ([tpl["msgId"]] if tpl else []) + ([state["live_msg_id_max"]] if "live_msg_id_max" in state else [])
    return {"status": "ok", "pid": pid, "version": client_version(),
            "supported_versions": sorted(VERSION_SPECS), "loco": "logined", "builder": "ok",
            "template": {"chatId": hex(tpl["chatId"]), "msgId": hex(tpl["msgId"]),
                         "roomVtable": hex(tpl["roomVtable"]), "sender": "set"} if tpl else "missing",
            "state": {"msg_id_next": hex(state["msg_id_next"]) if state else None,
                      "live_msg_id_max": hex(state["live_msg_id_max"]) if "live_msg_id_max" in state else None,
                      "headroom": state["msg_id_next"] - max(known)
                      if state and known else None}}, 0    # against the newest id anything has seen


def _validated(cap, spec):
    """The capture's chatId as an exact int (the JS sends a decimal string: a JS double would round ids
    above 2^53), or the refusal: not_plain_room when the owner's calibration message was not a plain-text
    send, capture_failed when any field is missing or out of shape."""
    if cap.get("room180") != spec["plain_no_extra"]:
        bad("not_plain_room")
    try:
        chat_id = int(cap["chatId"])
    except (KeyError, TypeError, ValueError):
        bad("capture_failed")
    if not 0 < chat_id < 1 << 63 or cap.get("senderHi") != 0 \
            or not 0 < cap.get("msgId", 0) <= 0xFFFFFFFF \
            or not spec["rdata_lo"] <= cap.get("roomVtable", 0) <= spec["rdata_hi"] \
            or not _HEX.match(cap.get("template_hex") or "") or len(cap["template_hex"]) != spec["msg_size"] * 2 \
            or not _HEX.match(cap.get("texthdr_hex") or "") \
            or len(cap["texthdr_hex"]) != spec["text_hdr_bytes"] * 2:
        bad("capture_failed")
    return chat_id


def cmd_calibrate(a):
    pid, _proc, spec = guards()
    session, script, lines, detached = _attach(pid, _render(CALIBRATE_JS, spec))
    try:
        line, _ = _collect(lines, ("[CAP]", "[CAP-FAIL]", "[SCRIPT-ERROR]"), CALIBRATE_WAIT + 10,
                           stop=lambda: bool(detached))
    finally:
        _teardown(session, script)
    if not line or not line.startswith("[CAP] "):
        bad("no_capture" if not line else "capture_failed")
    try:
        cap = json.loads(line[6:])
    except ValueError:
        bad("capture_failed")
    chat_id = _validated(cap, spec)
    template = {"v": 1, "captured_at": now(), "chatId": chat_id, "msgId": cap["msgId"],
                "roomVtable": cap["roomVtable"], "sender_id": cap["senderLo"],
                "template_hex": cap["template_hex"], "texthdr_hex": cap["texthdr_hex"]}
    atomic(Path(a.template).parent, Path(a.template), template)
    spath = state_path(a)
    state = read_state(spath)
    state["live_msg_id_max"] = max(state.get("live_msg_id_max", 0), cap["msgId"])   # a live send, seen directly
    state["msg_id_next"] = max(state.get("msg_id_next", 0), state["live_msg_id_max"] + 0x200)
    write_state(spath, state)
    return {"status": "ok", "chatId": hex(chat_id), "msgId": hex(cap["msgId"]),
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


def resolve_room(fragment):
    """--room <title fragment> -> the one chat id whose indexed room title matches: read-only, straight from
    the search index (located and opened exactly as kakaotalk search does; only data_root is read from the
    config), with search's own --room title semantics (a case-insensitive LIKE substring, wildcards
    escaped). Zero matches refuse room_not_found; several refuse ambiguous_room_title with the count only."""
    import sqlite3

    from kakaotalk import search
    from rawlayer.management.base import default_config
    from rawlayer.search import index as search_index

    fragment = fragment.strip()
    if not fragment:
        bad("bad_room_fragment")
    try:
        path = search_index.index_path(search_index.load_data_root(str(default_config("kakaotalk"))),
                                       search.SPEC)
        if not path.exists():
            bad("index_missing")
        con = search_index.open_ro(path)
        try:
            sql, params = search_index.like("title", fragment)
            rows = con.execute("SELECT chat_id FROM rooms WHERE title != '' AND " + sql, params).fetchall()
        finally:
            con.close()
    except FetchError:
        raise
    except sqlite3.OperationalError as e:
        bad("index_busy" if search_index.is_locked(e) else "index_unreadable")
    except (OSError, ValueError, sqlite3.DatabaseError, search_index.UsageError):
        bad("index_unreadable")
    if not rows:
        bad("room_not_found")
    if len(rows) > 1:
        bad("ambiguous_room_title")
    return _chat_id(rows[0][0])


def _target_chat_id(a):
    """The rooms/send chat id: --chat-id or --room (exactly one of the two), the fragment resolved read-only
    from the search index before anything touches the client."""
    if a.chat_id and a.room:
        bad("chat_id_exclusive")
    if not a.chat_id and not a.room:
        bad("chat_id_required")
    return _chat_id(a.chat_id) if a.chat_id else resolve_room(a.room)


def _result_extras(resp):
    """log_id and sent_at from the client's WRITE response, only when the response provides them: log_id an
    exact 64-bit decimal string (the ledger key's msg id — never through a JS number), sent_at a unix
    timestamp as a decimal string. Anything not exactly that shape is left out."""
    extras = {}
    log_id = resp.get("logId")
    if isinstance(log_id, str) and log_id.isdigit() and 0 < int(log_id) < 1 << 63:
        extras["log_id"] = log_id
    sent_at = resp.get("sendAt")
    if isinstance(sent_at, int) and not isinstance(sent_at, bool):
        sent_at = str(sent_at)
    if isinstance(sent_at, str) and sent_at.isdigit() and 0 < int(sent_at) < 1 << 62:
        extras["sent_at"] = sent_at
    return extras


def cmd_rooms(a):
    _pid, proc, spec = guards()
    tpl = read_template(a.template, spec)
    rooms = find_rooms(proc, a.chat_id, spec)
    items = [{"room": hex(r), "vtable": hex(v), "matches_template": v == tpl["roomVtable"]}
             for r, v in sorted(rooms.items())]
    return {"status": "ok", "rooms": len(items),
            "matching": sum(i["matches_template"] for i in items), "items": items[:50]}, 0


def cmd_send(a):
    pid, proc, spec = guards()
    tpl = read_template(a.template, spec)
    message = read_message(a.message_file)
    chat_id = a.chat_id                       # --chat-id or --room, resolved before anything was touched
    rooms = find_rooms(proc, chat_id, spec)
    matching = [r for r, v in rooms.items() if v == tpl["roomVtable"]]
    if not matching:
        bad("no_room")
    if len(matching) > 1:
        bad("ambiguous_room")
    spath = state_path(a)
    with state_lock(spath.parent):                      # one send at a time: read -> persist -> client call
        state = read_state(spath)
        if not state:
            bad("bad_state")                            # never fall back to the calibrated id: it may be in flight
        msg_id = _next_msg_id(state)                    # ahead of the client's own counter, or colliding with it
        if not a.dry_run:                               # at-most-once: persist before the client is called
            state["msg_id_next"] = msg_id + 1
            write_state(spath, state)
        source = _render(SEND_JS, spec, room_vtable=tpl["roomVtable"], room=matching[0],
                         dry=1 if a.dry_run else 0, msg_id=msg_id, text=json.dumps(message),
                         chat_id="0x%X" % chat_id, template_hex=tpl["template_hex"],
                         texthdr_hex=tpl["texthdr_hex"], window_ms=RESPONSE_WAIT * 1000)
        try:
            session, script, lines, detached = _attach(pid, source)
        except FetchError:
            raise                                       # frida_missing / frida_attach_failed: nothing ran
        except Exception as e:                          # load() runs the script, which may have called the client
            if a.dry_run:
                raise
            return {"status": "failed_after_write", "detail": type(e).__name__, "msgId": hex(msg_id)}, 1
        try:
            # [RESP]: the target room's own response ends the wait early; the window-end stays the maximum
            line, lines = _collect(lines, ("[RESP]", "[WINDOW-END]", "[DRY]", "[FAIL]", "[SCRIPT-ERROR]"),
                                   RESPONSE_WAIT + 25, stop=lambda: bool(detached))
        finally:
            _teardown(session, script)
        failed = next((l for l in lines if l.startswith("[FAIL]")), None)
        if failed or not any(l.startswith(("[OK]", "[DRY]")) for l in lines):
            detail = {"detail": (failed or "no result")[:200], "msgId": hex(msg_id)}
            if any(l.startswith("[CALLED]") for l in lines):   # the client was called: it may have sent
                return {"status": "failed_after_write", **detail}, 1
            return {"status": "refused", **detail}, 2
        if a.dry_run:
            return {"status": "dry_run", "msgId": hex(msg_id), "utf8": len(message.encode("utf-8"))}, 0
        resp = None
        if line:                                        # [RESP] (early) or [WINDOW-END] (the maximum)
            try:
                resp = json.loads(line.split(" ", 1)[1])
            except (IndexError, ValueError):
                resp = None
        summary = line.split(" ", 1)[1] if line and line.startswith("[WINDOW-END] ") else None
        body = {"status": "sent", "msgId": hex(msg_id), "utf8": len(message.encode("utf-8")),
                "err_code": None}
        if isinstance(resp, dict) and resp.get("room") == str(chat_id):   # only this room's response counts
            body["err_code"] = resp.get("err")
            body.update(_result_extras(resp))           # log_id / sent_at, when the response carries them
            if resp.get("msg"):
                body["err_msg"] = resp.get("msg")
            if resp.get("err") == 0:
                return body, 0                                          # the server accepted it
            if isinstance(resp.get("err"), int):
                body["status"] = "server_rejected"                      # the write happened; it was refused
                return body, 1
            basis = "unreadable_response"                               # the room's response, code unreadable
        elif isinstance(resp, dict):
            basis = "other_room_response" if resp.get("room") else "unreadable_response"
        elif summary == "unreadable response":          # the JS tally the window ended with
            basis = "unreadable_response"
        elif summary and summary.startswith("other room responses"):
            basis = "other_room_response"
        else:
            basis = "no_response"
        body["status"] = "unconfirmed"                   # the write happened; the result is unknown
        body["outcome_basis"] = basis                    # never ids: which case, not which room
        return body, 1


def _post(script, message):
    try:
        script.post(message)
    except Exception:
        pass                                            # the session died: the wait ends on `detached`


def _wait_id(lines, seq, timeout, detached):
    """The builder call's end marker: the state lock is held until it arrives (or the session detaches)."""
    end = time.time() + timeout
    while time.time() < end:
        if any(l.startswith("[ID-DONE] %d" % seq) for l in lines):
            return
        if detached:
            return
        time.sleep(0.1)


def _take_id(script, lines, spath, seq, detached):
    """One listen reply's msgId: taken from the same state counter `send` uses, read and persisted under the
    state lock, which spans the client call (a concurrent send waits or refuses state_busy; a lock still busy
    after a short retry fails this one command — never sends it — and the listener stays up)."""
    deadline = time.time() + 2
    while True:
        try:
            with state_lock(spath.parent):
                state = read_state(spath)
                if not state:
                    bad("bad_state")
                msg_id = _next_msg_id(state)            # ahead of the client's own counter, like send's ids
                state["msg_id_next"] = msg_id + 1
                write_state(spath, state)               # at-most-once: persisted before the client is called
                _post(script, {"type": "id", "seq": seq, "msgId": msg_id})
                _wait_id(lines, seq, 5, detached)
                return
        except FetchError as e:
            if e.category != "state_busy":
                raise
            if time.time() >= deadline:
                _post(script, {"type": "id", "seq": seq, "error": "state busy"})
                _wait_id(lines, seq, 5, detached)
                return
            time.sleep(0.1)


def _persist_live_id(spath, mid):
    """Fold a live msgId the listener saw into the state under the same lock: every taken id must stay ahead
    of the client's own counter. A lock a concurrent send holds past a short retry drops this one id — ids
    only rise, so the owner's next message reports the newer maximum again."""
    deadline = time.time() + 2
    while True:
        try:
            with state_lock(spath.parent):
                state = read_state(spath)
                if state and mid > state.get("live_msg_id_max", 0):
                    state["live_msg_id_max"] = mid
                    write_state(spath, state)
            return
        except FetchError as e:
            if e.category != "state_busy":
                raise
            if time.time() >= deadline:
                return
            time.sleep(0.1)


def cmd_listen(a):
    pid, _proc, spec = guards()
    tpl = read_template(a.template, spec)
    if not 1 <= len(a.prefix) <= 16:
        bad("bad_prefix")
    spath = state_path(a)
    if not read_state(spath):
        bad("bad_state")                    # every reply's id comes from the same counter `send` uses
    source = _render(LISTEN_JS, spec, prefix=json.dumps(a.prefix), owner_lo=tpl["sender_id"],
                     maxpayload=TEXT_MAX_UTF8)
    session, script, lines, detached = _attach(pid, source)
    commands, seen, stopped_by = 0, 0, "interrupt"
    try:
        deadline = time.time() + a.lifetime if a.lifetime > 0 else None
        while True:
            time.sleep(0.2)
            fresh = lines[seen:]            # a snapshot: lines arriving during the handshake come next round
            for line in fresh:
                if line.startswith("[TAKE-ID] "):
                    try:
                        seq = int(line[10:])
                    except ValueError:
                        continue
                    _take_id(script, lines, spath, seq, detached)
                elif line.startswith("[LIVE-ID] "):     # the client's own counter, seen live: fold it in
                    try:
                        mid = int(line[10:])
                    except ValueError:
                        continue
                    _persist_live_id(spath, mid)
                elif not line.startswith("[ID-DONE] "):
                    print(json.dumps({"app": "kakaotalk", "command": "listen", "event": line[:200]}), flush=True)
                    if line.startswith("[CMD]"):
                        commands += 1
            seen += len(fresh)
            if detached:
                stopped_by = "detached"
                break
            if deadline is not None and time.time() >= deadline:
                stopped_by = "lifetime"
                break
    except KeyboardInterrupt:
        stopped_by = "interrupt"
    finally:
        _teardown(session, script)                      # one unload, one detach — never kill the process
    return {"status": "stopped", "prefix": a.prefix, "commands": commands,
            "stopped_by": stopped_by}, 0


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
        s = sub.choices[name]
        s.add_argument("--chat-id", help="room chat id, e.g. 0x1234567890 (or --room, not both)")
        s.add_argument("--room", help="room title fragment, resolved read-only from the search index")
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
        if a.command in ("rooms", "send"):
            a.chat_id = _target_chat_id(a)   # --chat-id or --room, resolved before the client is touched
        body, code = COMMANDS[a.command](a)
    except FetchError as e:
        body, code = {"status": e.category}, 2
    except Exception:
        body, code = {"status": "error", "error_type": type(sys.exc_info()[1]).__name__}, 2
    print(json.dumps({"app": "kakaotalk", "command": a.command, **body}))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
