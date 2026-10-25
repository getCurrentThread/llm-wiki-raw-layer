# -*- coding: utf-8 -*-
"""kakaotalk dense retrieval (rawlayer/search/dense.py): the driver's chunk hooks (rec_key/key_where,
changed_containers, chunk_units) against a synthetic capture, and embed + search --hybrid end to end with a fake
Ollama server and a brute-force vector store -- the shape of tests/core/test_dense.py on the real kakaotalk driver.
All data is made up."""
import array
import contextlib
import hashlib
import http.server
import io
import json
import math
import sqlite3
import threading
import unittest
from unittest import mock

from rawlayer.search import common, dense, textindex as ti
from kakaotalk import search as kakao
from kakaotalk.tests import ledgerfix as L
from kakaotalk.tests.fixtures import Fixture

DIMS = 64
ROOM, OTHER, NAMED = "11", "12", "13"           # chat ids of the fixture rooms
T0 = L.kst(2024, 10, 1, 9)


def fake_vector(text, dims=DIMS):
    """A deterministic bag of bigrams: texts that share bigrams point the same way."""
    v = [0.0] * dims
    for tok in ti.ng_tokens(text):
        if tok != ti.SEP:
            v[int(hashlib.md5(tok.encode()).hexdigest(), 16) % dims] += 1.0
    return v if any(v) else [1.0] + [0.0] * (dims - 1)


class FakeOllama(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        data = json.dumps({"embeddings": [fake_vector(t, body["dimensions"]) for t in body["input"]]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *_args):
        pass


SERVER = URL = None


def setUpModule():
    global SERVER, URL
    SERVER = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeOllama)
    threading.Thread(target=SERVER.serve_forever, daemon=True).start()
    URL = f"http://127.0.0.1:{SERVER.server_address[1]}"


def tearDownModule():
    SERVER.shutdown()
    SERVER.server_close()


class BruteStore(dense.Vec0):
    """vec_chunks as a plain table, KNN by brute-force cosine (tests/core/test_dense.py's stand-in)."""

    @staticmethod
    def load(con):
        pass

    @staticmethod
    def create(con, dims):
        con.execute("CREATE TABLE IF NOT EXISTS vec_chunks(rowid INTEGER PRIMARY KEY, embedding BLOB NOT NULL, "
                    "ts_lo INTEGER, ts_hi INTEGER)")

    @staticmethod
    def knn(con, vec, k, since=None, until=None, ids=None):
        def dist(blob):
            w = array.array("f")
            w.frombytes(blob)
            return 1 - sum(x * y for x, y in zip(vec, w)) / math.sqrt(sum(x * x for x in vec) * sum(y * y for y in w))

        rows = [(dist(b), i) for i, b, lo, hi in con.execute("SELECT rowid, embedding, ts_lo, ts_hi FROM vec_chunks")
                if (since is None or hi >= since) and (until is None or lo <= until) and (ids is None or i in ids)]
        return [i for _, i in sorted(rows)[:k]]


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
    p = common.Parser(prog="manage.py search kakaotalk")
    kakao.add_arguments(p)
    return p


class FixtureCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fx = build()
        cls.con, _meta, _fresh = common.open_search(cls.fx.data_root, kakao.SPEC)
        cls.tz = common.parse_tz(common.DEFAULT_TZ)

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

    def test_every_changed_container_has_a_label_and_ordered_units(self):
        units = {c: kakao.DRIVER.chunk_units(self.con, c) for c in kakao.DRIVER.changed_containers(self.con, 0)}
        self.assertEqual(sorted(units), [ROOM, OTHER, NAMED])
        for chat, (label, us) in units.items():
            self.assertTrue(label, chat)                     # every fixture room is titled
            self.assertEqual([u[1] for u in us], sorted(u[1] for u in us))
        self.assertEqual([u[0] for u in units[OTHER][1]], [f"kakao:12:msg:1"])
        self.assertEqual(units[OTHER][0], "동떨어진 방")
        self.assertEqual(kakao.DRIVER.changed_containers(self.con, 10 ** 9), [])

    def test_key_where_parses_the_composite_and_skips_the_rest(self):
        sql, params = kakao.DRIVER.key_where([f"kakao:11:msg:{i}" for i in (1, 2)] + ["kakao:9", "discord:1:2", 7])
        self.assertEqual(sql, "(t.chat_id, t.log_id) IN (VALUES (?,?),(?,?))")
        self.assertEqual(params, ["11", "1", "11", "2"])
        self.assertEqual(kakao.DRIVER.key_where(["nonsense"]), ("1=0", []))


class RowsByKey(FixtureCase):

    def test_round_trip_honours_room_sender_and_excluded_term(self):
        keys = [u[0] for c in (ROOM, OTHER, NAMED) for u in kakao.DRIVER.chunk_units(self.con, c)[1]]
        q = ti.build_fts_query("대화 -두번째")
        flt = kakao.DRIVER.Filters(self.args(), self.tz)
        self.assertEqual(sorted(kakao.DRIVER.rows_by_key(self.con, keys, q, flt)),
                         sorted(set(keys) - {f"kakao:11:msg:2"}))
        flt = kakao.DRIVER.Filters(self.args("--room", OTHER), self.tz)
        self.assertEqual(sorted(kakao.DRIVER.rows_by_key(self.con, keys, q, flt)), [f"kakao:12:msg:1"])
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
        cls.con, _meta, _fresh = common.open_search(cls.fx.data_root, kakao.SPEC)

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


class HybridEndToEnd(FixtureCase):

    def setUp(self):
        p = mock.patch.object(dense, "STORE", BruteStore)
        p.start()
        self.addCleanup(p.stop)

    def embed(self, **kw):
        import argparse  # noqa: PLC0415
        a = argparse.Namespace(**{"model": "fake", "dims": DIMS, "url": URL, "limit": 0, "dry_run": False,
                                  "rebuild": False, **kw})
        return dense.embed(self.fx.data_root, kakao.DRIVER, a)

    def search(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(kakao.DRIVER.run(self.args(*argv)), 0)
        return json.loads(out.getvalue()), err.getvalue()

    def test_a_room_filter_searches_its_own_chunks(self):
        self.addCleanup(lambda: dense.sidecar_path(self.fx.data_root, "kakaotalk").unlink(missing_ok=True))
        self.embed()
        with mock.patch.object(dense, "SEM_K", 1):                     # the global KNN: the best chunk of all only
            env, _ = self.search("대화", "--hybrid", "--room", OTHER)
            self.assertEqual((env["hybrid"]["retrievers"]["sem"], env["rows"][0]["chat_id"]), (1, OTHER))
            self.assertEqual(env["rows"][0]["sources"], {"kw": 1, "sem": 1})   # its own best chunk, not room 11's

    def test_embed_then_hybrid_then_fallback(self):
        self.assertFalse(dense.sidecar_path(self.fx.data_root, "kakaotalk").exists())
        rep = self.embed()
        self.assertGreater(rep["chunks"], 0)
        self.assertEqual((rep["embedded"], rep["pending"]), (rep["chunks"], 0))
        self.assertEqual(self.embed()["embedded"], 0)               # nothing changed: nothing re-embedded
        env, _err = self.search("대화", "--hybrid")
        hy = env["hybrid"]
        self.assertTrue(hy["available"])
        self.assertEqual((hy["model"], hy["dims"]), ("fake", DIMS))
        fixture_keys = {kakao.DRIVER.rec_key(r) for r in self.con.execute("SELECT * FROM kakao")}
        self.assertTrue(env["rows"])
        for r in env["rows"]:
            self.assertTrue(r["key"].startswith("kakao:"))
            self.assertIn(r["key"], fixture_keys)
            self.assertIn("sources", r)
            self.assertIn("rrf", r)
        self.assertTrue(any(r.get("context") for r in env["rows"]))  # a chunk's rows ride along as context
        dense.sidecar_path(self.fx.data_root, "kakaotalk").unlink()
        env, err = self.search("대화", "--hybrid")
        self.assertFalse(env["hybrid"]["available"])
        self.assertIn("manage.py embed kakaotalk", env["hybrid"]["reason"])
        self.assertIn("--hybrid unavailable", err)
        self.assertTrue(env["rows"])                                 # keyword hits still come back
        self.assertTrue(all("sources" not in r for r in env["rows"]))


if __name__ == "__main__":
    unittest.main()
