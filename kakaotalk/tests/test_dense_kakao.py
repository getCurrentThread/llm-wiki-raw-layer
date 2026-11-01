# -*- coding: utf-8 -*-
"""kakaotalk's dense-retrieval hooks (rawlayer/search/dense.py): which rows are chunk units, which containers a seq
window marks as changed, and that the filters apply to semantic hits (rows_by_key). The embed + hybrid machinery
itself is tested in tests/core/test_dense.py. All data is made up."""
import unittest

from rawlayer.search import fts, index
from kakaotalk import search as kakao
from kakaotalk.tests import ledgerfix as L
from kakaotalk.tests.fixtures import Fixture

ROOM, OTHER, NAMED = "11", "12", "13"           # chat ids of the fixture rooms
T0 = L.kst(2024, 10, 1, 9)


# ------------------------------------------------------------------------------------------------ the fixture

def room11():
    return [
        L.Msg(11, 1, T0, 11, "첫 대화 메시지"),
        L.Msg(11, 2, T0 + 60, 12, "두번째 대화"),
        L.Msg(11, 3, T0 + 120, 11, "시스템이 적은 안내글", typ=0),        # system, with text: not a unit
        L.Msg(11, 4, T0 + 180, 12, "누가 들어오고 나갔어", typ=52),        # join/leave feed: not a unit
        L.Msg(11, 5, T0 + 240, 11, "", typ=2),                            # a photo: empty, not a unit
        L.Msg(11, 6, T0 + 300, 12, "지우고 남은 문장", deleted=1),        # deleted, text kept: not a unit
        L.Msg(11, 7, T0 + 7200, 11, "한참 뒤의 대화"),                     # 2 h on: its own chunk (gap 1800 s)
    ]


def write_round(src, rnd):
    src.room_db(11, room11())
    src.room_db(12, [L.Msg(12, 1, T0, 21, "다른 방 대화")])
    src.room_db(13, [L.Msg(13, 1, T0, 31, "이름 있는 방 대화")])
    src.manifest({11: "밀집 방", 12: "동떨어진 방" if rnd < 2 else "동떨어진 새 방", 13: "제목 그대로 방"})
    src.backup_users({11: ("레드", None), 12: ("블루", None), 21: ("그린", None),
                      31: ("옐로", None) if rnd < 2 else ("골드", None)})


def build(rounds=1):
    fx = Fixture()
    src = L.KakaoSources(fx)
    for rnd in range(1, rounds + 1):
        write_round(src, rnd)
        assert L.capture(fx, ["backup_decrypted"]) == 0
        rc, _out, err = L.run_rawsearch(["index", "--config", str(fx.config_path), "--source", "kakaotalk", "--json"])
        assert rc == 0, err
    return fx


def parser():
    p = index.Parser(prog="manage.py search kakaotalk")
    kakao.add_arguments(p)
    return p


class FixtureCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fx = build()
        cls.con, _meta, _fresh = index.open_search(cls.fx.data_root, kakao.SPEC)
        cls.tz = index.parse_tz(index.DEFAULT_TZ)

    @classmethod
    def tearDownClass(cls):
        cls.con.close()
        L.force_rmtree(cls.fx.base)

    def args(self, *argv):
        return parser().parse_args(["--config", str(self.fx.config_path), "--json", *argv])


class ChunkUnits(FixtureCase):

    def test_covers_the_live_text_rows_of_a_room_in_order(self):
        self.assertEqual(self.con.execute("SELECT count(*) FROM kakao WHERE chat_id=?", (ROOM,)).fetchone()[0], 7)
        label, us = kakao.DRIVER.chunk_units(self.con, ROOM)
        self.assertEqual(label, "밀집 방")
        self.assertEqual([u[0] for u in us], [f"kakao:11:msg:{i}" for i in (1, 2, 7)])   # order: (ts, log_no, id)
        self.assertTrue(all(u[3] is False for u in us))
        live = {kakao.DRIVER.rec_key(r): r for r in self.con.execute(
            "SELECT * FROM kakao WHERE chat_id=? AND deleted=0 AND message!='' ORDER BY ts, log_no, id", (ROOM,))}
        self.assertEqual(len(live), 5)                       # the system/feed rows are live but not units
        for u in us:
            h = kakao.DRIVER.hit(live[u[0]], self.tz, [], 80)
            self.assertEqual(u[0], h["key"])                 # the unit key is exactly hit()["key"]
            self.assertEqual(u[1], live[u[0]]["ts"])
            self.assertTrue(u[2].startswith(f"{h['sender'] or h['author_id']}: "))

class RowsByKey(FixtureCase):

    def test_round_trip_honours_room_sender_and_excluded_term(self):
        keys = [u[0] for c in (ROOM, OTHER, NAMED) for u in kakao.DRIVER.chunk_units(self.con, c)[1]]
        q = fts.build_fts_query("대화 -두번째")
        flt = kakao.DRIVER.Filters(self.args(), self.tz)
        self.assertEqual(sorted(kakao.DRIVER.rows_by_key(self.con, keys, q, flt)),
                         sorted(set(keys) - {"kakao:11:msg:2"}))
        flt = kakao.DRIVER.Filters(self.args("--room", OTHER), self.tz)
        self.assertEqual(sorted(kakao.DRIVER.rows_by_key(self.con, keys, q, flt)), ["kakao:12:msg:1"])
        flt = kakao.DRIVER.Filters(self.args("--sender", "레드"), self.tz)
        self.assertEqual(sorted(kakao.DRIVER.rows_by_key(self.con, keys, q, flt)),
                         [f"kakao:11:msg:{i}" for i in (1, 7)])
        self.assertEqual(kakao.DRIVER.rows_by_key(self.con, keys + ["kakao:99:msg:1"], q, flt),
                         kakao.DRIVER.rows_by_key(self.con, keys, q, flt))      # an unknown key is skipped


class ChangedContainers(unittest.TestCase):
    """Round 2 retitles room 12 (room_src), renames author 31 (authors -- and capture re-emits room 13's message
    with the new derived sender, moving ksrc), and leaves room 11 untouched."""

    @classmethod
    def setUpClass(cls):
        cls.fx = build(rounds=2)
        cls.con, _meta, _fresh = index.open_search(cls.fx.data_root, kakao.SPEC)

    @classmethod
    def tearDownClass(cls):
        cls.con.close()
        L.force_rmtree(cls.fx.base)

    def test_seq_window_marks_exactly_the_touched_rooms(self):
        self.assertEqual(sorted(kakao.DRIVER.changed_containers(self.con, 0)), [ROOM, OTHER, NAMED])
        self.assertEqual(sorted(kakao.DRIVER.changed_containers(self.con, 1)), [OTHER, NAMED])
        self.assertEqual(kakao.DRIVER.changed_containers(self.con, 2), [])
        self.assertEqual(self.con.execute("SELECT room FROM kakao WHERE chat_id=?", (OTHER,)).fetchone()[0],
                         "동떨어진 새 방")
        self.assertEqual(self.con.execute("SELECT sender FROM kakao WHERE chat_id=?", (NAMED,)).fetchone()[0],
                         "골드")


if __name__ == "__main__":
    unittest.main()
