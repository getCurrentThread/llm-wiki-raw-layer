# -*- coding: utf-8 -*-
"""Synthetic ledgers for the kakaotalk search tests, made by running the real capture pipeline
(rawlayer.capture.run) on inputs synthesised in code (kakaotalk/tests/fixtures.py, kakaotalk/tests/edbfix.py), so
the index sees exactly the record shapes capture writes. Every name, title and text below is invented.

Helpers: tests/core/appfix.py (capture, run_rawsearch, clone_data_root, force_rmtree, kst, LedgerSeq).
Inputs: Msg, KakaoSources (writers for the five Kakao origins) and KakaoScenario (three capture rounds).
"""
import io
import os
import sqlite3
import stat
from pathlib import Path

from kakaotalk.tests import edbfix as E
# importing fixtures points rawlayer.capture.paths.DATA_PARENT at the temp directory
from kakaotalk.tests.fixtures import TEST_PARENT, Fixture
from tests.core import appfix

KAKAO_ORIGINS = ["merged_out", "backup_decrypted", "decrypted_live", "live_out", "edb"]

kst = appfix.kst
preserved_env = appfix.preserved_env
capture = appfix.capture
run_rawsearch = appfix.run_rawsearch
force_rmtree = appfix.force_rmtree
LedgerSeq = appfix.LedgerSeq
_force = appfix._force


def clone_data_root(src_data_root):
    return appfix.clone_data_root(src_data_root, TEST_PARENT)


# ------------------------------------------------------------------------------------------------ kakao inputs

class Msg:
    """One Kakao message as the producers would hold it."""

    def __init__(self, chat, log, ts, author, text, typ=1, deleted=0, sender=None):
        self.chat, self.log, self.ts, self.author, self.text = chat, log, ts, author, text
        self.type, self.deleted, self.sender = typ, deleted, sender

    def but(self, **kw):
        m = Msg(self.chat, self.log, self.ts, self.author, self.text, self.type, self.deleted, self.sender)
        for k, v in kw.items():
            setattr(m, "type" if k == "typ" else k, v)
        return m


MERGED_DDL = """
CREATE TABLE messages(chatId TEXT, logId TEXT, sendAt INTEGER, authorId TEXT, type INTEGER, deleted INTEGER,
  message TEXT, sender TEXT, type_name TEXT, date TEXT, source TEXT);
CREATE TABLE rooms(chatId TEXT, title TEXT, messages INTEGER, first TEXT, last TEXT, sources TEXT);"""
LIVE_DDL = """
CREATE TABLE messages(chatId TEXT, logId TEXT, sendAt INTEGER, authorId TEXT, type INTEGER, deleted INTEGER,
  message TEXT, sender TEXT, type_name TEXT, date TEXT, source TEXT, captured_at INTEGER);
CREATE TABLE cursors(chatId TEXT, last_logId TEXT, title TEXT, count INTEGER, first_ts INTEGER, last_ts INTEGER,
  updated_at INTEGER);"""


def _fresh_db(path, ddl):
    path = Path(path)
    for suf in ("", "-wal", "-shm", "-journal"):
        p = E.sidecar(path, suf)
        if p.exists():
            p.unlink()
    con = sqlite3.connect(path)
    con.executescript(ddl)
    return con


def _sender(m):
    return m.sender if m.sender is not None else str(m.author)    # producers fall back to the numeric id


class KakaoSources:
    """Writes the Kakao inputs of a Fixture. Each call replaces that input completely."""

    def __init__(self, fx):
        self.fx = fx
        self.keys = {}

    def merged(self, msgs, titles):
        con = _fresh_db(self.fx.merged, MERGED_DDL)
        con.executemany("INSERT INTO messages VALUES (?,?,?,?,?,?,?,?,?,?,?)", [
            (str(m.chat), str(m.log), m.ts, str(m.author), m.type, m.deleted, m.text, _sender(m), "t", "d", "backup")
            for m in msgs])
        con.executemany("INSERT INTO rooms VALUES (?,?,?,?,?,?)",
                        [(str(c), t, 0, "a", "b", "backup") for c, t in sorted(titles.items())])
        con.commit()
        con.close()

    def live(self, msgs, titles):
        con = _fresh_db(self.fx.live, LIVE_DDL)
        con.executemany("INSERT INTO messages VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", [
            (str(m.chat), str(m.log), m.ts, str(m.author), m.type, m.deleted, m.text, _sender(m), "t", "d", "live",
             m.ts + 2) for m in msgs])
        con.executemany("INSERT INTO cursors VALUES (?,?,?,?,?,?,?)",
                        [(str(c), "0", t, 0, 0, 0, 0) for c, t in sorted(titles.items())])
        con.commit()
        con.close()

    @staticmethod
    def _rows(msgs):
        return [(m.log, m.chat, m.type, m.author, m.text, m.ts, m.deleted, 0, 0, 0, 0) for m in msgs]

    def room_db(self, origin, chat, msgs, hist=()):
        """backup_decrypted / decrypted_live <chat>.sqlite (the room-DB chatLogs layout of edbfix.py)."""
        root = self.fx.backup if origin == "backup_decrypted" else self.fx.dlive
        path = root / f"{chat}.sqlite"
        if path.exists():
            path.unlink()
        E.plain_backup_room(path, self._rows(msgs), hist)

    def edb(self, chat, msgs, hist=()):
        """chat_data/chatLogs_<chat>.edb, encrypted like the real files, plus its keystore entry."""
        key = self.keys.setdefault(chat, E.key_for(chat))
        E.place_room(self.fx, chat, self._rows(msgs), key, hist=hist)
        E.write_keystore(self.fx.keystore, self.keys)


BIG = 2 ** 53 + 1           # a logId a float would round
A_ALPHA, A_BRAVO, A_CHARLIE, A_DELTA, A_NONAME = 11, 12, 13, 14, 15


class KakaoScenario:
    """Rooms (chat ids are made up):
      101 "스터디 방"     backup_decrypted + merged_out; Korean / Latin / digit / % texts, a photo, a 2^53+1 logId
      201 "web_dev 채널"  decrypted_live + merged_out; one message deleted locally (emptied in the room DB)
      301 "Edb 방 301"    edb (30 rows, a history row) + two rows also in live_out; title from live_out
      401 "Live 방"       live_out only (renamed "Renamed 방" in round 2)
      501 "webXdev 잡담"  live_out only (a title an unescaped LIKE '_' would match)
    Round 1: genesis. Round 2: edb 301 edits L5, deletes L6 for everyone (type | 0x4000, base64 body), loses L7
    and L8 (pending), gains L31; live_out gains 401:L3 with a new name for author 14 and renames 401.
    Round 3: same inputs; edb writes `absent` for L7 (then gone) and L8 (live_out still holds it)."""

    def __init__(self):
        self.fx = Fixture()
        self.src = KakaoSources(self.fx)
        self.rounds = 0

    @property
    def config(self):
        return self.fx.config_path

    @property
    def data_root(self):
        return self.fx.data_root

    def cleanup(self):
        force_rmtree(self.fx.base)

    # ---- data
    @staticmethod
    def room101():
        return [
            Msg(101, 1, kst(2024, 9, 2, 10), A_ALPHA, "회의는 10월에 합니다", sender="Alpha"),
            Msg(101, 2, kst(2024, 9, 15, 11), A_BRAVO, "가나 나다 라마", sender="Bravo"),
            Msg(101, 3, kst(2024, 10, 3, 9, 30), A_ALPHA, "가나다라 테스트", sender="Alpha"),
            Msg(101, 4, kst(2024, 10, 20, 14), A_CHARLIE, "API 키 발급 완료", sender="Charlie"),
            Msg(101, 5, kst(2024, 11, 5, 16), A_BRAVO, "rapid 배포 준비", sender="Bravo"),
            Msg(101, 6, kst(2024, 11, 6, 8), A_ALPHA, "100% 출석 체크", sender="Alpha"),
            Msg(101, 7, kst(2024, 11, 7, 9), A_CHARLIE, "", typ=2, sender="Charlie"),
            Msg(101, BIG, kst(2024, 11, 8, 10), A_NONAME, "큰 번호 메시지"),
        ]

    @staticmethod
    def room201_merged():
        return [
            Msg(201, 1, kst(2024, 10, 10, 15), A_BRAVO, "3시에 보자", sender="Bravo"),
            Msg(201, 2, kst(2024, 10, 11, 18), A_ALPHA, "SQL문을 고쳤어", sender="Alpha"),
            Msg(201, 3, kst(2024, 10, 12, 20), A_CHARLIE, "지운 메시지 원문", sender="Charlie"),
        ]

    @classmethod
    def room201_db(cls):
        m = cls.room201_merged()
        return m[:2] + [m[2].but(text="", deleted=1)]          # deleted locally: flag set, body emptied

    @staticmethod
    def edb301(rnd):
        out = []
        for i in range(1, 31):
            author = A_DELTA if i % 2 else A_ALPHA
            text = {5: "수정 전 문장", 6: "곧 삭제될 메시지", 7: "사라질 메시지 칠", 8: "라이브에도 있는 팔"}.get(i, f"edb 메시지 {i}")
            out.append(Msg(301, i, kst(2024, 12, 1, 9) + i * 3600, author, text))
        if rnd >= 2:
            out[4] = out[4].but(text="수정 후 문장")
            out[5] = out[5].but(typ=1 | 0x4000, text="QUJDREVGR0g=")
            out = [m for m in out if m.log not in (7, 8)]
            out.append(Msg(301, 31, kst(2024, 12, 5, 9), A_DELTA, "새로 온 메시지 10월에"))
        return out

    @classmethod
    def live_msgs(cls, rnd):
        e = {m.log: m for m in cls.edb301(1)}
        out = [e[8].but(sender="Alpha"), e[9].but(sender="Delta"),
               Msg(401, 1, kst(2025, 1, 2, 9), A_DELTA, "live 첫 메시지", sender="Delta"),
               Msg(401, 2, kst(2025, 1, 2, 9, 5), A_BRAVO, "라이브 두번째", sender="Bravo"),
               Msg(501, 1, kst(2025, 1, 4, 20), A_CHARLIE, "잡담 메시지", sender="Charlie")]
        if rnd >= 2:
            out.append(Msg(401, 3, kst(2025, 1, 3, 10), A_DELTA, "라이브 세번째", sender="Delta Two"))
        return out

    @staticmethod
    def live_titles(rnd):
        return {301: "Edb 방 301", 401: "Live 방" if rnd < 2 else "Renamed 방", 501: "webXdev 잡담"}

    def write_inputs(self, rnd):
        s = self.src
        s.merged(self.room101() + self.room201_merged(), {101: "스터디 방", 201: "web_dev 채널"})
        s.room_db("backup_decrypted", 101, self.room101())
        s.room_db("decrypted_live", 201, self.room201_db())
        s.edb(301, self.edb301(rnd), hist=[(5, 0, "수정 전 문장")])
        s.live(self.live_msgs(rnd), self.live_titles(rnd))

    def next_round(self):
        """Write the next round's inputs and capture every Kakao origin. Returns capture's exit code."""
        self.rounds += 1
        self.write_inputs(self.rounds)
        return capture(self.fx, KAKAO_ORIGINS)
