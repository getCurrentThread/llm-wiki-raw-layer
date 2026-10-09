# -*- coding: utf-8 -*-
"""rawsearch kakaotalk: the index built from a real capture ledger (ledgerfix.KakaoScenario: genesis, then a run
with an edit, a delete-for-all tombstone, a new message and two vanished edb rows, then the run that writes their
`absent` records), incremental catch-up == full rebuild, ledger reset / purge manifest / mid-commit / defect
handling, the version gate, the writer lock, every search mode and filter, context, browse paging, exit codes, the
tokenizer regressions end to end and the stale warning. All data is synthesised in code."""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from rawlayer import conf
from rawlayer.capture import fsio
from rawlayer.search import common
from kakaotalk import search as kakao
from rawlayer.search import textindex as ti
from kakaotalk.tests import ledgerfix as L
from kakaotalk.tests.edbfix import sidecar
from kakaotalk.tests.fixtures import TEST_PARENT, Fixture

REPO = conf.PROJECT_DIR
EDB, MACDB, BACKUP = (1 << kakao.RANK[o] for o in kakao.PRECEDENCE)
QUERIES = ["10월", "가나다", "api", "메시지", "edb 메시지", "수정", "삭제될", "\"100%\"", "라이브 OR 잡담", "메시지 -edb",
           "사라질", "SQL문", "3시"]


def rs(*argv):
    return L.run_rawsearch(argv)


def ipath(data_root):
    return Path(data_root) / "index" / "kakaotalk.sqlite"


def ro(data_root):
    con = sqlite3.connect(fsio.ro_uri(ipath(data_root), immutable=False), uri=True)
    con.row_factory = sqlite3.Row
    return con


def dump(data_root):
    """Everything the index derives from the ledger (row ids and timestamps of the build excluded)."""
    con = ro(data_root)
    try:
        out = {
            "kakao": sorted(tuple(r) for r in con.execute(
                "SELECT chat_id, log_id, log_no, ts, room, sender, author_id, type, origins, source, edited, "
                "deleted, gone, message, ng FROM kakao")),
            "ksrc": sorted(tuple(r) for r in con.execute(
                "SELECT k.chat_id, k.log_id, s.origin, s.rev, s.seq, s.held, s.th, s.deleted, s.ts, s.author_id, "
                "s.type, s.edited, s.has_text FROM ksrc s JOIN kakao k ON k.id = s.mid")),
        }
        for t in ("texts", "room_src", "authors", "rooms", "counts"):
            out[t] = sorted(tuple(r) for r in con.execute(f"SELECT * FROM {t}"))
        meta = dict(con.execute("SELECT k, v FROM meta").fetchall())
        out["head"] = (meta["head_seq"], meta["head_sha256"], meta["head_run"])
        return out
    finally:
        con.close()


def fts_ok(data_root):
    con = sqlite3.connect(ipath(data_root))
    try:
        con.execute("INSERT INTO kakao_fts(kakao_fts, rank) VALUES('integrity-check', 1)")
        return True
    except sqlite3.DatabaseError:
        return False
    finally:
        con.close()


def search(cfg, *args):
    rc, out, err = rs("kakaotalk", "--config", cfg, "--json", *args)
    if rc != 0:
        raise AssertionError(f"rc={rc} err={err}")
    return json.loads(out)


def keys(env):
    return sorted(r["key"] for r in env["rows"])


def answers(cfg):
    return {q: (keys(search(cfg, "-q", q, "--limit", "100", "--include-deleted")),
                search(cfg, "-q", q, "--mode", "count")["total"]) for q in QUERIES}


def k(chat, log):
    return f"kakao:{chat}:msg:{log}"


class Scenario(unittest.TestCase):
    """One three-round ledger, indexed incrementally after each round; tests that change anything work on clones."""

    @classmethod
    def setUpClass(cls):
        cls.sc = L.KakaoScenario()
        cls.cfg, cls.dr = cls.sc.config, cls.sc.data_root
        cls.reports = []
        cls.dumps = {}
        cls.clones = []
        for rnd in (1, 2, 3):
            assert cls.sc.next_round() == 0
            rc, out, err = rs("index", "--config", cls.cfg, "--source", "kakaotalk", "--json")
            assert rc == 0, err
            cls.reports.append(json.loads(out)["kakaotalk"])
            cls.dumps[rnd] = dump(cls.dr)
            if rnd == 2:
                cls.round2 = cls.clone()

    @classmethod
    def tearDownClass(cls):
        for base in cls.clones:
            L.force_rmtree(base)
        cls.sc.cleanup()

    @classmethod
    def clone(cls, src=None):
        base, cfg, dr = L.clone_data_root(src or cls.dr)
        cls.clones.append(base)
        return cfg, dr

    def index(self, cfg, *extra):
        rc, out, err = rs("index", "--config", cfg, "--source", "kakaotalk", "--json", *extra)
        return rc, (json.loads(out)["kakaotalk"] if out.strip() else None), err

    def row(self, chat, log, dr=None):
        con = ro(dr or self.dr)
        try:
            return con.execute("SELECT * FROM kakao WHERE chat_id=? AND log_id=?", (str(chat), str(log))).fetchone()
        finally:
            con.close()


class TestBuild(Scenario):
    def test_reports(self):
        r1, r2, r3 = self.reports
        self.assertEqual((r1["action"], r1["reason"], r1["seq"], r1["ledger_seq"]), ("rebuilt", "missing", 1, 1))
        self.assertEqual((r2["action"], r2["seq"], r2["manifests"]), ("updated", 2, 1))
        self.assertEqual((r3["action"], r3["seq"], r3["behind"]), ("updated", 3, False))
        self.assertEqual(r3["rows"], 48)
        self.assertEqual((r3["deleted"], r3["gone"]), (2, 1))
        self.assertGreaterEqual(r3["skipped_hist"], 1)
        self.assertGreaterEqual(r3["skipped_schema"], 1)
        rc, rep, _ = self.index(self.cfg)
        self.assertEqual((rc, rep["action"], rep["manifests"]), (0, "current", 0))


    def test_origins_text_and_flags(self):
        r = self.row(101, 1)
        self.assertEqual((r["origins"], r["source"], r["message"]), (BACKUP, "backup_decrypted", "회의는 10월에 합니다"))
        self.assertEqual((r["sender"], r["room"], r["author_id"]), ("Alpha", "스터디 방", "11"))
        edited = self.row(301, 5)
        self.assertEqual((edited["message"], edited["edited"], edited["deleted"]), ("수정 후 문장", 1, 0))
        tomb = self.row(301, 6)            # deleted for everyone: the base64 body is never the text
        self.assertEqual((tomb["deleted"], tomb["message"], tomb["type"]), (1, "곧 삭제될 메시지", 1 | 0x4000))
        self.assertNotIn("qujdrevgr0g", tomb["ng"])
        gone = self.row(301, 7)
        self.assertEqual((gone["gone"], gone["origins"], gone["message"]), (1, 0, "사라질 메시지 칠"))
        both = self.row(301, 8)            # edb's absent cleared its bit; the backup still holds the key
        self.assertEqual((both["gone"], both["origins"], both["source"]), (0, BACKUP, "backup_decrypted"))
        self.assertEqual(self.row(301, 9)["origins"], EDB | BACKUP)
        local = self.row(201, 3)           # deleted locally and emptied in the backup; the edb room keeps the text
        self.assertEqual((local["deleted"], local["message"], local["source"], local["origins"]),
                         (1, "지운 메시지 원문", "edb", EDB | BACKUP))
        photo = self.row(101, 7)
        self.assertEqual((photo["message"], photo["type"], photo["ng"]), ("", 2, ""))
        big = self.row(101, L.BIG)
        self.assertEqual((big["log_id"], big["log_no"], big["sender"], big["author_id"]), (str(L.BIG), L.BIG, "", "15"))


    def test_labels_follow_renames(self):
        con = ro(self.dr)
        try:
            names = {r[0] for r in con.execute("SELECT DISTINCT sender FROM kakao WHERE author_id='14'")}
            self.assertEqual(names, {"Delta Two"})        # the newest name, on edb rows too
            self.assertEqual({r[0] for r in con.execute("SELECT DISTINCT room FROM kakao WHERE chat_id='401'")},
                             {"Renamed 방"})
            self.assertEqual({r[0] for r in con.execute("SELECT DISTINCT room FROM kakao WHERE chat_id='601'")},
                             {"개명된 리스트 방"})          # the client's chat list renamed it in round 2
            rooms = {r["chat_id"]: r for r in con.execute("SELECT * FROM rooms")}
        finally:
            con.close()
        self.assertEqual(sorted(rooms), ["101", "201", "301", "401", "501", "601"])
        self.assertEqual((rooms["101"]["n"], rooms["101"]["senders"]), (8, 4))
        self.assertEqual((rooms["301"]["n"], rooms["301"]["n_deleted"], rooms["301"]["n_gone"]), (30, 1, 1))
        self.assertEqual(rooms["201"]["title"], "web_dev 채널")

    def test_chat_list_titled_room(self):
        """Room 601 exists only as an encrypted edb room: its title comes from the client's chat-list database,
        and the round-2 rename there re-titled every row."""
        self.assertEqual(search(self.cfg, "--room", "리스트", "--mode", "count")["total"], 2)
        env = search(self.cfg, "인사", "--room", "개명된")
        self.assertEqual(keys(env), [k(601, 1), k(601, 2)])
        self.assertEqual({r["room"] for r in env["rows"]}, {"개명된 리스트 방"})
        rc, out, _ = rs("kakaotalk", "--config", self.cfg, "--list-rooms")
        self.assertIn("개명된 리스트 방", out)


    def test_incremental_equals_rebuild(self):
        self.assertTrue(fts_ok(self.dr))
        expect = answers(self.cfg)
        cfg, dr = self.clone()
        rc, rep, _ = self.index(cfg, "--rebuild")
        self.assertEqual((rc, rep["action"], rep["reason"], rep["seq"], rep["manifests"]), (0, "rebuilt", "requested", 3, 3))
        self.assertEqual(dump(dr), self.dumps[3])
        self.assertTrue(fts_ok(dr))
        self.assertEqual(answers(cfg), expect)




class TestSearch(Scenario):
    def test_tokenizer_regressions_end_to_end(self):
        self.assertEqual(keys(search(self.cfg, "10월")), [k(101, 1), k(301, 31)])     # '10월' finds '10월에'
        self.assertEqual(keys(search(self.cfg, "3시")), [k(201, 1)])
        self.assertEqual(keys(search(self.cfg, "SQL문")), [k(201, 2)])
        self.assertEqual(keys(search(self.cfg, "가나다")), [k(101, 3)])                # not '가나 나다'
        self.assertEqual(keys(search(self.cfg, "api")), [k(101, 4)])                  # not 'rapid'
        self.assertEqual(keys(search(self.cfg, "\"100%\"")), [k(101, 6)])

    def test_hidden_and_never_indexed(self):
        self.assertEqual(search(self.cfg, "삭제될")["total"], 0)
        env = search(self.cfg, "삭제될", "--include-deleted")
        self.assertEqual(([r["key"] for r in env["rows"]], env["rows"][0]["deleted"]), ([k(301, 6)], True))
        self.assertEqual(search(self.cfg, "QUJDREVGR0g", "--include-deleted")["total"], 0)
        self.assertEqual(search(self.cfg, "수정 전", "--include-deleted")["total"], 0)
        gone = search(self.cfg, "사라질")["rows"][0]
        self.assertEqual((gone["gone"], gone["origins"]), (True, []))
        local = search(self.cfg, "지운", "--include-deleted")["rows"][0]
        self.assertEqual((local["deleted"], local["source"]), (True, "edb"))

    def test_hit_fields_and_text_output(self):
        h = search(self.cfg, "큰 번호")["rows"][0]
        self.assertEqual((h["log_id"], h["chat_id"], h["key"], h["sender"], h["author_id"]),
                         (str(L.BIG), "101", k(101, L.BIG), "", "15"))
        self.assertEqual(h["cursor"], f"{h['ts']}.{L.BIG}.{h['id']}")
        self.assertEqual(h["origins"], ["backup_decrypted"])
        self.assertEqual(h["date"], "2024-11-08 10:00")
        rc, out, _ = rs("kakaotalk", "--config", self.cfg, "가나다")
        self.assertEqual(rc, 0)
        self.assertIn("=== kakaotalk: 1 match(es)", out)        # the banner and the hit prefix name the app label
        self.assertIn("[kakaotalk] ", out)
        self.assertIn("Alpha", out)
        self.assertIn("스터디 방", out)
        self.assertIn("chat=101 log=3", out)
        self.assertIn("[가나다]", out)
        rc, out, _ = rs("kakaotalk", "--config", self.cfg, "큰 번호")
        self.assertIn(f"log={L.BIG}", out)
        self.assertIn(" | 15 ", out)                       # no name known: the author id is shown

    def test_ranking_then_recency(self):
        env = search(self.cfg, "메시지", "--limit", "50")
        self.assertEqual(env["total"], len(env["rows"]))
        rows = search(self.cfg, "edb", "--limit", "50")["rows"]
        ts = [r["ts"] for r in rows]
        self.assertEqual(ts, sorted(ts, reverse=True))     # equal bm25 -> newest first

    def test_count_timeline_senders_rooms(self):
        self.assertEqual(search(self.cfg, "10월", "--mode", "count")["total"], 2)
        tl = search(self.cfg, "메시지", "--mode", "timeline")
        self.assertEqual([(r["bucket"], r["n"]) for r in tl["rows"]], [("2024-11", 1), ("2024-12", 28), ("2025-01", 2)])
        tl = search(self.cfg, "메시지", "--mode", "timeline", "--limit", "1")
        self.assertEqual((tl["rows"], tl["omitted_buckets"]), ([{"bucket": "2025-01", "n": 2}], 2))
        rc, out, _ = rs("kakaotalk", "--config", self.cfg, "메시지", "--mode", "timeline", "--limit", "1")
        self.assertIn("2 earlier month(s) omitted", out)
        tl = search(self.cfg, "메시지", "--mode", "timeline", "--tz", "UTC")
        self.assertEqual(tl["tz"], "+00:00")
        snd = search(self.cfg, "edb 메시지", "--mode", "senders")
        self.assertEqual([(r["label"], r["author_id"], r["n"]) for r in snd["rows"]],
                         [("Alpha", "11", 13), ("Delta Two", "14", 13)])
        rooms = search(self.cfg, "메시지", "--mode", "rooms")
        self.assertEqual([(r["label"], r["chat_id"], r["n"]) for r in rooms["rows"]],
                         [("Edb 방 301", "301", 28), ("스터디 방", "101", 1), ("Renamed 방", "401", 1),
                          ("webXdev 잡담", "501", 1)])
        rc, out, _ = rs("kakaotalk", "--config", self.cfg, "메시지", "--mode", "rooms")
        self.assertIn("chat=301", out)

    def test_aggregates_without_query(self):
        env = search(self.cfg, "--room", "스터디", "--mode", "count")
        self.assertEqual(env["total"], 8)
        env = search(self.cfg, "--room", "301", "--mode", "senders")
        self.assertEqual(sum(r["n"] for r in env["rows"]), 30)

    def test_json_envelope(self):
        env = search(self.cfg, "10월 -회의")
        for key in ("source", "mode", "query", "fts", "exclude_fts", "weak", "verify", "exclude", "total", "rows",
                    "built_at", "index", "filters", "tz"):
            self.assertIn(key, env)
        self.assertEqual((env["source"], env["mode"], env["query"], env["total"]), ("kakaotalk", "hits", "10월 -회의", 1))
        self.assertEqual(env["index"], {"seq": 3, "ledger_seq": 3, "stale": False})
        self.assertIsInstance(env["rows"][0]["log_id"], str)
        env = search(self.cfg, '"가나 다라" -마바')          # the separator token is shown as <S>, as in --explain
        self.assertEqual((env["fts"], env["exclude_fts"]), ('"가나 <S> 다라"', '"마바"'))
        self.assertNotIn(common.ti.SEP, json.dumps(env, ensure_ascii=False))

    def test_filters(self):
        self.assertEqual(keys(search(self.cfg, "메시지", "--room", "스터디")), [k(101, L.BIG)])
        self.assertEqual(search(self.cfg, "메시지", "--room", "301")["total"], 28)            # a chat id
        self.assertEqual(search(self.cfg, "메시지", "--room", "301", "--room", "Renamed")["total"], 29)
        self.assertEqual({r["chat_id"] for r in search(self.cfg, "--room", "web_dev")["rows"]}, {"201"})
        self.assertEqual(search(self.cfg, "--room", "web%dev")["total"], 0)                    # escaped
        self.assertEqual(search(self.cfg, "메시지", "--room", "Live 방")["total"], 0)           # renamed away
        self.assertEqual({r["sender"] for r in search(self.cfg, "--sender", "Alpha", "--limit", "100")["rows"]},
                         {"Alpha"})
        self.assertEqual(keys(search(self.cfg, "--sender", "15")), [k(101, L.BIG)])            # an author id
        self.assertEqual(keys(search(self.cfg, "가나")), [k(101, 2), k(101, 3)])
        self.assertEqual(keys(search(self.cfg, "가나", "--since", "2024-10", "--until", "2024-10")), [k(101, 3)])
        self.assertEqual(keys(search(self.cfg, "가나", "--until", "2024-09")), [k(101, 2)])      # inclusive month
        self.assertEqual(keys(search(self.cfg, "가나", "--since", "2024-09-15", "--until", "2024-09-15")), [k(101, 2)])
        self.assertEqual(keys(search(self.cfg, "가나", "--until", "2024-10-03 09:30")), [k(101, 2), k(101, 3)])
        self.assertEqual(keys(search(self.cfg, "가나", "--until", "2024-10-03 09:29")), [k(101, 2)])
        self.assertEqual(keys(search(self.cfg, "가나", "--until", "2024-10-03 00:29", "--tz", "UTC")), [k(101, 2)])
        self.assertEqual(keys(search(self.cfg, "가나", "--until", "2024-10-03 00:30", "--tz", "UTC")),
                         [k(101, 2), k(101, 3)])

    def test_context(self):
        h = search(self.cfg, "수정", "--context", "2")["rows"][0]
        self.assertEqual([c["log_id"] for c in h["context"]], ["3", "4", "5", "7", "8"])   # 6 is deleted: hidden
        self.assertEqual([c["hit"] for c in h["context"]], [False, False, True, False, False])
        h = search(self.cfg, "수정", "--context", "2", "--include-deleted")["rows"][0]
        self.assertEqual([c["log_id"] for c in h["context"]], ["3", "4", "5", "6", "7"])
        rc, out, _ = rs("kakaotalk", "--config", self.cfg, "수정", "--context", "1")
        self.assertIn(">>", out)
        first = search(self.cfg, "--room", "301", "--before", "1733000000.1", "--limit", "1")   # nothing older
        self.assertEqual(first["rows"], [])

    def test_browse_paging(self):
        newest = search(self.cfg, "--room", "301", "--limit", "10")
        self.assertEqual(newest["mode"], "browse")
        self.assertEqual(newest["total"], 30)
        self.assertEqual(newest["rows"][-1]["log_id"], "31")
        ts = [(r["ts"], int(r["log_id"])) for r in newest["rows"]]
        self.assertEqual(ts, sorted(ts))                         # displayed ascending
        seen = [r["log_id"] for r in newest["rows"]]
        page = newest
        while page["page"]["more_older"]:
            page = search(self.cfg, "--room", "301", "--limit", "10", "--before", page["page"]["before"])
            seen = [r["log_id"] for r in page["rows"]] + seen
        self.assertEqual(len(seen), 30)
        self.assertEqual(len(set(seen)), 30)
        self.assertEqual(seen[0], "1")
        again = search(self.cfg, "--room", "301", "--limit", "10", "--after", page["page"]["after"])
        self.assertEqual(again["rows"][0]["log_id"], seen[10])
        rc, out, _ = rs("kakaotalk", "--config", self.cfg, "--room", "301", "--limit", "3")
        self.assertIn("--before", out)
        self.assertIn("#31", out)
        q = search(self.cfg, "edb", "--room", "301", "--before", newest["page"]["before"], "--limit", "5")
        self.assertEqual(q["mode"], "browse")
        self.assertTrue(all(r["ts"] < newest["rows"][0]["ts"] for r in q["rows"]))

    def test_list_rooms(self):
        env = search(self.cfg, "--list-rooms")
        self.assertEqual([(r["chat_id"], r["label"], r["n"]) for r in env["rows"]],
                         [("301", "Edb 방 301", 30), ("101", "스터디 방", 8), ("401", "Renamed 방", 3),
                          ("201", "web_dev 채널", 2), ("601", "개명된 리스트 방", 2), ("501", "webXdev 잡담", 1)])
        env = search(self.cfg, "--list-rooms", "--room", "web_dev")
        self.assertEqual([r["chat_id"] for r in env["rows"]], ["201"])
        rc, out, _ = rs("kakaotalk", "--config", self.cfg, "--list-rooms")
        self.assertIn("id=301", out)

    def test_explain(self):
        rc, out, _ = rs("kakaotalk", "--config", self.cfg, "--explain", "\"가나 나다\"")
        self.assertEqual(rc, 0)
        self.assertIn("# fts5 index expr", out)
        self.assertNotIn(chr(0xE000), out)
        self.assertIn("<S>", out)
        rc, out, err = rs("kakaotalk", "--config", self.cfg, "--explain", "\"가 나\"")
        self.assertIn("(none -- full scan)", out)
        self.assertIn("full scan", err)

    def test_usage_errors(self):
        for argv in (["\"unbalanced"], ["-q", "-x"], ["a", "-q", "b"], [], ["x", "--limit", "0"],
                     ["--room", "301", "--before", "nope"], ["x", "--before", "1.1", "--after", "1.2"],
                     ["x", "--mode", "count", "--before", "1.1"], ["x", "--since", "yesterday"], ["x", "--tz", "KST"],
                     ["owner:x"], ["--bogus"]):
            rc, _out, err = rs("kakaotalk", "--config", self.cfg, *argv)
            self.assertEqual(rc, 2, argv)
            self.assertTrue(err.startswith("search:"), argv)
        self.assertEqual(rs("kakaotalk", "--config", self.cfg, "--help")[0], 0)
        self.assertEqual(rs("index", "--config", Path(TEST_PARENT) / "no-such-config.json")[0], 2)


class TestEdges(unittest.TestCase):
    def test_missing_index_and_empty_ledger(self):
        base = Path(tempfile.mkdtemp(prefix="rsx-", dir=TEST_PARENT))
        try:
            dr = base / "data"
            dr.mkdir(parents=True)
            cfg = base / "config.json"
            with cfg.open("w", encoding="utf-8") as fh:
                json.dump({"v": 1, "data_root": str(dr)}, fh)
            rc, _out, err = rs("kakaotalk", "--config", cfg, "x")
            self.assertEqual(rc, 2)
            self.assertIn("manage.py collect", err)
            rc, out, _ = rs("index", "--config", cfg, "--source", "kakaotalk", "--json")
            rep = json.loads(out)["kakaotalk"]
            self.assertEqual((rc, rep["action"], rep["seq"], rep["rows"]), (0, "rebuilt", 0, 0))
            env = search(cfg, "가나다")
            self.assertEqual((env["total"], env["index"]["stale"]), (0, False))
            self.assertEqual(rs("index", "--config", cfg, "--source", "kakaotalk")[0], 0)
        finally:
            L.force_rmtree(base)

    def test_data_root_outside_parent_refused(self):
        base = Path(tempfile.mkdtemp(prefix="rsx-", dir=TEST_PARENT))
        try:
            cfg = base / "config.json"
            with cfg.open("w", encoding="utf-8") as fh:
                json.dump({"v": 1, "data_root": str(REPO)}, fh)
            self.assertEqual(rs("index", "--config", cfg)[0], 2)
        finally:
            L.force_rmtree(base)

    def test_odd_values_are_counted_not_raised(self):
        fx = Fixture()
        try:
            src = L.KakaoSources(fx)
            src.room_db(101, [L.Msg(101, 1, "1700000000", None, b"\x00\x01", typ="x"),
                              L.Msg(101, 2, None, 5, "정상 메시지"),
                              L.Msg(101, 3, 1700000000, 6, None, typ=None, deleted="1")])
            src.manifest({})
            self.assertEqual(L.capture(fx, ["backup_decrypted"]), 0)
            rc, out, err = rs("index", "--config", fx.config_path, "--source", "kakaotalk", "--json")
            self.assertEqual(rc, 0, err)
            rep = json.loads(out)["kakaotalk"]
            self.assertEqual(rep["rows"], 3)
            con = ro(fx.data_root)
            rows = {r["log_id"]: r for r in con.execute("SELECT * FROM kakao")}
            con.close()
            self.assertEqual((rows["1"]["ts"], rows["1"]["message"], rows["1"]["type"]), (1700000000, "", None))
            self.assertEqual((rows["2"]["ts"], rows["2"]["sender"], rows["2"]["message"]), (0, "", "정상 메시지"))
            self.assertEqual(rows["3"]["deleted"], 1)
            env = search(fx.config_path, "정상")
            self.assertEqual((env["rows"][0]["date"], env["rows"][0]["sender"], env["rows"][0]["room"]), ("?", "", ""))
        finally:
            L.force_rmtree(fx.base)

    def test_browse_pages_through_rows_that_share_time_and_log_id(self):
        """Review finding: the same logId in two rooms at the same second (one (ts, log_no) for several rows). The
        cursor carries the row id too, so walking pages either way sees every row exactly once."""
        fx = Fixture()
        try:
            ts = 1_700_000_000
            msgs = [L.Msg(1, n, ts, 5, f"같은 시각 {n}") for n in range(1, 7)] + \
                   [L.Msg(2, n, ts, 6, f"같은 시각 {n}") for n in range(1, 4)]
            src = L.KakaoSources(fx)
            src.room_db(1, [m for m in msgs if m.chat == 1])
            src.room_db(2, [m for m in msgs if m.chat == 2])
            src.manifest({1: "가 방", 2: "나 방"})
            self.assertEqual(L.capture(fx, ["backup_decrypted"]), 0)
            self.assertEqual(rs("index", "--config", fx.config_path, "--source", "kakaotalk")[0], 0)
            base = ("--since", "2020", "--limit", "2")
            page = search(fx.config_path, *base)
            seen = [h["key"] for h in page["rows"]]
            while page["page"]["more_older"]:
                page = search(fx.config_path, *base, "--before", page["page"]["before"])
                seen = [h["key"] for h in page["rows"]] + seen
            self.assertEqual((len(seen), len(set(seen)), page["total"]), (9, 9, 9))
            fwd = []
            page = search(fx.config_path, *base, "--after", "0.0.0")
            while True:
                fwd += [h["key"] for h in page["rows"]]
                if not page["page"]["more_newer"]:
                    break
                page = search(fx.config_path, *base, "--after", page["page"]["after"])
            self.assertEqual(fwd, seen)
            # a two-part cursor (older pages printed <ts>.<logId>) keeps its meaning: every row of that
            # (ts, n) is past it
            self.assertEqual(search(fx.config_path, *base, "--before", f"{ts}.3")["page"]["after"].split(".")[:2],
                             [str(ts), "2"])
            self.assertEqual(search(fx.config_path, *base, "--after", f"{ts}.3")["rows"][0]["log_id"], "4")
        finally:
            L.force_rmtree(fx.base)

    def test_applier_error_is_reported_and_leaves_no_index(self):
        """Review finding: an unexpected error in the kakaotalk update (here a sqlite error in the applier) is
        reported for kakaotalk (exit 1) and leaves no index behind; the next run builds it. (That another app's
        index is still built is a core rule: tests/core/test_search_driver.py.)"""
        fx = Fixture().standard()
        try:
            from rawlayer.registry import origin_order  # noqa: PLC0415
            self.assertEqual(L.capture(fx, origin_order()), 0)
            with mock.patch.object(kakao.KakaoApplier, "finish",
                                   side_effect=sqlite3.OperationalError("disk I/O error")):
                rc, out, err = rs("index", "--config", fx.config_path, "--json")
            self.assertEqual(rc, 1, err)
            rep = json.loads(out)
            self.assertEqual((rep["kakaotalk"]["action"], rep["kakaotalk"]["reason"]),
                             ("failed", "error OperationalError: disk I/O error"))
            self.assertFalse(ipath(fx.data_root).exists())
            self.assertFalse(sidecar(ipath(fx.data_root), ".new").exists())
            rc, out, _ = rs("index", "--config", fx.config_path)
            self.assertEqual(rc, 0)
            self.assertIn("kakaotalk: rebuilt", out)
        finally:
            L.force_rmtree(fx.base)


class TestApplier(unittest.TestCase):
    """The fold on hand-made records: stale revs, purge, and records of shapes capture never writes."""

    def setUp(self):
        self.con = sqlite3.connect(":memory:", isolation_level=None)
        self.con.executescript(common.META_DDL)
        kakao.SPEC.create_tables(self.con)
        kakao.SPEC.create_triggers(self.con)
        self.app = kakao.SPEC.applier(self.con, full=False)

    def tearDown(self):
        self.con.close()

    def rec(self, origin, key, rev, op="put", native=None, derived=None, ts_ms=None, kind="msg"):
        return {"v": 1, "kind": kind, "origin": origin, "key": key, "rev": rev, "op": op, "native": native,
                "derived": derived, "ts_ms": ts_ms}

    def row(self):
        return self.con.execute("SELECT origins, gone, message, deleted FROM kakao").fetchone()

    def test_purge_stale_and_bad_records(self):
        key = "kakao:9:msg:1"
        self.app.record(self.rec("backup_decrypted", key, 1, native={"message": "원문 텍스트", "type": 1}), 1)
        self.app.end_manifest(1)
        self.assertEqual(self.row(), (BACKUP, 0, "원문 텍스트", 0))
        self.app.record(self.rec("backup_decrypted", key, 1, native={"message": "다른 것"}), 2)  # stale rev
        for bad in (None, [], {"origin": "nowhere", "key": key, "rev": 1, "op": "put"},
                    self.rec("backup_decrypted", "kakao:x:msg:1", 1), self.rec("backup_decrypted", key, True),
                    self.rec("backup_decrypted", key, 2, op="erase"),
                    self.rec("edb", "kakao:9", 1, kind="room", derived=7),
                    self.rec("edb", key, 1, native="not a dict"), self.rec("macdb", "kakao:9:msg:2", 1,
                                                                           native={"message": 5, "type": 2 ** 70})):
            self.app.record(bad, 2)
        self.app.end_manifest(2)
        counts = common.read_counts(self.con)
        self.assertEqual(counts["stale_rev"], 1)
        self.assertEqual(counts["bad_record"], 5)
        self.assertEqual(counts["bad_key"], 1)
        self.assertEqual((counts["bad_native"], counts["body_other"]), (1, 1))
        self.app.record(self.rec("backup_decrypted", key, 2, op="purged"), 3)
        self.app.end_manifest(3)
        origins, gone, message, _d = self.row()
        self.assertEqual((origins & BACKUP, message), (0, ""))
        self.con.execute("INSERT INTO kakao_fts(kakao_fts, rank) VALUES('integrity-check', 1)")

    def test_derive_precedence(self):
        S = kakao._Src
        d = kakao.derive([S(kakao.RANK["backup_decrypted"], 1, b"m", 0, 10, "1", 1, 0, 1),
                          S(kakao.RANK["edb"], 1, None, 1, 20, "1", 0x4001, 0, 0),
                          S(kakao.RANK["macdb"], 0, b"l", 0, 30, None, 1, 1, 1)])
        self.assertEqual((d["th"], d["source"], d["ts"], d["deleted"], d["edited"], d["gone"], d["type"]),
                         (b"m", "backup_decrypted", 20, 1, 1, 0, 0x4001))
        self.assertEqual(d["origins"], EDB | BACKUP)
        d = kakao.derive([S(kakao.RANK["edb"], 0, b"e", 0, 0, None, None, 0, 1)])
        self.assertEqual((d["gone"], d["th"], d["source"], d["origins"]), (1, b"e", "edb", 0))
        # a holder's OLD text (its latest put had none) does not outrank a lower holder's current text ...
        d = kakao.derive([S(kakao.RANK["edb"], 1, b"old", 1, 20, "1", 1, 0, 0),
                          S(kakao.RANK["backup_decrypted"], 1, b"new", 0, 20, "1", 1, 1, 1)])
        self.assertEqual((d["th"], d["source"]), (b"new", "backup_decrypted"))
        # ... but with no current text anywhere, the best holder's last text is kept, before a non-holder's
        d = kakao.derive([S(kakao.RANK["macdb"], 0, b"gone", 0, 20, "1", 1, 0, 0),
                          S(kakao.RANK["backup_decrypted"], 1, b"kept", 1, 20, "1", 1, 0, 0)])
        self.assertEqual((d["th"], d["source"]), (b"kept", "backup_decrypted"))

    def test_tombstoned_origin_does_not_hide_the_current_text(self):
        """Review finding: edb and backup_decrypted both put '약속은 3시'; then the backup puts an
        edited text and edb a local-delete row with no text. The row shows (and the index finds) the edited
        text, from the backup."""
        key = "kakao:9:msg:5"
        for origin in ("edb", "backup_decrypted"):
            self.app.record(self.rec(origin, key, 1, native={"message": "약속은 3시", "type": 1}), 1)
        self.app.end_manifest(1)
        self.app.record(self.rec("backup_decrypted", key, 2, native={"message": "약속은 4시로 변경", "type": 1}), 2)
        self.app.record(self.rec("edb", key, 2, native={"message": "", "type": 1, "deleted": 1}), 2)
        self.app.end_manifest(2)
        r = self.con.execute("SELECT message, source, origins, edited, deleted FROM kakao").fetchone()
        self.assertEqual(r, ("약속은 4시로 변경", "backup_decrypted", EDB | BACKUP, 1, 1))
        q = ti.build_fts_query("변경")
        frm, where, params = ti.sql_parts(q, "kakao", "kakao_fts", alias="t")
        ti.register_functions(self.con)
        self.assertEqual(self.con.execute(f"SELECT count(*) FROM {frm} WHERE {' AND '.join(where)}",
                                          params).fetchone()[0], 1)
        # the same ledger through a full build gives the same row
        con = sqlite3.connect(":memory:", isolation_level=None)
        con.executescript(common.META_DDL)
        kakao.SPEC.create_tables(con)
        app = kakao.SPEC.applier(con, full=True)
        for seq, recs in ((1, [self.rec(o, key, 1, native={"message": "약속은 3시", "type": 1})
                               for o in ("edb", "backup_decrypted")]),
                          (2, [self.rec("backup_decrypted", key, 2, native={"message": "약속은 4시로 변경", "type": 1}),
                               self.rec("edb", key, 2, native={"message": "", "type": 1, "deleted": 1})])):
            for rec in recs:
                app.record(rec, seq)
            app.end_manifest(seq)
        app.finish()
        self.assertEqual(con.execute("SELECT message, source, origins, edited, deleted FROM kakao").fetchone(), r)
        con.close()


if __name__ == "__main__":
    unittest.main()
