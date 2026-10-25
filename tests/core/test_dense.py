# -*- coding: utf-8 -*-
"""Dense retrieval (rawlayer.search.dense): the URL policy, the embedding client against a fake Ollama on 127.0.0.1,
pack(), and embed + search --hybrid end to end on a synthetic index with a brute-force vector store (the real
sqlite-vec only when RAWLAYER_TEST_SQLITE_VEC names its vec0 DLL). All data is synthesised in code."""
import argparse
import array
import contextlib
import hashlib
import http.server
import io
import json
import math
import os
import shutil
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from tests.core import toyfix as T
from rawlayer.search import common, dense, search as drv, textindex as ti

DIMS = 64
H = 3600
T0 = 1_700_000_000


def fake_vector(text, dims=DIMS):
    """A deterministic bag of bigrams: texts that share bigrams point the same way."""
    v = [0.0] * dims
    for tok in ti.ng_tokens(text):
        if tok != ti.SEP:
            v[int(hashlib.md5(tok.encode()).hexdigest(), 16) % dims] += 1.0
    return v if any(v) else [1.0] + [0.0] * (dims - 1)


class FakeOllama(http.server.BaseHTTPRequestHandler):
    calls = []

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeOllama.calls.append(dict(body, path=self.path))
        model = body["model"]
        if model == "http500":
            self.send_error(500)
            return
        if model == "garbage":                   # not an HTTP reply
            self.wfile.write(b"SSH-2.0-x\r\n")
            return
        if model == "redirect":
            self.send_response(302)
            self.send_header("Location", "http://127.0.0.1:9/")
            self.end_headers()
            return
        if model in ("message", "nope") or model == "flaky" and len(FakeOllama.calls) % 2:
            data = json.dumps({"error": "the input length exceeds the context length"}).encode()
            self.send_response({"message": 400, "nope": 404, "flaky": 400}[model])
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        vecs = [fake_vector(t, body["dimensions"]) for t in body["input"]]
        if model == "short":
            vecs = [v[:-1] for v in vecs]
        elif model == "nan":
            vecs[0][0] = float("nan")
        elif model == "zero":
            vecs[0] = [0.0] * len(vecs[0])
        data = json.dumps({"embeddings": vecs}).encode()
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
    """vec_chunks as a plain table, KNN by brute-force cosine (the insert/delete SQL is Vec0's)."""

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


# ------------------------------------------------------------------------------------------------ a synthetic app

class DtIndex(common.SourceIndex):
    name = "dt"
    origins = frozenset({"dt"})
    schema_version = extractor_version = "dt-1"
    fts_tables = ("rows_fts",)


SPEC = DtIndex()


class DtFilters(drv.Filters):
    def __init__(self, a, tz):
        super().__init__(a, tz)
        if a.sender:                     # the synthetic rows have no sender: --sender names a room
            self.where.append("t.room = ?")
            self.params.append(a.sender)


class DtSearch(drv.SearchDriver):
    name, table, fts_table = "dt", "rows", "rows_fts"
    sort_cols = ("ts", "key")
    Filters = DtFilters
    key_col, container_col, chunk_gap_s = "key", "room", 1800

    def hit(self, r, tz, marks, width):
        return {"id": r["id"], "key": r["key"], "room": r["room"], "ts": r["ts"], "deleted": bool(r["deleted"])}

    def context(self, con, r, n, a, tz):
        return [{**self.hit(r, tz, [], a.width), "hit": True}]

    def chunk_units(self, con, container):
        rows = con.execute("SELECT key, ts, message FROM rows WHERE room=? AND deleted=0 AND message!='' "
                           "ORDER BY ts, key", (container,)).fetchall()
        return "room " + container, [(r["key"], r["ts"], "someone: " + r["message"], False) for r in rows]


DRIVER = DtSearch(SPEC)

ROWS = [  # key, room, ts, message
    ("a1", "A", T0, "주간 회의 일정 공유합니다"),
    ("a2", "A", T0 + 60, "점심 메뉴 추천 받아요"),
    ("a3", "A", T0 + 3 * H, "배포 체크리스트 정리"),          # 3 h later: a new chunk in room A
    ("b1", "B", T0 + 10 * H, "서버 장애 보고서 작성"),
    ("b2", "B", T0 + 10 * H + 30, "장애 원인은 디스크 부족"),
]


def build_index(root, rows=ROWS, head=1):
    path = common.index_path(root, SPEC)
    path.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.closing(sqlite3.connect(path)) as con:
        con.executescript(common.META_DDL)
        con.execute("CREATE TABLE rows(id INTEGER PRIMARY KEY, key TEXT NOT NULL UNIQUE, room TEXT NOT NULL, "
                    "ts INTEGER NOT NULL, seq INTEGER NOT NULL, deleted INTEGER NOT NULL DEFAULT 0, "
                    "message TEXT NOT NULL, ng TEXT NOT NULL)")
        con.execute(ti.fts_ddl("rows"))
        common.create_triggers(con, "rows", "rows_fts")
        con.executemany("INSERT INTO rows(key, room, ts, seq, message, ng) VALUES(?,?,?,1,?,?)",
                        [(k, r, ts, m, ti.ng_text(m)) for k, r, ts, m in rows])
        common._set_meta(con, **SPEC.stamp(), head_seq=head)
        con.commit()


def change(root, sql, params, head):
    """Fold a change into the synthetic index: run sql (it sets seq) and move head_seq."""
    with contextlib.closing(sqlite3.connect(common.index_path(root, SPEC))) as con:
        con.execute(sql, params)
        common._set_meta(con, head_seq=head)
        con.commit()


def parser():
    p = common.Parser()
    common.add_search_arguments(p, ["hits", "count"])
    p.add_argument("--before")
    p.add_argument("--after")
    return p


def embed_args(**kw):
    return argparse.Namespace(**{"model": "fake", "dims": DIMS, "url": URL, "limit": 0, "dry_run": False,
                                 "rebuild": False, **kw})


# ------------------------------------------------------------------------------------------------ tests

class UrlAndClient(unittest.TestCase):

    def test_only_loopback_ip_literals(self):
        for url, want in (("http://127.0.0.1:11434", "http://127.0.0.1:11434"), ("http://127.0.0.1", "http://127.0.0.1:80"),
                          ("http://127.0.0.2:5/", "http://127.0.0.2:5"), ("http://[::1]:8", "http://[::1]:8")):
            self.assertEqual(dense.check_url(url), want)
        for bad in ("https://127.0.0.1:1", "http://localhost:1", "http://10.0.0.1:1", "http://0.0.0.0:1",
                    "http://user@127.0.0.1:1", "http://127.0.0.1:1/api", "http://127.0.0.1:99999", "ftp://127.0.0.1",
                    "", None, "http://127.0.0.1:1?x=1", "http://example.com"):
            with self.subTest(url=bad), self.assertRaises(common.UsageError):
                dense.check_url(bad)

    def test_embed_request_and_checks(self):
        FakeOllama.calls.clear()
        with mock.patch.dict(os.environ, {"HTTP_PROXY": "http://127.0.0.1:9", "http_proxy": "http://127.0.0.1:9"}):
            vecs = dense.Embedder(URL, "fake", DIMS)(["회의 일정", "점심"])     # a proxy in the environment is ignored
        self.assertEqual([len(v) for v in vecs], [DIMS, DIMS])
        call = FakeOllama.calls[-1]
        self.assertEqual((call["path"], call["dimensions"], call["keep_alive"], call["input"]),
                         ("/api/embed", DIMS, "30m", ["회의 일정", "점심"]))
        for model in ("http500", "redirect", "short", "nan", "zero", "garbage"):
            with self.subTest(model=model), self.assertRaises(dense.EmbedError) as cm:
                dense.Embedder(URL, model, DIMS)(["비밀 본문 텍스트"])
            self.assertNotIn("비밀", str(cm.exception))
            self.assertEqual(cm.exception.retry, model in ("http500", "redirect", "garbage"))
        with self.assertRaises(dense.EmbedError):
            dense.Embedder("http://127.0.0.1:9", "fake", DIMS, timeout=5)(["x"])

    def test_server_message_and_retries(self):
        with self.assertRaises(dense.EmbedError) as cm:
            dense.Embedder(URL, "message", DIMS)(["비밀 본문 텍스트"])
        self.assertEqual((str(cm.exception), cm.exception.retry),
                         ("embedding server: HTTP 400: the input length exceeds the context length", True))
        with self.assertRaises(dense.EmbedError) as cm:
            dense.Embedder(URL, "nope", DIMS)(["x"])
        self.assertEqual((cm.exception.retry, str(cm.exception).endswith("(is nope pulled?)")), (False, True))
        with mock.patch.object(dense, "RETRY_WAITS", (0, 0)):
            for model, calls, ok in (("flaky", 2, True), ("message", 3, False), ("nope", 1, False)):
                FakeOllama.calls.clear()
                with self.subTest(model=model):
                    try:
                        self.assertEqual(len(dense.embed_retrying(dense.Embedder(URL, model, DIMS), ["회의"])), 1)
                    except dense.EmbedError:
                        self.assertFalse(ok)
                    self.assertEqual(len(FakeOllama.calls), calls)

    def test_query_prefix(self):
        self.assertTrue(dense.query_prefix("qwen3-embedding:4b").startswith("Instruct: "))
        self.assertEqual(dense.query_prefix("bge-m3"), "")

    def test_vec0_loader_refuses_a_missing_or_foreign_dll(self):
        tmp = Path(tempfile.mkdtemp(dir=T.TEST_PARENT))
        self.addCleanup(shutil.rmtree, tmp, True)
        con = sqlite3.connect(":memory:")
        self.addCleanup(con.close)
        with mock.patch.object(dense, "vec_dll", return_value=tmp / "none.dll"), \
                self.assertRaisesRegex(dense.Unavailable, "not installed"):
            dense.Vec0.load(con)
        (tmp / "vec0.dll").write_bytes(b"not sqlite-vec")
        with mock.patch.object(dense, "vec_dll", return_value=tmp / "vec0.dll"), \
                self.assertRaisesRegex(dense.Unavailable, "SHA-256"):
            dense.Vec0.load(con)


class Pack(unittest.TestCase):

    def test_joins_small_units_under_a_dated_header(self):
        (keys, lo, hi, text), = dense.pack("room A", [("k1", T0, "a: 안녕", False), ("k2", T0 + 5, "b: 반가워", False)])
        self.assertEqual((keys, lo, hi), (["k1", "k2"], T0, T0 + 5))
        self.assertTrue(text.startswith("room A | 20"))
        self.assertTrue(text.endswith("a: 안녕\nb: 반가워"))
        self.assertEqual(dense.pack("x", [("k", 0, "  ", False)]), [])
        self.assertEqual(dense.pack("x", [("k", 0, "글", False)])[0][3], "x\n글")      # no time: no date

    def test_target_overlap_and_cap(self):
        units = [(f"k{i}", T0 + i, f"u{i}: " + "가" * 95, False) for i in range(30)]
        chunks = dense.pack("L", units, gap_s=1800)
        self.assertGreater(len(chunks), 2)
        body = [c[3].split("\n", 1)[1] for c in chunks]
        self.assertTrue(all(len(b) <= dense.TARGET + 100 for b in body))
        for prev, cur in zip(chunks, chunks[1:]):       # a short last unit is carried over
            self.assertEqual(cur[0][0], prev[0][-1])
        self.assertEqual({k for c in chunks for k in c[0]}, {u[0] for u in units})

    def test_gap_and_heading_start_a_chunk_without_overlap(self):
        units = [("k1", T0, "a: 하나", False), ("k2", T0 + 2 * H, "a: 둘", False)]
        self.assertEqual([c[0] for c in dense.pack("L", units, gap_s=1800)], [["k1"], ["k2"]])
        self.assertEqual(len(dense.pack("L", units)), 1)                 # no gap rule: one chunk
        units = [("h1", T0, "제목", True), ("p1", T0, "본문", False), ("h2", T0, "다음 제목", True), ("p2", T0, "끝", False)]
        self.assertEqual([c[0] for c in dense.pack("P", units)], [["h1", "p1"], ["h2", "p2"]])

    def test_a_long_unit_becomes_overlapping_windows(self):
        text = "".join(chr(0xAC00 + i % 500) for i in range(4000))
        chunks = dense.pack("L", [("k", T0, text, False)])
        pieces = [c[3].split("\n", 1)[1] for c in chunks]
        self.assertTrue(all(len(p) <= dense.CAP for p in pieces))
        self.assertEqual(pieces[0] + "".join(p[dense.OVERLAP:] for p in pieces[1:]), text)
        self.assertTrue(all(c[0] == ["k"] for c in chunks))

    def test_a_long_label_is_flattened_and_cut(self):
        label = "상위 페이지 제목\n" * 300               # a breadcrumb alone past Ollama's 2048 tokens
        self.assertEqual(dense.pack(label, [("k", 0, "본문", False)])[0][3],
                         common.flat(label)[:dense.LABEL_CAP] + "\n본문")
        self.assertEqual(dense.pack("짧은\n제목", [("k", 0, "본문", False)])[0][3], "짧은\n제목\n본문")

    def test_sha_is_stable_and_per_container(self):
        a = dense.chunk_sha("A", ["k1"], "t")
        self.assertEqual(a, dense.chunk_sha("A", ["k1"], "t"))
        self.assertNotEqual(a, dense.chunk_sha("B", ["k1"], "t"))
        self.assertNotEqual(a, dense.chunk_sha("A", ["k2"], "t"))


class HybridEndToEnd(unittest.TestCase):

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(dir=T.TEST_PARENT))
        self.addCleanup(T.force_rmtree, self.root)
        self.cfg = self.root / "config.json"
        self.cfg.write_text(json.dumps({"data_root": str(self.root)}), encoding="utf-8")
        build_index(self.root)
        p = mock.patch.object(dense, "STORE", BruteStore)
        p.start()
        self.addCleanup(p.stop)

    def embed(self, **kw):
        return dense.embed(self.root, DRIVER, embed_args(**kw))

    def search(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(DRIVER.run(parser().parse_args(["--config", str(self.cfg), "--json", *argv])), 0)
        return json.loads(out.getvalue()), err.getvalue()

    def side(self, sql, params=()):
        with contextlib.closing(sqlite3.connect(dense.sidecar_path(self.root, "dt"))) as con:
            return con.execute(sql, params).fetchall()

    def test_embed_incremental_purge_limit_and_stamp(self):
        dry = self.embed(dry_run=True)
        self.assertEqual((dry["containers"], dry["chunks"], dry["pending"]), (2, 3, 3))
        self.assertFalse(dense.sidecar_path(self.root, "dt").exists())
        rep = self.embed()
        self.assertEqual((rep["embedded"], rep["pending"], rep["src_seq"]), (3, 0, 1))
        self.assertEqual(self.side("SELECT count(*) FROM vec_chunks"), [(3,)])
        self.assertEqual(self.embed()["embedded"], 0)                      # nothing changed: nothing re-embedded
        change(self.root, "UPDATE rows SET message=?, ng=?, seq=2 WHERE key='b2'", ("장애 원인은 메모리", ti.ng_text("장애 원인은 메모리")), 2)
        rep = self.embed()
        self.assertEqual((rep["containers"], rep["embedded"], rep["removed"], rep["src_seq"]), (1, 1, 1, 2))
        change(self.root, "UPDATE rows SET message='', ng='', seq=3 WHERE key='a3'", (), 3)    # a purge
        rep = self.embed()
        self.assertEqual((rep["embedded"], rep["removed"]), (0, 1))
        self.assertEqual(self.side("SELECT count(*) FROM chunk_rec WHERE rec_key='a3'"), [(0,)])
        self.assertEqual(self.side("SELECT count(*) FROM chunks"), self.side("SELECT count(*) FROM vec_chunks"))
        with self.assertRaisesRegex(common.UsageError, "--rebuild"):
            self.embed(model="other")
        rep = self.embed(model="other", rebuild=True, limit=1)            # resumable: the watermark waits
        self.assertEqual((rep["embedded"], rep["pending"], rep["src_seq"]), (1, 1, 0))
        rep = self.embed(model=None)                                       # the sidecar's model
        self.assertEqual((rep["model"], rep["embedded"], rep["pending"], rep["src_seq"]), ("other", 1, 0, 3))
        self.assertEqual(dict(self.side("SELECT k, v FROM meta"))["chunker"], f"{dense.CHUNKER}/1")

    def test_embed_retries_a_failed_batch(self):
        FakeOllama.calls.clear()
        with mock.patch.object(dense, "RETRY_WAITS", (0, 0)):
            rep = self.embed(model="flaky")                                # the one batch fails once
        self.assertEqual((rep["embedded"], rep["pending"], len(FakeOllama.calls)), (3, 0, 2))

    def test_a_new_chunker_rechunks_in_place(self):
        old = dense.chunker_id(DRIVER)
        self.embed()
        with mock.patch.object(dense, "CHUNKER", "pack-test"):
            env, err = self.search("회의 일정", "--hybrid")                 # older chunks: usable, but stale
            self.assertEqual((env["hybrid"]["available"], env["hybrid"]["stale"]), (True, True))
            self.assertIn("behind the index", err)
            with mock.patch.object(dense, "LABEL_CAP", 5):                 # every chunk's text changes
                rep = self.embed(limit=1)
                self.assertEqual((rep["containers"], rep["embedded"], rep["removed"], rep["pending"]), (2, 1, 3, 2))
                self.assertEqual(dict(self.side("SELECT k, v FROM meta"))["chunker"], old)   # not done yet
                rep = self.embed()
                self.assertEqual((rep["containers"], rep["embedded"], rep["removed"], rep["pending"]), (2, 2, 0, 0))
            self.assertEqual(dict(self.side("SELECT k, v FROM meta"))["chunker"], "pack-test/1")
            env, err = self.search("회의 일정", "--hybrid")
            self.assertFalse(env["hybrid"]["stale"])
            self.assertNotIn("behind the index", err)

    def test_stale_only_when_rows_changed(self):
        self.embed()
        change(self.root, "UPDATE rows SET seq=seq WHERE 0", (), 5)            # the ledger moved, no row did
        env, err = self.search("회의 일정", "--hybrid")
        self.assertEqual((env["hybrid"]["vec_seq"], env["index"]["seq"], env["hybrid"]["stale"]), (1, 5, False))
        self.assertNotIn("behind the index", err)
        change(self.root, "UPDATE rows SET message=?, ng=?, seq=6 WHERE key='b2'", ("장애 원인은 메모리", ti.ng_text("장애 원인은 메모리")), 6)
        env, err = self.search("회의 일정", "--hybrid")
        self.assertTrue(env["hybrid"]["stale"])
        self.assertIn("behind the index", err)
        self.embed()
        self.assertFalse(self.search("회의 일정", "--hybrid")[0]["hybrid"]["stale"])
        with contextlib.closing(sqlite3.connect(dense.sidecar_path(self.root, "dt"))) as con:
            con.execute("UPDATE meta SET v='{}' WHERE k='src_stamp'")         # an index of another version
            con.commit()
        self.assertTrue(self.search("회의 일정", "--hybrid")[0]["hybrid"]["stale"])

    def test_a_keyword_hit_in_an_overlap_stays_a_hit(self):
        fill = "".join(chr(0xAC00 + (i * 37) % 9000) for i in range(846))      # unrelated bigrams
        common.index_path(self.root, SPEC).unlink()
        build_index(self.root, [("x1", "R", T0, fill), ("k", "R", T0 + 60, "배포 완료했어요 여러분 감사합니다 오늘도 수고"),
                                ("j", "R", T0 + 120, "배포 일정 공유"), ("y", "R", T0 + 180, "점심 먹으러 가요"),
                                ("o1", "O", T0 + 50 * H, "주간 보고 작성")])
        self.embed()
        self.assertEqual(self.side("SELECT count(*) FROM chunk_rec WHERE rec_key='k'"), [(2,)])   # [x1, k], [k, j, y]
        env, _ = self.search("배포", "--hybrid")
        src = {r["key"]: r["sources"] for r in env["rows"]}
        self.assertEqual((src["j"]["kw"], src["k"]["kw"]), (1, 2))      # k keeps its own unit, not only j's context
        self.assertNotIn("x1", src)                                      # nor does its other chunk show a stranger

    def test_a_selective_filter_searches_its_own_chunks(self):
        self.embed()
        with mock.patch.object(dense, "SEM_K", 1):                     # the global KNN: the best chunk of all only
            env, _ = self.search("장애 보고서", "--hybrid", "--sender", "A")
            self.assertEqual((env["hybrid"]["retrievers"]["sem"], {r["room"] for r in env["rows"]}), (1, {"A"}))
            with mock.patch.object(dense, "PREFILTER", 1):             # more rows than that: KNN, then the filter
                env, _ = self.search("장애 보고서", "--hybrid", "--sender", "A")
                self.assertEqual((env["hybrid"]["retrievers"]["sem"], env["rows"]), (0, []))

    def test_a_long_row_is_one_hit(self):
        long = "서버 장애 대응 절차 " * 400                                  # several windows of one row
        change(self.root, "INSERT INTO rows(key, room, ts, seq, message, ng) VALUES('c1', 'C', ?, 2, ?, ?)",
               (T0 + 20 * H, long, ti.ng_text(long)), 2)
        self.embed()
        self.assertGreater(len(self.side("SELECT chunk_id FROM chunk_rec WHERE rec_key='c1'")), 2)
        env, _ = self.search("장애 대응 절차", "--hybrid")
        keys = [r["key"] for r in env["rows"]]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertEqual((keys[0], env["rows"][0]["sources"]), ("c1", {"kw": 1, "sem": 1}))

    def test_hybrid_fuses_filters_and_falls_back(self):
        env, err = self.search("회의 일정", "--hybrid")                   # no sidecar yet
        self.assertFalse(env["hybrid"]["available"])
        self.assertIn("manage.py embed dt", env["hybrid"]["reason"])
        self.assertIn("--hybrid unavailable", err)
        self.assertEqual([r["key"] for r in env["rows"]], ["a1"])
        self.embed()
        env, _ = self.search("회의 일정", "--hybrid")
        h = env["hybrid"]
        self.assertTrue(h["available"])
        self.assertEqual((h["model"], h["dims"], h["rrf_k"], h["vec_seq"], h["stale"]), ("fake", DIMS, 60, 1, False))
        top = env["rows"][0]
        self.assertEqual((top["key"], top["sources"]), ("a1", {"kw": 1, "sem": 1}))
        self.assertAlmostEqual(top["rrf"], 2 / 61, places=6)
        self.assertEqual([(c["key"], c["hit"]) for c in top["context"]], [("a1", True), ("a2", False)])
        self.assertEqual(env["total"], 1)
        self.assertTrue(all(r["sources"]["kw"] is None for r in env["rows"][1:]))
        # no keyword match ("회의일정" is one phrase): meaning only, no relaxing
        env, _ = self.search("회의일정", "--hybrid")
        self.assertEqual((env["total"], env["relaxed"], env["rows"][0]["key"]), (0, False, "a1"))
        self.assertEqual(env["rows"][0]["sources"], {"kw": None, "sem": 1})
        env, _ = self.search("메뉴추천", "--hybrid")                        # the chunk's row nearest the words
        self.assertEqual((env["rows"][0]["key"], env["rows"][0]["sources"]), ("a2", {"kw": None, "sem": 1}))
        self.assertEqual([(c["key"], c["hit"]) for c in env["rows"][0]["context"]], [("a1", False), ("a2", True)])
        # no row holds both terms: the keyword list relaxes to either, as plain hits do
        env, err = self.search("회의 장애", "--hybrid")
        self.assertEqual((env["total"], env["relaxed"]), (3, True))
        kw = [r for r in env["rows"] if r["sources"]["kw"]]
        self.assertEqual(len(kw), 2)                                        # b1 and b2 share a chunk: one unit
        self.assertEqual({c["key"] for r in kw for c in r["context"]}, {"a1", "a2", "b1", "b2"})
        for r in kw:                                                        # a relaxed list weighs less
            s = r["sources"]
            self.assertAlmostEqual(r["rrf"], dense.RELAXED_KW / (60 + s["kw"]) + (1 / (60 + s["sem"]) if s["sem"] else 0),
                                   places=6)
        self.assertIn("(relaxed)", err)
        # filters and exclusions reach the semantic side through the main index
        env, _ = self.search("회의일정 -점심", "--hybrid")
        self.assertNotIn("a2", {c["key"] for r in env["rows"] for c in r["context"]})
        env, _ = self.search("회의일정", "--hybrid", "--since", "2023-11-15 06:00", "--tz", "UTC")
        self.assertTrue({r["key"] for r in env["rows"]} <= {"b1", "b2"})
        change(self.root, "UPDATE rows SET deleted=1 WHERE key IN ('a1','a2')", (), 1)
        env, _ = self.search("회의일정", "--hybrid")
        self.assertNotIn("a1", {r["key"] for r in env["rows"]})
        # the server gone: keyword hits, and the reason
        with contextlib.closing(sqlite3.connect(dense.sidecar_path(self.root, "dt"))) as con:
            con.execute("UPDATE meta SET v='http://127.0.0.1:9' WHERE k='url'")
            con.commit()
        env, _ = self.search("배포", "--hybrid")
        self.assertFalse(env["hybrid"]["available"])
        self.assertIn("embedding server", env["hybrid"]["reason"])
        self.assertEqual([r["key"] for r in env["rows"]], ["a3"])

    def test_hybrid_usage(self):
        for argv in (["--hybrid", "--mode", "count", "회의"], ["--hybrid", "--since", "2020"],
                     ["--hybrid", "회의", "--before", "1.2.3"]):
            with self.subTest(argv=argv), self.assertRaisesRegex(common.UsageError, "--hybrid"):
                DRIVER.run(parser().parse_args(["--config", str(self.cfg), *argv]))


@unittest.skipUnless(os.environ.get("RAWLAYER_TEST_SQLITE_VEC"), "set RAWLAYER_TEST_SQLITE_VEC to the vec0 DLL")
class RealVec0(unittest.TestCase):

    def test_knn_window_and_delete(self):
        con = sqlite3.connect(":memory:")
        self.addCleanup(con.close)
        with mock.patch.object(dense, "vec_dll", return_value=Path(os.environ["RAWLAYER_TEST_SQLITE_VEC"])):
            dense.Vec0.load(con)
        dense.Vec0.create(con, 4)
        dense.Vec0.insert(con, [(1, [1, 0, 0, 0], 10, 20), (2, [0.9, 0.1, 0, 0], 30, 40), (3, [0, 1, 0, 0], 50, 60)])
        self.assertEqual(dense.Vec0.knn(con, [1, 0, 0, 0], 3), [1, 2, 3])
        self.assertEqual(dense.Vec0.knn(con, [1, 0, 0, 0], 3, since=25), [2, 3])
        self.assertEqual(dense.Vec0.knn(con, [1, 0, 0, 0], 3, until=25), [1])
        self.assertEqual(dense.Vec0.knn(con, [1, 0, 0, 0], 3, ids=[3, 2]), [2, 3])
        self.assertEqual(dense.Vec0.knn(con, [1, 0, 0, 0], 3, since=25, ids=[1, 2]), [2])
        dense.Vec0.delete(con, [1])
        self.assertEqual(dense.Vec0.knn(con, [1, 0, 0, 0], 3), [2, 3])


if __name__ == "__main__":
    unittest.main()
