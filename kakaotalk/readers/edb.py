# -*- coding: utf-8 -*-
"""edb (SPEC 10.3, 10.4): KakaoTalk chatLogs_<chatId>.edb, decrypted clean-room.

Pages are AES-256-CBC with a raw 32-byte key, one IV per page in the reserved area; page 1 keeps its
first 16 bytes (the salt) in clear. The WAL is applied up to its last valid commit frame, checking the
cumulative frame checksums over the stored (encrypted) bytes. The plaintext image only ever exists in
raw/.work/<run>/ and is removed as soon as the room is dumped.

kakatok's edb_reader is a test oracle only; nothing here imports it."""
import hashlib
import json
import re
import secrets
import struct
import time
from pathlib import Path

from Crypto.Cipher import AES

from rawlayer.capture import canon, fsio
from rawlayer.capture.model import Container, ContainerError, Obs

from . import dumper
from ..state import base_for, edb_salt

PAGE = 4096
RESERVE = 80
CT_END = PAGE - RESERVE            # 4016
IV_END = CT_END + 16               # 4032
FRAME = 24 + PAGE
SQLITE_MAGIC = b"SQLite format 3\x00"
WAL_MAGICS = (0x377F0682, 0x377F0683)
_FILE = re.compile(r"^chatLogs_([0-9]+)\.edb$")
_KEY_REL = re.compile(r"^chat_data\\chatLogs_([0-9]+)\.edb$")
ATTEMPTS = 3


# ---------------------------------------------------------------- keys

def load_keystore(path):
    """(keys {chatId: 32-byte key} for header_ok entries, all_ids, ok_ids). Keys never leave this process."""
    d = json.loads(fsio.read_shared(path))
    if not isinstance(d, dict):
        raise ValueError("keystore shape")
    keys, all_ids, ok_ids = {}, set(), set()
    for rel, e in d.items():
        m = _KEY_REL.fullmatch(rel)
        if not m or not isinstance(e, dict):
            continue
        cid = m.group(1)
        all_ids.add(cid)
        if e.get("header_ok") is not True:
            continue
        try:
            k = bytes.fromhex(e.get("key_hex") or "")
        except (ValueError, TypeError):         # one bad entry makes its own room no_key, not the origin error
            continue
        if len(k) == 32:
            keys[cid] = k
            ok_ids.add(cid)
    return keys, all_ids, ok_ids


def chat_of(name):
    m = _FILE.fullmatch(name)
    return m.group(1) if m else None


def edb_sid(chat):
    return canon.source_id(f"chat_data/chatLogs_{chat}.edb")


# ---------------------------------------------------------------- pages and WAL

def decrypt_page(key, page, first):
    iv = page[CT_END:IV_END]
    if first:
        return SQLITE_MAGIC + AES.new(key, AES.MODE_CBC, iv).decrypt(page[16:CT_END]) + bytes(RESERVE)
    return AES.new(key, AES.MODE_CBC, iv).decrypt(page[:CT_END]) + bytes(RESERVE)


def wal_cksum(data, s0, s1, big_endian):
    for x0, x1 in struct.iter_unpack(">II" if big_endian else "<II", data):
        s0 = (s0 + x0 + s1) & 0xFFFFFFFF
        s1 = (s1 + x1 + s0) & 0xFFFFFFFF
    return s0, s1


def read_wal(wal):
    """-> (facts, frames to apply [(pgno, encrypted page)], commit_size or None)."""
    facts = {"state": "none", "frames_total": 0, "frames_valid": 0, "frames_applied": 0, "commits": 0,
             "zero_filled": 0}
    if not wal:
        return facts, [], None
    facts["frames_total"] = max(0, (len(wal) - 32) // FRAME)
    if len(wal) < 32:
        facts["state"] = "bad_header"
        return facts, [], None
    magic, _fmt, psize, _ckpt, salt1, salt2, c1, c2 = struct.unpack(">8I", wal[:32])
    big = (magic & 1) == 1
    if magic not in WAL_MAGICS or psize != PAGE or wal_cksum(wal[:24], 0, 0, big) != (c1, c2):
        facts["state"] = "bad_header"
        return facts, [], None
    facts["state"] = "ok"
    s0, s1 = c1, c2
    valid = []
    last_commit = -1
    off = 32
    while off + FRAME <= len(wal):
        fh = wal[off:off + 24]
        pg = wal[off + 24:off + FRAME]
        pgno, commit, fs1, fs2, fc1, fc2 = struct.unpack(">6I", fh)
        if (fs1, fs2) != (salt1, salt2) or pgno < 1:
            break
        s0, s1 = wal_cksum(fh[:8], s0, s1, big)
        s0, s1 = wal_cksum(pg, s0, s1, big)
        if (s0, s1) != (fc1, fc2):
            break
        valid.append((pgno, commit, pg))
        if commit:
            last_commit = len(valid) - 1
        off += FRAME
    applied = valid[:last_commit + 1]
    facts["frames_valid"] = len(valid)
    facts["frames_applied"] = len(applied)
    facts["commits"] = sum(1 for _p, c, _g in applied if c)
    return facts, [(p, g) for p, _c, g in applied], (applied[-1][1] if applied else None)


def build_image(key, main, wal):
    """-> (plaintext image bytes or None when pages had to be zero-filled, wal facts).
    Raises ContainerError('unreadable', ...) when main is not whole pages."""
    if len(main) == 0 or len(main) % PAGE:
        raise ContainerError("unreadable", "main_not_page_multiple")
    facts, frames, commit_size = read_wal(wal)
    n_main = len(main) // PAGE
    latest = {}
    for pgno, pg in frames:
        latest[pgno] = pg                      # a later frame for the same page wins
    n = commit_size if commit_size else n_main
    out = []
    for i in range(1, n + 1):
        enc = latest.get(i)
        if enc is None:
            if i > n_main:
                facts["zero_filled"] += 1
                out.append(bytes(PAGE))
                continue
            enc = main[(i - 1) * PAGE:i * PAGE]
        out.append(decrypt_page(key, enc, i == 1))
    if facts["zero_filled"]:
        return None, facts
    return b"".join(out), facts


def header_ok(img):
    return len(img) >= 100 and img[16:18] == b"\x10\x00" and img[20] == RESERVE and img[21:24] == b"\x40\x20\x20"


# ---------------------------------------------------------------- stable read

def _wal_head(p):
    try:
        return fsio.read_shared(p, limit=32)
    except FileNotFoundError:
        return None


RETRY_SLEEP = 0.5


def stable_read(main_p, wal_p, sleep=None):
    sleep = RETRY_SLEEP if sleep is None else sleep
    for attempt in range(ATTEMPTS):
        before = (fsio.snap(main_p), fsio.snap(wal_p), _wal_head(wal_p))
        main = fsio.read_shared(main_p)
        try:
            wal = fsio.read_shared(wal_p)
        except FileNotFoundError:
            wal = None
        after = (fsio.snap(main_p), fsio.snap(wal_p), _wal_head(wal_p))
        sizes_ok = before[0] is not None and len(main) == before[0][0] and \
            ((wal is None and before[1] is None) or (wal is not None and before[1] is not None
                                                     and len(wal) == before[1][0]))
        if before == after and sizes_ok:
            return main, wal, before
        if attempt + 1 < ATTEMPTS:
            time.sleep(sleep)
    raise ContainerError("unstable", "unstable")


# ---------------------------------------------------------------- containers

def edb_containers(ctx):
    d = Path(ctx.inputs.edb_dir)
    keys, _all, _ok = load_keystore(ctx.inputs.keystore)
    notes = ctx.origin_notes["edb"]
    names = sorted(p.name for p in d.iterdir())  # no chat_data directory is a broken input (origin error), not 0 rooms
    out = []
    present = set()
    for name in names:
        if not name.endswith(".edb"):
            continue
        m = _FILE.fullmatch(name)
        if not m:
            notes["not_room"] += 1
            continue
        chat = m.group(1)
        present.add(chat)
        out.append(_room(ctx, d, name, chat, keys.get(chat)))
    before = {r[0][len("kakao:"):] for r in ctx.state.execute(
        "SELECT key FROM heads WHERE origin='edb' AND kind='room' AND op='put'")}
    gone = len(before - present)
    if gone:
        notes["room_gone"] += gone
    return out


def _room(ctx, d, name, chat, key):
    rel = "chat_data/" + name
    main_p = d / name
    wal_p = d / (name + "-wal")
    c = Container("edb", rel, edb_sid(chat), status="clean")
    if key is None:
        c.status = "no_key"
        return c
    held = {"con": None, "tmp": None}

    def it():
        main, wal, st = stable_read(main_p, wal_p)
        c.asof_s = max(int(st[0][1] // 1_000_000_000), int(st[1][1] // 1_000_000_000) if st[1] else 0)
        salt_fp = hashlib.sha256(main[:16]).hexdigest()[:16]
        img, wal_facts = build_image(key, main, wal)
        del main, wal
        c.wal = wal_facts
        if img is None:
            raise ContainerError("unreadable", "zero_filled")
        if not header_ok(img):
            prev = edb_salt(ctx.state, chat)
            if prev is not None and prev != salt_fp:
                raise ContainerError("rekeyed", "gate_header")
            raise ContainerError("unreadable", "gate_header")
        if wal_facts["state"] == "bad_header":
            c.status = "partial"
        tmp = Path(ctx.work_dir) / (secrets.token_hex(8) + ".sqlite")
        held["tmp"] = tmp
        tmp.write_bytes(img)
        del img
        con = fsio.connect_ro(tmp, immutable=True)
        held["con"] = con
        if con.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ContainerError("unreadable", "quick_check")
        c.tables = dumper.table_counts(con)
        yield Obs("room", f"kakao:{chat}", {"chatId": chat, "salt_fp": salt_fp}, {}, None, c.asof_s)
        yield from dumper.iter_room_obs(con, chat, base_for(ctx, "edb"), c.notes, c.asof_s)
        # immutable=1 never writes beside the image; a sidecar means it was not read as one frozen file
        if any(tmp.with_name(tmp.name + suf).exists() for suf in ("-wal", "-shm", "-journal")):
            raise ContainerError("unreadable", "sidecar_left")

    def cleanup():
        if held["con"] is not None:
            held["con"].close()
        tmp = held["tmp"]
        if tmp is not None:
            fsio.remove_file(tmp)
            for suf in ("-wal", "-shm", "-journal"):        # counted in it() (room -> unreadable)
                fsio.remove_file(tmp.with_name(tmp.name + suf))

    c.iter_obs = it
    c.cleanup = cleanup
    return c
