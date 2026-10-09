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

USER_DDL = """
CREATE TABLE talkUser(userId INTEGER PRIMARY KEY, type INTEGER, nickName TEXT, friendNickName TEXT);
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


def make_user_db(path, names):
    """The backup's decrypted user database: {userId: (nickName, friendNickName)}. Replaces the file."""
    path = Path(path)
    if path.exists():
        path.unlink()
    _db(path, USER_DDL, {"talkUser": [(uid, 0, nick, friend) for uid, (nick, friend) in sorted(names.items())]})


class Fixture:
    """A full set of input paths plus a data root, laid out like config.json expects. Every path is a Path."""

    def __init__(self, base=None):
        self.base = Path(base or tempfile.mkdtemp(prefix="llmw-"))
        b = self.base
        self.inputs = b / "in"
        self.data_root = b / "data"
        self.backup = self.inputs / "backup_decrypted"
        self.user_dir = self.inputs / "kakao_user"
        self.keystore = self.inputs / "keys" / "keystore.json"
        for d in (self.backup, self.user_dir / "chat_data", self.keystore.parent, self.data_root):
            d.mkdir(parents=True, exist_ok=True)
        with self.keystore.open("w", encoding="utf-8") as fh:
            json.dump({}, fh)
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
               "sources": {"backup_decrypted": str(self.backup),
                           "kakao_user_dir": str(self.user_dir), "keystore": str(self.keystore)},
               "kb_search": str(kb), "kakatok_oracle_dir": str(oracle)}
        with self.config_path.open("w", encoding="utf-8") as fh:
            json.dump(cfg, fh)
        return self.config_path

    def standard(self):
        """Two backup rooms (titled by the manifest, senders named by the TalkUserDB beside them)."""
        r101 = make_room(self.backup / "101.sqlite", 101, 3,
                         hist=[(1, 1, "old")], meta=[(2, 0, 3, "c", None)], att=[(3, b"\x00\x01")])
        r102 = make_room(self.backup / "102.sqlite", 102, 2,
                         extra_rows=[(BIG, 102, 1, 7, b"\xffbin", None, T0 + 99, 0, 2)])
        make_user_db(self.backup / "TalkUserDB.sqlite", {7: ("Beta", None)})
        self.write_producer_manifest(self.backup, {"101": len(r101), "102": len(r102)},
                                     titles={"101": "r", "102": "r2"})
        return self

    @staticmethod
    def write_producer_manifest(root, rows_by_chat, titles=None):
        entries = []
        for c, n in sorted(rows_by_chat.items()):
            e = {"chatId": c, "rows": n, "from": "a", "to": "b"}
            t = (titles or {}).get(c)
            if t:
                e["title"] = t
            entries.append(e)
        with (Path(root) / "_manifest.json").open("w", encoding="utf-8") as fh:
            json.dump({"decrypted": entries, "updated": "x"}, fh)

