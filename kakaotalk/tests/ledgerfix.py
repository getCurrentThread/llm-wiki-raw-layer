# -*- coding: utf-8 -*-
"""Synthetic ledgers for the kakaotalk search tests, made by running the real capture pipeline
(rawlayer.capture.run) on inputs synthesised in code (kakaotalk/tests/fixtures.py, kakaotalk/tests/edbfix.py), so
the index sees exactly the record shapes capture writes. Every name, title and text below is invented.

Helpers: tests/core/appfix.py (capture, run_rawsearch, clone_data_root, force_rmtree, kst, LedgerSeq).
Inputs: Msg, KakaoSources (writers for the backup and edb inputs) and KakaoScenario (three capture rounds).
"""
import json

from kakaotalk.tests import edbfix as E
# importing fixtures points rawlayer.capture.paths.DATA_PARENT at the temp directory
from kakaotalk.tests.fixtures import TEST_PARENT, Fixture, make_user_db
from tests.core import appfix

KAKAO_ORIGINS = ["backup_decrypted", "edb"]

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
    """One Kakao message as the room DBs hold it."""

    def __init__(self, chat, log, ts, author, text, typ=1, deleted=0):
        self.chat, self.log, self.ts, self.author, self.text = chat, log, ts, author, text
        self.type, self.deleted = typ, deleted

    def but(self, **kw):
        m = Msg(self.chat, self.log, self.ts, self.author, self.text, self.type, self.deleted)
        for k, v in kw.items():
            setattr(m, "type" if k == "typ" else k, v)
        return m


class KakaoSources:
    """Writes the Kakao inputs of a Fixture. Each call replaces that input completely."""

    def __init__(self, fx):
        self.fx = fx
        self.keys = {}
        self.extra = {}                     # the non-room keystore entries (chat list, user database)

    @staticmethod
    def _rows(msgs):
        return [(m.log, m.chat, m.type, m.author, m.text, m.ts, m.deleted, 0, 0, 0, 0) for m in msgs]

    def room_db(self, chat, msgs, hist=()):
        """backup_decrypted/<chat>.sqlite (the room-DB chatLogs layout of edbfix.py)."""
        path = self.fx.backup / f"{chat}.sqlite"
        if path.exists():
            path.unlink()
        E.plain_backup_room(path, self._rows(msgs), hist)

    def manifest(self, titles):
        """The backup producer's _manifest.json: rows and the titles the backup's chat-list database gave."""
        entries = []
        for p in sorted(self.fx.backup.glob("*.sqlite")):
            if not p.stem.isdigit():
                continue
            e = {"chatId": p.stem, "rows": 0, "from": "a", "to": "b"}
            t = titles.get(int(p.stem))
            if t:
                e["title"] = t
            entries.append(e)
        with (self.fx.backup / "_manifest.json").open("w", encoding="utf-8") as fh:
            json.dump({"decrypted": entries, "updated": "x"}, fh)

    def backup_users(self, names):
        """The backup's decrypted user database beside the rooms (sender names, the backup's rank)."""
        make_user_db(self.fx.backup / "TalkUserDB.sqlite", names)

    def edb(self, chat, msgs, hist=()):
        """chat_data/chatLogs_<chat>.edb, encrypted like the real files, plus its keystore entry."""
        key = self.keys.setdefault(chat, E.key_for(chat))
        E.place_room(self.fx, chat, self._rows(msgs), key, hist=hist)

    def chat_list(self, titles):
        """chat_data/chatListInfo.edb (the client's own chat-list database), encrypted and keyed."""
        key = E.key_for(900001)
        E.place_chat_list(self.fx, key, [(c, t, None) for c, t in sorted(titles.items())])
        E.write_list_keystore_entry(self.extra, key)
        E.write_keystore(self.fx.keystore, self.keys, self.extra)

    def talk_user(self, names):
        """<user dir>/TalkUserDB.edb (the client's own user database), encrypted and keyed."""
        key = E.key_for(900002)
        E.place_talk_user(self.fx.user_dir, key, names)
        E.write_users_keystore_entry(self.extra, key)
        E.write_keystore(self.fx.keystore, self.keys, self.extra)


BIG = 2 ** 53 + 1           # a logId a float would round
A_ALPHA, A_BRAVO, A_CHARLIE, A_DELTA, A_NONAME = 11, 12, 13, 14, 15


class KakaoScenario:
    """Rooms (chat ids are made up):
      101 "스터디 방"     backup_decrypted (manifest title); Korean / Latin / digit / % texts, a photo, a 2^53+1 logId
      201 "web_dev 채널"  backup (one message deleted locally: emptied there) + the full texts in the edb room
      301 "Edb 방 301"    edb (30 rows, a history row) + rows 8 and 9 also in the backup; title from the chat list
      401 "Live 방"       edb only, titled by the chat list (renamed "Renamed 방" in round 2)
      501 "webXdev 잡담"  backup only, titled by the manifest (a title an unescaped LIKE '_' would match)
      601 "클라 리스트 방" edb only; titled by the client's own chat-list database
                          (chatListInfo.edb; renamed "개명된 리스트 방" in round 2, the title alone re-titling it)
    Round 1: genesis. Round 2: edb 301 edits L5, deletes L6 for everyone (type | 0x4000, base64 body), loses L7
    and L8 (pending), gains L31; edb 401 gains L3 and the chat list renames it; the user database renames author
    14 to "Delta Two". Round 3: same inputs; edb writes `absent` for L7 (then gone) and L8 (the backup still
    holds it)."""

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
            Msg(101, 1, kst(2024, 9, 2, 10), A_ALPHA, "회의는 10월에 합니다"),
            Msg(101, 2, kst(2024, 9, 15, 11), A_BRAVO, "가나 나다 라마"),
            Msg(101, 3, kst(2024, 10, 3, 9, 30), A_ALPHA, "가나다라 테스트"),
            Msg(101, 4, kst(2024, 10, 20, 14), A_CHARLIE, "API 키 발급 완료"),
            Msg(101, 5, kst(2024, 11, 5, 16), A_BRAVO, "rapid 배포 준비"),
            Msg(101, 6, kst(2024, 11, 6, 8), A_ALPHA, "100% 출석 체크"),
            Msg(101, 7, kst(2024, 11, 7, 9), A_CHARLIE, "", typ=2),
            Msg(101, BIG, kst(2024, 11, 8, 10), A_NONAME, "큰 번호 메시지"),
        ]

    @staticmethod
    def room201():
        return [
            Msg(201, 1, kst(2024, 10, 10, 15), A_BRAVO, "3시에 보자"),
            Msg(201, 2, kst(2024, 10, 11, 18), A_ALPHA, "SQL문을 고쳤어"),
            Msg(201, 3, kst(2024, 10, 12, 20), A_CHARLIE, "지운 메시지 원문"),
        ]

    @classmethod
    def room201_backup(cls):
        m = cls.room201()
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

    @staticmethod
    def backup301():
        e = {m.log: m for m in KakaoScenario.edb301(1)}
        return [e[8], e[9]]

    @staticmethod
    def edb401(rnd):
        out = [Msg(401, 1, kst(2025, 1, 2, 9), A_DELTA, "live 첫 메시지"),
               Msg(401, 2, kst(2025, 1, 2, 9, 5), A_BRAVO, "라이브 두번째")]
        if rnd >= 2:
            out.append(Msg(401, 3, kst(2025, 1, 3, 10), A_DELTA, "라이브 세번째"))
        return out

    @staticmethod
    def room501():
        return [Msg(501, 1, kst(2025, 1, 4, 20), A_CHARLIE, "잡담 메시지")]

    @staticmethod
    def edb601():
        return [Msg(601, 1, kst(2025, 2, 1, 10), A_ALPHA, "리스트 방 첫 인사"),
                Msg(601, 2, kst(2025, 2, 2, 11), A_BRAVO, "두번째 인사 말")]

    @staticmethod
    def list_titles(rnd):
        return {301: "Edb 방 301", 401: "Live 방" if rnd < 2 else "Renamed 방",
                601: "클라 리스트 방" if rnd < 2 else "개명된 리스트 방"}

    @staticmethod
    def user_names(rnd):
        # (nickName = the account's display name, friendNickName = the owner's own alias): Alpha carries a
        # differing alias, so the suite proves the account's name wins everywhere the labels flow.
        names = {A_ALPHA: ("Alpha", "알파 별칭"), A_BRAVO: ("Bravo", None), A_CHARLIE: ("Charlie", None)}
        if rnd < 2:
            names[A_DELTA] = ("Delta", None)
        else:
            names[A_DELTA] = ("Delta Two", None)              # renamed in the round-2 user database
        return names

    def write_inputs(self, rnd):
        s = self.src
        s.room_db(101, self.room101())
        s.room_db(201, self.room201_backup())
        s.room_db(301, self.backup301())
        s.room_db(501, self.room501())
        s.manifest({101: "스터디 방", 201: "web_dev 채널", 501: "webXdev 잡담"})
        s.backup_users(self.user_names(rnd))
        s.edb(201, self.room201())
        s.edb(301, self.edb301(rnd), hist=[(5, 0, "수정 전 문장")])
        s.edb(401, self.edb401(rnd))
        s.edb(601, self.edb601())
        s.chat_list(self.list_titles(rnd))
        s.talk_user(self.user_names(rnd))

    def next_round(self):
        """Write the next round's inputs and capture every Kakao origin. Returns capture's exit code."""
        self.rounds += 1
        self.write_inputs(self.rounds)
        return capture(self.fx, KAKAO_ORIGINS)
