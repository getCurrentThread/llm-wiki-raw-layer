# -*- coding: utf-8 -*-
"""Room titles from the client's own chat-list database: roomlist's fallback chain in isolation, the edb
origin reading an encrypted chatListInfo.edb beside the rooms (titled rooms, best-effort degradation when the
list key is missing or wrong, idempotence), and the search fold picking the titles up. All data is made up."""
import shutil
import sqlite3
import unittest

from kakaotalk import roomlist
from kakaotalk.tests import edbfix as E
from kakaotalk.tests.fixtures import Fixture
from kakaotalk.tests.helpers import _force, capture, manifests, records

EDB = ["edb"]


class TestRoomlistChain(unittest.TestCase):
    """chat_titles: the client's title first, then the type-3 meta, then the displayed members."""

    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        self.con.executescript(E.LIST_DDL)

    def tearDown(self):
        self.con.close()

    def test_title_beats_meta_and_members(self):
        self.con.executemany("INSERT INTO chatRoomList(chatId, chatRoomTitle, titleDisplayMembers) VALUES(?,?,?)",
                             [(1, "정식 제목", "멤甲, 멤乙")])
        self.con.execute("INSERT INTO chatMetas VALUES(1, 3, '메타 제목')")
        self.assertEqual(roomlist.chat_titles(self.con), {"1": "정식 제목"})

    def test_meta_beats_members(self):
        self.con.execute("INSERT INTO chatRoomList(chatId, chatRoomTitle, titleDisplayMembers) VALUES(2, '', '멤甲, 멤乙')")
        self.con.execute("INSERT INTO chatMetas VALUES(2, 3, '메타 제목')")
        self.assertEqual(roomlist.chat_titles(self.con), {"2": "메타 제목"})

    def test_members_name_unnamed_rooms(self):
        self.con.execute("INSERT INTO chatRoomList(chatId, chatRoomTitle, titleDisplayMembers) VALUES(3, NULL, '멤甲, 멤乙')")
        self.assertEqual(roomlist.chat_titles(self.con), {"3": "멤甲, 멤乙"})

    def test_blank_everywhere_and_other_meta_types(self):
        self.con.execute("INSERT INTO chatRoomList(chatId, chatRoomTitle, titleDisplayMembers) VALUES(4, '  ', '\t')")
        self.con.execute("INSERT INTO chatMetas VALUES(4, 7, '공지 내용')")
        self.assertEqual(roomlist.chat_titles(self.con), {})

    def test_missing_tables_and_columns(self):
        con = sqlite3.connect(":memory:")
        self.assertEqual(roomlist.chat_titles(con), {})          # no tables at all
        con.execute("CREATE TABLE chatRoomList(chatId INTEGER PRIMARY KEY)")   # no title-bearing column
        con.execute("INSERT INTO chatRoomList VALUES(5)")
        self.assertEqual(roomlist.chat_titles(con), {})
        con.close()


class TestUserNames(unittest.TestCase):
    """user_names: the account's display name (nickName) first; the owner's alias (friendNickName) only where
    the account has no nickname -- the join the kakatok research documented."""

    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        self.con.executescript(E.USERS_DDL)

    def tearDown(self):
        self.con.close()

    def test_account_name_beats_the_owners_alias(self):
        self.con.execute("INSERT INTO talkUser VALUES(?,?,?,?)", (1, 2, "계정명", "내 별칭"))
        self.assertEqual(roomlist.user_names(self.con), {"1": "계정명"})

    def test_alias_names_accounts_without_a_nickname(self):
        self.con.executemany("INSERT INTO talkUser VALUES(?,?,?,?)",
                             [(2, 2, None, "별칭 뿐"), (3, 2, "  ", "공백 닉"), (4, 2, "정상 닉", "")])
        self.assertEqual(roomlist.user_names(self.con), {"2": "별칭 뿐", "3": "공백 닉", "4": "정상 닉"})

    def test_blank_and_missing_columns(self):
        self.con.execute("INSERT INTO talkUser VALUES(?,?,?,?)", (5, 2, None, None))
        self.assertEqual(roomlist.user_names(self.con), {})
        con = sqlite3.connect(":memory:")
        con.execute("CREATE TABLE talkUser(userId INTEGER PRIMARY KEY)")       # no name column at all
        con.execute("INSERT INTO talkUser VALUES(6)")
        self.assertEqual(roomlist.user_names(con), {})
        con.close()


class TestEdbChatList(unittest.TestCase):
    """The edb origin with an encrypted chatListInfo.edb in chat_data."""

    def setUp(self):
        self.fx = Fixture()
        self.cd = self.fx.user_dir / "chat_data"
        self.keys = {}
        self.extra = {}

    def tearDown(self):
        shutil.rmtree(self.fx.base, onerror=_force)

    def keystore(self):
        E.write_keystore(self.fx.keystore, self.keys, self.extra)

    def room(self, chat, rs):
        key = E.key_for(chat)
        self.keys[chat] = key
        E.place_room(self.fx, chat, rs, key)

    def chat_list(self, rooms, key=None, metas=()):
        key = key or E.key_for(900002)
        E.place_chat_list(self.fx, key, rooms, metas=metas)
        E.write_list_keystore_entry(self.extra, key)

    def latest_room_rec(self, chat):
        recs = [r for r in records(self.fx) if r["key"] == f"kakao:{chat}" and r["op"] == "put"]
        return recs[-1]

    def test_titled_rooms(self):
        self.room(21, [E.msg(21, 1), E.msg(21, 2)])
        self.chat_list([(21, "리스트 제목 21", None), (99, "방 없는 제목", None)])
        self.keystore()
        self.assertEqual(capture(self.fx, EDB), 0)
        self.assertEqual(self.edb()["notes"].get("titled"), 2)
        rec = self.latest_room_rec(21)
        self.assertEqual(rec["native"], {"chatId": "21", "salt_fp": rec["native"]["salt_fp"]})
        self.assertEqual(rec["derived"].get("title"), "리스트 제목 21")
        # nothing changes -> nothing written (the volatile list columns never enter a record)
        self.assertEqual(capture(self.fx, EDB), 0)
        self.assertEqual(manifests(self.fx)[-1]["segments"], [])

    def test_meta_and_member_fallbacks_apply_live(self):
        self.room(22, [E.msg(22, 1)])
        self.chat_list([(22, None, "표시 멤버 22")], metas=[(22, 3, "메타 제목 22")])
        self.keystore()
        self.assertEqual(capture(self.fx, EDB), 0)
        self.assertEqual(self.latest_room_rec(22)["derived"].get("title"), "메타 제목 22")

    def test_rename_retitles(self):
        self.room(23, [E.msg(23, 1)])
        self.chat_list([(23, "처음 제목", None)])
        self.keystore()
        self.assertEqual(capture(self.fx, EDB), 0)
        self.chat_list([(23, "바뀐 제목", None)])
        self.keystore()
        self.assertEqual(capture(self.fx, EDB), 0)
        self.assertEqual(self.latest_room_rec(23)["derived"].get("title"), "바뀐 제목")

    def test_no_list_key_degrades_to_untitled(self):
        self.room(24, [E.msg(24, 1)])
        self.chat_list([(24, "안 열리는 제목", None)])
        del self.extra[E.LIST_REL]                       # the list is there but its key is not
        self.keystore()
        self.assertEqual(capture(self.fx, EDB), 0)
        self.assertNotIn("titled", self.edb()["notes"])
        self.assertNotIn("title", self.latest_room_rec(24)["derived"])

    def test_wrong_list_key_is_not_an_error(self):
        self.room(25, [E.msg(25, 1)])
        E.place_chat_list(self.fx, E.key_for(900003), [(25, "다른 키 제목", None)])   # the file under key A
        E.write_list_keystore_entry(self.extra, E.key_for(900004))                    # the keystore says key B
        self.keystore()
        self.keystore()
        self.assertEqual(capture(self.fx, EDB), 0)
        self.assertEqual(self.edb()["status"], "ok")
        self.assertNotIn("title", self.latest_room_rec(25)["derived"])

    def test_no_list_file(self):
        self.room(26, [E.msg(26, 1)])
        self.keystore()
        self.assertEqual(capture(self.fx, EDB), 0)
        self.assertNotIn("titled", self.edb()["notes"])

    def edb(self, i=-1):
        return manifests(self.fx)[i]["origins"]["edb"]


if __name__ == "__main__":
    unittest.main()
