# -*- coding: utf-8 -*-
"""Synthetic edb files built in code: a real SQLite database with 80 reserved bytes per page (set
through sqlite3_file_control(SQLITE_FCNTL_RESERVE_BYTES) on the sqlite3.dll Python ships), its WAL
produced by SQLite itself, then every page encrypted the SQLCipher-4 raw-key way (AES-256-CBC over the
first 4016 bytes, page 1 keeping a clear 16-byte salt, IV in the reserved area) and the WAL checksums
recomputed over the encrypted bytes. Nothing here imports rawlayer/. Off Windows (no such sqlite3.dll) the tests
that build edb files are skipped."""
import ctypes
import hashlib
import json
import os
import sqlite3
import struct
import sys
import unittest
from pathlib import Path

from Crypto.Cipher import AES

PAGE, RESERVE = 4096, 80
CT_END = PAGE - RESERVE
T0 = 1_600_000_000
SQLITE_FCNTL_RESERVE_BYTES = 38

EDB_DDL = """
CREATE TABLE chatLogs(logId INTEGER PRIMARY KEY, chatId INTEGER, type INTEGER, authorId INTEGER,
  message TEXT, sendAt INTEGER, deleted INTEGER, prevLogId INTEGER, prev_msg_missing INTEGER,
  next_msg_missing INTEGER, revision INTEGER);
CREATE TABLE chatLogHistory(logId INTEGER, revision INTEGER, message TEXT, PRIMARY KEY(logId, revision));
CREATE TABLE chatLogDrafts(chatId INTEGER PRIMARY KEY, draft TEXT);
"""

# the client's chat-list database (chatListInfo.edb): only the columns roomlist.chat_titles reads
LIST_DDL = """
CREATE TABLE chatRoomList(chatId INTEGER PRIMARY KEY, type TEXT, activeMembersCount INTEGER,
  useCustomChatRoomTitle INTEGER, chatRoomTitle TEXT, titleDisplayMembers TEXT, lastUpdatedAt INTEGER,
  lastChatMessage TEXT, directChatMemberId INTEGER, linkId INTEGER);
CREATE TABLE chatMetas(chatId INTEGER, type INTEGER, content TEXT, PRIMARY KEY(chatId, type));
"""

# the client's user database (TalkUserDB.edb): only the columns roomlist.user_names reads
USERS_DDL = """
CREATE TABLE talkUser(userId INTEGER PRIMARY KEY, type INTEGER, nickName TEXT, friendNickName TEXT);
"""


def key_for(n):
    return hashlib.sha256(b"test-key-%d" % n).digest()


def msg(chat, lid, typ=1, text=None, deleted=0, prev=None, pm=0, nm=0, rev=0):
    return (lid, chat, typ, 7, text if text is not None else f"e{chat}-{lid}", T0 + lid * 60, deleted,
            (lid - 1 if lid > 1 else 0) if prev is None else prev, pm, nm, rev)


def plain_backup_room(path, rows, hist=()):
    """A decrypted backup room DB (the backup_decrypted layout) with the same chatLogs columns."""
    con = sqlite3.connect(path)
    con.executescript(EDB_DDL)
    if rows:
        con.executemany("INSERT INTO chatLogs VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
    if hist:
        con.executemany("INSERT INTO chatLogHistory VALUES (?,?,?)", hist)
    con.commit()
    con.close()


def sidecar(path, suffix):
    """`path` (a Path) with `suffix` appended to its last part: <x>.edb -> <x>.edb-wal ("" gives `path`)."""
    return path.with_name(path.name + suffix)


def _lib():
    if os.name != "nt":         # the reserve-bytes file control goes through the sqlite3.dll of a Windows Python
        raise unittest.SkipTest("synthetic edb files need the sqlite3.dll of a Windows Python")
    for d in (Path(sys.base_prefix) / "DLLs", Path(sys.executable).parent / "DLLs"):
        p = d / "sqlite3.dll"
        if p.exists():
            lib = ctypes.CDLL(p)
            lib.sqlite3_open_v2.argtypes = [ctypes.c_char_p, ctypes.POINTER(ctypes.c_void_p), ctypes.c_int,
                                            ctypes.c_char_p]
            lib.sqlite3_file_control.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int, ctypes.c_void_p]
            lib.sqlite3_exec.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_void_p, ctypes.c_void_p,
                                         ctypes.c_void_p]
            lib.sqlite3_close.argtypes = [ctypes.c_void_p]
            return lib
    raise RuntimeError("sqlite3.dll not found")


def reserve_db(path):
    """An empty one-page database whose header asks for 80 reserved bytes per page."""
    path = Path(path)
    if path.exists():
        path.unlink()
    lib = _lib()
    db = ctypes.c_void_p()
    if lib.sqlite3_open_v2(str(path).encode("utf-8"), ctypes.byref(db), 0x2 | 0x4, None) != 0:
        raise RuntimeError("open")
    n = ctypes.c_int(RESERVE)
    lib.sqlite3_file_control(db, b"main", SQLITE_FCNTL_RESERVE_BYTES, ctypes.byref(n))
    rc = lib.sqlite3_exec(db, b"PRAGMA page_size=4096; PRAGMA user_version=1;", None, None, None)
    lib.sqlite3_close(db)
    if rc != 0:
        raise RuntimeError("exec")
    with path.open("rb") as fh:
        head = fh.read(100)
    if head[20] != RESERVE:
        raise RuntimeError("reserve bytes not applied")


class PlainRoom:
    """A plaintext room DB in WAL mode. snapshot() returns (main bytes, wal bytes) as they are on disk
    while the writer is open; checkpoint() moves everything into main."""

    def __init__(self, path, rows, hist=(), ddl=None):
        self.path = Path(path)
        reserve_db(self.path)
        self.con = sqlite3.connect(self.path, isolation_level=None)
        self.con.execute("PRAGMA journal_mode=WAL")
        self.con.execute("PRAGMA wal_autocheckpoint=0")
        self.con.executescript(ddl or EDB_DDL)
        if ddl is None:
            self.add(rows, hist)
            self.con.execute("INSERT INTO chatLogDrafts VALUES (1, 'secret draft')")
        self.checkpoint()

    def add(self, rows, hist=()):
        self.con.execute("BEGIN")
        if rows:
            self.con.executemany("INSERT OR REPLACE INTO chatLogs VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
        if hist:
            self.con.executemany("INSERT INTO chatLogHistory VALUES (?,?,?)", hist)
        self.con.execute("COMMIT")

    def sql(self, q, args=()):
        self.con.execute("BEGIN")
        self.con.execute(q, args)
        self.con.execute("COMMIT")

    def checkpoint(self):
        self.con.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    def snapshot(self):
        main = self.path.read_bytes()
        wal = b""
        w = sidecar(self.path, "-wal")
        if w.exists():
            wal = w.read_bytes()
        return main, wal

    def rows(self):
        return self.con.execute("SELECT * FROM chatLogs ORDER BY logId").fetchall()

    def close(self):
        self.con.close()


# ---------------------------------------------------------------- encryption

def _iv(key, pgno, plain):
    return hashlib.sha256(key + struct.pack(">I", pgno) + plain).digest()[:16]


def enc_page(key, salt, pgno, plain):
    iv = _iv(key, pgno, plain)
    tail = hashlib.sha256(iv).digest() * 2           # the rest of the reserved area (HMAC slot), unused
    if pgno == 1:
        return salt + AES.new(key, AES.MODE_CBC, iv).encrypt(plain[16:CT_END]) + iv + tail
    return AES.new(key, AES.MODE_CBC, iv).encrypt(plain[:CT_END]) + iv + tail


def enc_main(key, salt, main):
    return b"".join(enc_page(key, salt, i // PAGE + 1, main[i:i + PAGE]) for i in range(0, len(main), PAGE))


def cksum(data, s0, s1, big):
    for x0, x1 in struct.iter_unpack(">II" if big else "<II", data):
        s0 = (s0 + x0 + s1) & 0xFFFFFFFF
        s1 = (s1 + x1 + s0) & 0xFFFFFFFF
    return s0, s1


def wal_frames(wal):
    """Plain SQLite WAL -> (header fields, [(pgno, commit, page)]) without any checking."""
    hdr = struct.unpack(">8I", wal[:32])
    out = []
    off = 32
    while off + 24 + PAGE <= len(wal):
        pgno, commit = struct.unpack(">II", wal[off:off + 8])
        out.append((pgno, commit, wal[off + 24:off + 24 + PAGE]))
        off += 24 + PAGE
    return hdr, out


def build_wal(frames, magic=0x377F0682, salts=(0x11111111, 0x22222222), ckpt=0, frame_salts=None):
    """frames: [(pgno, commit, stored page bytes)] -> WAL bytes with valid cumulative checksums.
    frame_salts: optional per-frame salt pairs (a previous generation's frames)."""
    big = (magic & 1) == 1
    hdr = struct.pack(">6I", magic, 3007000, PAGE, ckpt, salts[0], salts[1])
    s0, s1 = cksum(hdr, 0, 0, big)
    out = [hdr, struct.pack(">II", s0, s1)]
    for i, (pgno, commit, page) in enumerate(frames):
        fs = frame_salts[i] if frame_salts and frame_salts[i] else salts
        h8 = struct.pack(">II", pgno, commit)
        s0, s1 = cksum(h8, s0, s1, big)
        s0, s1 = cksum(page, s0, s1, big)
        out.append(h8 + struct.pack(">4I", fs[0], fs[1], s0, s1) + page)
    return b"".join(out)


def enc_wal(key, salt, wal, **kw):
    """Re-encrypt every frame page of a plain SQLite WAL and rebuild the checksums."""
    if not wal:
        return b""
    hdr, frames = wal_frames(wal)
    kw.setdefault("magic", hdr[0])
    kw.setdefault("salts", (hdr[4], hdr[5]))
    return build_wal([(p, c, enc_page(key, salt, p, pg)) for p, c, pg in frames], **kw)


# ---------------------------------------------------------------- placing files

def write_edb(chat_dir, chat, main_enc, wal_enc=b""):
    p = Path(chat_dir) / f"chatLogs_{chat}.edb"
    p.write_bytes(main_enc)
    w = sidecar(p, "-wal")
    if wal_enc:
        w.write_bytes(wal_enc)
    elif w.exists():
        w.unlink()
    return p


def write_keystore(path, keys, extra=None):
    """keys: {chat: 32-byte key}. extra: {relpath: entry} added as is (e.g. header_ok false)."""
    d = {}
    for chat, k in keys.items():
        d[f"chat_data\\chatLogs_{chat}.edb"] = {"chatId": int(chat), "key_hex": k.hex(), "wrapped_hex": "00",
                                               "integrity": True, "header_ok": True}
    d.update(extra or {})
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(d, fh)


def place_room(fx, chat, rows, key, salt=None, hist=(), wal_rows=None, keep=False):
    """Build a plaintext room (rows checkpointed into main; wal_rows, if given, as one more transaction
    left in the WAL), encrypt it and drop it into fx's chat_data. Returns the PlainRoom when keep=True."""
    salt = salt or hashlib.sha256(b"salt-%d" % chat).digest()[:16]
    work = Path(fx.base) / "plain"
    work.mkdir(parents=True, exist_ok=True)
    pr = PlainRoom(work / f"{chat}.sqlite", rows, hist)
    if wal_rows:
        pr.add(wal_rows)
    main, wal = pr.snapshot()
    write_edb(Path(fx.user_dir) / "chat_data", chat, enc_main(key, salt, main), enc_wal(key, salt, wal))
    if keep:
        return pr, salt
    pr.close()
    for suf in ("", "-wal", "-shm"):
        p = sidecar(pr.path, suf)
        if p.exists():
            p.unlink()
    return None


def place_chat_list(fx, key, rooms, metas=(), salt=None):
    """The client's chat-list database, encrypted like the real chat_data/chatListInfo.edb.
    rooms: [(chatId, custom_title_or_None, display_members_or_None)] — a None title falls through to the
    metas/display-members steps of roomlist's chain. metas: [(chatId, type, content)]."""
    salt = salt or hashlib.sha256(b"salt-chatlist").digest()[:16]
    work = Path(fx.base) / "plain"
    work.mkdir(parents=True, exist_ok=True)
    pr = PlainRoom(work / "chatListInfo.sqlite", (), ddl=LIST_DDL)
    pr.con.executemany(
        "INSERT INTO chatRoomList(chatId, type, activeMembersCount, useCustomChatRoomTitle, chatRoomTitle,"
        " titleDisplayMembers, lastUpdatedAt, lastChatMessage, directChatMemberId, linkId)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        [(int(c), "MultiChat", 3, 1 if t else 0, t, m, 1700000000, f"preview {c}", 0, 0)
         for c, t, m in rooms])
    pr.con.executemany("INSERT INTO chatMetas(chatId, type, content) VALUES (?,?,?)",
                       [(int(c), ty, ct) for c, ty, ct in metas])
    pr.con.commit()
    main, wal = pr.snapshot()
    d = Path(fx.user_dir) / "chat_data"
    write_list_edb(d, enc_main(key, salt, main), enc_wal(key, salt, wal))
    pr.close()
    for suf in ("", "-wal", "-shm"):
        p = sidecar(pr.path, suf)
        if p.exists():
            p.unlink()


def write_list_edb(chat_dir, main_enc, wal_enc=b""):
    p = Path(chat_dir) / "chatListInfo.edb"
    p.write_bytes(main_enc)
    w = sidecar(p, "-wal")
    if wal_enc:
        w.write_bytes(wal_enc)
    elif w.exists():
        w.unlink()
    return p


LIST_REL = "chat_data\\chatListInfo.edb"
USERS_REL = "TalkUserDB.edb"            # the store wraps it under the user directory's root


def write_list_keystore_entry(keys_extra, key):
    """The keystore entry that keys the chat-list database (chatId null: it is not a room)."""
    keys_extra[LIST_REL] = {"chatId": None, "key_hex": key.hex(), "wrapped_hex": "00",
                            "integrity": True, "header_ok": True}


def write_users_keystore_entry(keys_extra, key):
    """The keystore entry that keys the user database."""
    keys_extra[USERS_REL] = {"chatId": None, "key_hex": key.hex(), "wrapped_hex": "00",
                             "integrity": True, "header_ok": True}


def place_talk_user(user_dir, key, names, salt=None):
    """The client's user database, encrypted like the real <user dir>/TalkUserDB.edb (at the user directory's
    root, NOT in chat_data). names: {userId: (nickName, friendNickName)}."""
    salt = salt or hashlib.sha256(b"salt-talkuser").digest()[:16]
    work = Path(user_dir).parent / "plain"
    work.mkdir(parents=True, exist_ok=True)
    pr = PlainRoom(work / "TalkUserDB.sqlite", (), ddl=USERS_DDL)
    pr.con.executemany("INSERT INTO talkUser(userId, type, nickName, friendNickName) VALUES (?,?,?,?)",
                       [(int(u), 0, n, f) for u, (n, f) in sorted(names.items())])
    pr.con.commit()
    main, wal = pr.snapshot()
    p = Path(user_dir) / USERS_REL
    p.write_bytes(enc_main(key, salt, main))
    w = sidecar(p, "-wal")
    ewal = enc_wal(key, salt, wal)
    if ewal:
        w.write_bytes(ewal)
    elif w.exists():
        w.unlink()
    pr.close()
    for suf in ("", "-wal", "-shm"):
        q = sidecar(pr.path, suf)
        if q.exists():
            q.unlink()
