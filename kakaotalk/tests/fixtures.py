# -*- coding: utf-8 -*-
"""Synthetic inputs of the kakaotalk app built in code (no data files are committed). Every value is made up; ids stay
short so they cannot look like real Kakao ids."""
import json
import os
import sqlite3
import tempfile
from pathlib import Path

from rawlayer.capture import paths

BIG = 2 ** 53 + 1          # a logId that a float would round

# Test data roots live under the temp directory, outside the production data parent. Config refuses a data root outside
# paths.DATA_PARENT (SPEC 10.1), so the test process points it at the temp directory; a child process that
# builds a Config must do the same (TEST_PARENT). TEST_PARENT stays the str tempfile gives: it is also handed to
# child processes as JSON text.
TEST_PARENT = tempfile.gettempdir()
paths.DATA_PARENT = Path(TEST_PARENT)


def _db(path, ddl, rows_by_table):
    con = sqlite3.connect(path)
    con.executescript(ddl)
    for table, rows in rows_by_table.items():
        if rows:
            q = ",".join("?" * len(rows[0]))
            con.executemany(f'INSERT INTO "{table}" VALUES ({q})', rows)
    con.commit()
    con.close()


ROOM_DDL = """
CREATE TABLE chatLogs(logId INTEGER PRIMARY KEY, chatId INTEGER, type INTEGER, authorId INTEGER,
  message TEXT, attachment TEXT, sendAt INTEGER, deleted INTEGER, prevLogId INTEGER);
CREATE TABLE chatLogHistory(logId INTEGER, revision INTEGER, message TEXT, PRIMARY KEY(logId, revision));
CREATE TABLE chatLogMeta(logId INTEGER, revision INTEGER, type INTEGER, content TEXT, extra TEXT,
  PRIMARY KEY(logId, type));
CREATE TABLE chatLog_attachment(logId INTEGER PRIMARY KEY, attach_fullData BLOB);
CREATE TABLE chatLogDrafts(chatId INTEGER PRIMARY KEY, draft TEXT);
CREATE TABLE chatLogOutdated(logId INTEGER PRIMARY KEY);
"""

T0 = 1_600_000_000


def room_rows(chat, n, start=1):
    rows = []
    for i in range(start, start + n):
        rows.append((i, chat, 1, 7, f"m{chat}-{i}", None, T0 + i * 3600, 0, i - 1 if i > 1 else 0))
    return rows


def make_room(path, chat, n=3, extra_rows=(), hist=(), meta=(), att=(), draft=True):
    rows = room_rows(chat, n) + list(extra_rows)
    _db(path, ROOM_DDL, {"chatLogs": rows, "chatLogHistory": list(hist), "chatLogMeta": list(meta),
                         "chatLog_attachment": list(att), "chatLogDrafts": [(chat, "secret draft")] if draft else []})
    return rows


class Fixture:
    """A full set of input paths plus a data root, laid out like config.json expects. Every path is a Path."""

    def __init__(self, base=None):
        self.base = Path(base or tempfile.mkdtemp(prefix="llmw-"))
        b = self.base
        self.inputs = b / "in"
        self.data_root = b / "data"
        self.merged = self.inputs / "merged" / "merged.sqlite"
        self.live = self.inputs / "live" / "live.sqlite"
        self.backup = self.inputs / "backup_decrypted"
        self.dlive = self.inputs / "decrypted_live"
        self.user_dir = self.inputs / "kakao_user"
        self.keystore = self.inputs / "keys" / "keystore.json"
        for d in (self.merged.parent, self.live.parent, self.backup, self.dlive, self.user_dir / "chat_data",
                  self.keystore.parent, self.data_root):
            d.mkdir(parents=True, exist_ok=True)
        with self.keystore.open("w", encoding="utf-8") as fh:
            json.dump({}, fh)
        for p in (self.merged, self.live):          # empty files are valid empty SQLite databases
            p.write_bytes(b"")
        self.config_path = b / "config.json"
        self.write_config()

    def write_config(self, data_root=None):
        # The production config.json shape, keys of the removed verify included (kb_search, kakatok_oracle_dir:
        # accepted and ignored; inputs_root: read only by T1/T2), so every test also proves that shape still loads.
        frozen, oracle = self.base / "frozen", self.base / "oracle"
        kb = self.base / "kbstub" / "kb_search.py"
        for d in (frozen, oracle, kb.parent, data_root or self.data_root):
            Path(d).mkdir(parents=True, exist_ok=True)
        if not kb.exists():
            with kb.open("w", encoding="utf-8") as fh:
                fh.write("import sys\nsys.exit(1)\n")
        # config.json holds text: a data_root the caller passed as a str is written as given
        cfg = {"v": 1, "impl": "claude", "data_root": os.fspath(data_root or self.data_root),
               "inputs_root": str(frozen),
               "sources": {"merged_out": str(self.merged), "live_out": str(self.live),
                           "backup_decrypted": str(self.backup), "decrypted_live": str(self.dlive),
                           "kakao_user_dir": str(self.user_dir), "keystore": str(self.keystore)},
               "kb_search": str(kb), "kakatok_oracle_dir": str(oracle)}
        with self.config_path.open("w", encoding="utf-8") as fh:
            json.dump(cfg, fh)
        return self.config_path

    def standard(self):
        """Two backup rooms, one decrypted_live room, merged built by the build_merged rule, a live db."""
        r101 = make_room(self.backup / "101.sqlite", 101, 3,
                         hist=[(1, 1, "old")], meta=[(2, 0, 3, "c", None)], att=[(3, b"\x00\x01")])
        r102 = make_room(self.backup / "102.sqlite", 102, 2,
                         extra_rows=[(BIG, 102, 1, 7, b"\xffbin", None, T0 + 99, 0, 2)])
        r201 = make_room(self.dlive / "201.sqlite", 201, 4)
        _db(self.backup / "TalkUserDB.sqlite", "CREATE TABLE t(a);", {})
        self.write_producer_manifest(self.backup, {"101": len(r101), "102": len(r102)})
        self.write_producer_manifest(self.dlive, {"201": len(r201)})
        merged = []
        for chat, rows, src in ((201, r201, "live"), (101, r101, "backup"), (102, r102, "backup")):
            for (lid, _c, typ, author, msg, _a, send, deleted, _p) in rows:
                merged.append((str(chat), str(lid), send, str(author), typ, deleted, msg, "s", "t", "d", src))
        _db(self.merged, """
            CREATE TABLE messages(chatId TEXT, logId TEXT, sendAt INTEGER, authorId TEXT, type INTEGER,
              deleted INTEGER, message TEXT, sender TEXT, type_name TEXT, date TEXT, source TEXT);
            CREATE TABLE rooms(chatId TEXT, title TEXT, messages INTEGER, first TEXT, last TEXT, sources TEXT);""",
            {"messages": merged, "rooms": [("101", "r", 3, "a", "b", "backup"), ("201", "r", 4, "a", "b", "live")]})
        _db(self.live, """
            CREATE TABLE messages(chatId TEXT, logId TEXT, sendAt INTEGER, authorId TEXT, type INTEGER,
              deleted INTEGER, message TEXT, sender TEXT, type_name TEXT, date TEXT, source TEXT, captured_at INTEGER);
            CREATE TABLE cursors(chatId TEXT, last_logId TEXT, title TEXT, count INTEGER, first_ts INTEGER,
              last_ts INTEGER, updated_at INTEGER);""", {})
        return self

    @staticmethod
    def write_producer_manifest(root, rows_by_chat):
        d = {"decrypted": [{"chatId": c, "rows": n, "from": "a", "to": "b"} for c, n in sorted(rows_by_chat.items())],
             "updated": "x"}
        with (Path(root) / "_manifest.json").open("w", encoding="utf-8") as fh:
            json.dump(d, fh)


