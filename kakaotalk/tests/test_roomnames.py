# -*- coding: utf-8 -*-
"""Room titles from the client's own chat-list database: roomlist's fallback chains in isolation and the edb origin
reading an encrypted chatListInfo.edb beside the rooms (titled rooms, best-effort degradation when the list key is
missing or wrong). All data is made up."""
import shutil
import sqlite3
import unittest

from kakaotalk import roomlist
from kakaotalk.tests import edbfix as E
from kakaotalk.tests.fixtures import Fixture
from kakaotalk.tests.helpers import _force, capture, manifests, records

EDB = ["edb"]


class TestRoomlist(unittest.TestCase):
    def test_title_chain(self):
        """chat_titles: the client's title first, then the type-3 meta, then the displayed members."""
        con = sqlite3.connect(":memory:")
        self.addCleanup(con.close)
        con.executescript(E.LIST_DDL)
        con.executemany("INSERT INTO chatRoomList(chatId, chatRoomTitle, titleDisplayMembers) VALUES(?,?,?)",
                        [(1, "정식 제목", "멤甲, 멤乙"), (2, "", "멤甲, 멤乙"), (3, None, "멤甲, 멤乙"), (4, "  ", "\t")])
        con.executemany("INSERT INTO chatMetas VALUES(?, ?, ?)", [(1, 3, "메타 제목"), (2, 3, "메타 제목"),
                                                                  (4, 7, "공지 내용")])
        self.assertEqual(roomlist.chat_titles(con), {"1": "정식 제목", "2": "메타 제목", "3": "멤甲, 멤乙"})
        bare = sqlite3.connect(":memory:")
        self.addCleanup(bare.close)
        self.assertEqual(roomlist.chat_titles(bare), {})          # no tables at all
        bare.execute("CREATE TABLE chatRoomList(chatId INTEGER PRIMARY KEY)")   # no title-bearing column
        bare.execute("INSERT INTO chatRoomList VALUES(5)")
        self.assertEqual(roomlist.chat_titles(bare), {})

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

    def latest_room_rec(self, chat):
        recs = [r for r in records(self.fx) if r["key"] == f"kakao:{chat}" and r["op"] == "put"]
        return recs[-1]

    def edb(self):
        return manifests(self.fx)[-1]["origins"]["edb"]

    def test_titled_rooms(self):
        self.room(21, [E.msg(21, 1), E.msg(21, 2)])
        E.place_chat_list(self.fx, E.key_for(900002), [(21, "리스트 제목 21", None), (99, "방 없는 제목", None)])
        E.write_list_keystore_entry(self.extra, E.key_for(900002))
        self.keystore()
        self.assertEqual(capture(self.fx, EDB), 0)
        self.assertEqual(self.edb()["notes"].get("titled"), 2)
        rec = self.latest_room_rec(21)
        self.assertEqual(rec["native"], {"chatId": "21", "salt_fp": rec["native"]["salt_fp"]})
        self.assertEqual(rec["derived"].get("title"), "리스트 제목 21")
        # nothing changes -> nothing written (the volatile list columns never enter a record)
        self.assertEqual(capture(self.fx, EDB), 0)
        self.assertEqual(manifests(self.fx)[-1]["segments"], [])

    def test_a_list_that_cannot_be_opened_degrades_to_untitled(self):
        """No list file, a list without its key, a list under another key: best effort, never an error."""
        def untitled(why):
            with self.subTest(why):
                self.assertEqual(capture(self.fx, EDB), 0)
                self.assertEqual(self.edb()["status"], "ok")
                self.assertNotIn("titled", self.edb()["notes"])
                self.assertNotIn("title", self.latest_room_rec(24)["derived"])

        self.room(24, [E.msg(24, 1)])
        self.keystore()
        untitled("no list file")
        E.place_chat_list(self.fx, E.key_for(900003), [(24, "안 열리는 제목", None)])
        untitled("no list key")
        E.write_list_keystore_entry(self.extra, E.key_for(900004))
        self.keystore()
        untitled("wrong list key")


if __name__ == "__main__":
    unittest.main()
