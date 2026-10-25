# -*- coding: utf-8 -*-
"""rawsearch mattermost: the index built from a real capture ledger (ledgerfix.MatterScenario: genesis, then a run
with an edited post, a root's new version, a late reply, attachment-only, two-file and system posts, a post seen in two
versions within one run and a channel rename, then a run with a deleted reply, a user rename and a channel file that
moved), incremental catch-up == full rebuild, ledger reset / purge manifest / mid-commit / defect handling, the
version gate, the writer lock, every search mode and filter, context, threads, browse paging, exit codes, the
tokenizer regressions end to end and the stale warning. All data is synthesised in code."""
import json
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
from mattermost import search as matter
from mattermost.tests import ledgerfix as L
from mattermost.tests.fixtures import TEST_PARENT, Fixture, mm_file

REPO = conf.PROJECT_DIR
QUERIES = ["10월", "가나다", "api", "메시지", "답글", "수정", "보고서", "\"100%\"", "배포 OR 잡담", "메시지 -잡담", "3시",
           "SQL문", "빌드", "문장", "joined"]
ROWS_COLS = ("post_id, rev, seq, held, channel_id, channel, team, ts, ts_ms, sender, user_id, type, body_from, root_id, "
             "reply_count, file_ids, files, edited, deleted, system, gone, message, ng")


def rs(*argv):
    return L.run_rawsearch(argv)


def ipath(data_root):
    return Path(data_root) / "index" / "mattermost.sqlite"


def sibling(path, suffix):
    """`path` with `suffix` appended to its file name (the .new / .keep / .part files next to a file)."""
    return path.with_name(path.name + suffix)


def ro(data_root):
    con = sqlite3.connect(fsio.ro_uri(ipath(data_root), immutable=False), uri=True)
    con.row_factory = sqlite3.Row
    return con


def dump(data_root):
    """Everything the index derives from the ledger (row ids and timestamps of the build excluded)."""
    con = ro(data_root)
    try:
        out = {"matter": sorted(tuple(r) for r in con.execute(f"SELECT {ROWS_COLS} FROM matter"))}
        for t in ("ch_src", "users", "channels", "counts"):
            out[t] = sorted(tuple(r) for r in con.execute(f"SELECT * FROM {t}"))
        meta = dict(con.execute("SELECT k, v FROM meta").fetchall())
        out["head"] = (meta["head_seq"], meta["head_sha256"], meta["head_run"])
        return out
    finally:
        con.close()


def fts_ok(data_root):
    con = sqlite3.connect(ipath(data_root))
    try:
        con.execute("INSERT INTO matter_fts(matter_fts, rank) VALUES('integrity-check', 1)")
        return True
    except sqlite3.DatabaseError:
        return False
    finally:
        con.close()


def search(cfg, *args):
    rc, out, err = rs("mattermost", "--config", cfg, "--json", *args)
    if rc != 0:
        raise AssertionError(f"rc={rc} err={err}")
    return json.loads(out)


def keys(env):
    return sorted(r["key"] for r in env["rows"])


def ids(env):
    return [r["post_id"] for r in env["rows"]]


def answers(cfg):
    return {q: (keys(search(cfg, "-q", q, "--limit", "100", "--include-deleted", "--include-system")),
                search(cfg, "-q", q, "--mode", "count")["total"]) for q in QUERIES}


def k(pid):
    return f"mm:post:{pid}"


class Scenario(unittest.TestCase):
    """One three-round ledger, indexed incrementally after each round; tests that change anything work on clones."""

    @classmethod
    def setUpClass(cls):
        cls.sc = L.MatterScenario()
        cls.cfg, cls.dr = cls.sc.config, cls.sc.data_root
        cls.reports = []
        cls.dumps = {}
        cls.clones = []
        for rnd in (1, 2, 3):
            assert cls.sc.next_round() == 0
            rc, out, err = rs("index", "--config", cls.cfg, "--source", "mattermost", "--json")
            assert rc == 0, err
            cls.reports.append(json.loads(out)["mattermost"])
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

    def index(self, cfg, *extra, source="mattermost"):
        rc, out, err = rs("index", "--config", cfg, "--source", source, "--json", *extra)
        rep = json.loads(out) if out.strip() else None
        return rc, (rep[source] if rep and source != "all" else rep), err

    def row(self, pid, dr=None):
        con = ro(dr or self.dr)
        try:
            return con.execute("SELECT * FROM matter WHERE post_id=?", (pid,)).fetchone()
        finally:
            con.close()

    def round2_index_with_ledger3(self):
        """A clone whose index stands at seq 2 while its ledger has seq 3."""
        cfg, dr = self.clone()
        led = L.LedgerSeq(dr)
        m3 = led.manifest(3)
        keep = sibling(m3, ".keep")
        shutil.move(m3, keep)
        self.assertEqual(self.index(cfg)[1]["seq"], 2)
        shutil.move(keep, m3)
        return cfg, dr


class TestBuild(Scenario):
    def test_reports(self):
        r1, r2, r3 = self.reports
        self.assertEqual((r1["action"], r1["reason"], r1["seq"], r1["ledger_seq"], r1["records"]),
                         ("rebuilt", "missing", 1, 1, 27))
        self.assertEqual((r2["action"], r2["seq"], r2["manifests"], r2["records"]), ("updated", 2, 1, 11))
        self.assertEqual((r3["action"], r3["seq"], r3["records"], r3["behind"]), ("updated", 3, 6, False))
        self.assertEqual((r3["rows"], r3["deleted"], r3["system"], r3["gone"]), (24, 2, 2, 0))
        self.assertEqual((r3["channels"], r3["users"], r3["skipped_file"]), (4, 3, 3))
        rc, rep, _ = self.index(self.cfg)
        self.assertEqual((rc, rep["action"], rep["manifests"]), (0, "current", 0))
        rc, out, _ = rs("index", "--config", self.cfg, "--source", "mattermost")
        self.assertIn("mattermost: current", out)
        self.assertIn("channels=4", out)


    def test_versions_and_flags(self):
        r = self.row("pa12")                 # the archive held the old and the edited line
        self.assertEqual((r["message"], r["edited"], r["rev"], r["seq"]), ("수정 후 문장", 1, 2, 2))
        r = self.row("pa17")                 # two versions within one run: two puts in one manifest
        self.assertEqual((r["message"], r["edited"], r["rev"], r["seq"]), ("최종 문장", 1, 2, 2))
        r = self.row("pa05")                 # a new version with the same text is not an edit
        self.assertEqual((r["message"], r["edited"], r["reply_count"], r["rev"]), ("rapid 배포 준비", 0, 3, 2))
        r = self.row("pb01")                 # moved to a new channel file: a put, same text, not an edit
        self.assertEqual((r["rev"], r["seq"], r["edited"], r["channel_id"], r["message"]), (2, 3, 0, "cb", "3시에 보자"))
        r = self.row("pa07")                 # deleted, message emptied by the server: the captured text is kept
        self.assertEqual((r["deleted"], r["message"], r["body_from"], r["root_id"], r["edited"]),
                         (1, "배포 스레드 답글 둘", "message", "pa05", 0))
        r = self.row("pb03")                 # archived already deleted
        self.assertEqual((r["deleted"], r["message"]), (1, "지운 메시지 원문"))
        r = self.row("pa10")
        self.assertEqual((r["body_from"], r["message"]), ("attachment", "빌드 알림\n빌드 결과\n테스트 통과\n브랜치 main"))
        r = self.row("pa11")
        self.assertEqual((r["body_from"], r["message"], json.loads(r["files"]), json.loads(r["file_ids"])),
                         ("files", "보고서_최종.pdf", ["보고서_최종.pdf"], ["fa11"]))
        r = self.row("pa15")
        self.assertEqual((r["body_from"], r["message"]), ("files", "회의록.hwp diagram.png"))
        r = self.row("pa08")
        self.assertEqual((r["system"], r["type"], r["deleted"]), (1, "system_join_channel", 0))
        r = self.row("pa09")
        self.assertEqual((r["sender"], r["user_id"]), ("", "u9"))
        r = self.row("pa03")
        self.assertEqual((r["ts"], r["ts_ms"], r["type"], r["body_from"], r["file_ids"], r["files"], r["gone"]),
                         (L.kst(2024, 10, 3, 9, 30), L.kst_ms(2024, 10, 3, 9, 30), "", "message", "", "", 0))


    def test_labels_follow_renames(self):
        con = ro(self.dr)
        try:
            self.assertEqual({r[0] for r in con.execute("SELECT DISTINCT sender FROM matter WHERE user_id='uc'")},
                             {"charlie2"})
            self.assertEqual({tuple(r) for r in con.execute("SELECT DISTINCT channel, team FROM matter "
                                                            "WHERE channel_id='cd'")}, {("webXdev 잡담", "team-a")})
            chans = {r["channel_id"]: r for r in con.execute("SELECT * FROM channels")}
            users = {r["user_id"]: r for r in con.execute("SELECT * FROM users")}
        finally:
            con.close()
        self.assertEqual(sorted(chans), ["ca", "cb", "cc", "cd"])
        ca = chans["ca"]
        self.assertEqual((ca["label"], ca["name"], ca["team"], ca["ch_type"], ca["n"], ca["senders"], ca["n_deleted"],
                          ca["n_system"]), ("개발 채널", "dev", "team-a", "O", 14, 4, 1, 2))
        self.assertEqual((chans["cb"]["n"], chans["cb"]["n_deleted"], chans["cb"]["label"]), (3, 1, "web_dev 공지"))
        self.assertEqual((chans["cc"]["team"], chans["cc"]["ch_type"], chans["cc"]["n"]), ("", "D", 2))
        self.assertEqual(chans["ca"]["first_ts"], L.kst(2024, 9, 2, 10))       # the system post before it: not counted
        self.assertEqual((users["ua"]["username"], users["ua"]["name"], users["ub"]["nickname"]),
                         ("alpha", "Alpha One", "브라보닉"))


    def test_incremental_equals_rebuild(self):
        self.assertTrue(fts_ok(self.dr))
        expect = answers(self.cfg)
        cfg, dr = self.clone()
        rc, rep, _ = self.index(cfg, "--rebuild")
        self.assertEqual((rc, rep["action"], rep["reason"], rep["seq"], rep["manifests"], rep["records"]),
                         (0, "rebuilt", "requested", 3, 3, 44))
        self.assertEqual(dump(dr), self.dumps[3])
        self.assertTrue(fts_ok(dr))
        self.assertEqual(answers(cfg), expect)




class TestSearch(Scenario):
    def test_chunk_units_cover_the_live_posts_in_order(self):
        from rawlayer.search import dense
        con, _meta, _fresh = common.open_search(self.dr, matter.SPEC)
        self.addCleanup(con.close)
        units = {c: matter.DRIVER.chunk_units(con, c) for c in matter.DRIVER.changed_containers(con, 0)}
        live = {r[0] for r in con.execute("SELECT post_id FROM matter WHERE deleted=0 AND system=0 AND message!=''")}
        self.assertEqual(sorted(u[0] for _, us in units.values() for u in us), sorted(live))
        for label, us in units.values():
            self.assertTrue(label or not us)
            self.assertEqual([u[1] for u in us], sorted(u[1] for u in us))
        chunks, _containers = dense.plan(con, matter.DRIVER, 0)
        self.assertTrue(chunks and all(set(v[1]) <= live for v in chunks.values()))

    def test_tokenizer_regressions_end_to_end(self):
        self.assertEqual(keys(search(self.cfg, "10월")), [k("pa01"), k("pa13")])        # '10월' finds '10월에'
        self.assertEqual(keys(search(self.cfg, "3시")), [k("pb01")])
        self.assertEqual(keys(search(self.cfg, "SQL문")), [k("pb02")])
        self.assertEqual(keys(search(self.cfg, "가나다")), [k("pa03")])                  # not '가나 나다'
        self.assertEqual(keys(search(self.cfg, "api")), [k("pa04")])                    # not 'rapid'
        self.assertEqual(keys(search(self.cfg, "rapid")), [k("pa05")])
        self.assertEqual(keys(search(self.cfg, "\"100%\"")), [k("pb04")])

    def test_korean_normalization(self):
        env = search(self.cfg, "회의는")                  # pa01 "회의는 10월에 합니다", pa15 "회의록.hwp ..."
        self.assertEqual((env["normalized_query"], env["relaxed"]), ("회의", False))
        self.assertEqual(ids(env)[0], "pa01")             # the typed form first
        self.assertIn("pa15", ids(env))
        self.assertEqual(env["total"], search(self.cfg, "회의")["total"])
        lit = search(self.cfg, "회의는", "--literal")
        self.assertEqual((ids(lit), lit["normalized_query"]), (["pa01"], None))
        self.assertEqual(search(self.cfg, "회의는", "--mode", "count")["total"], env["total"])
        env = search(self.cfg, "회의는 존재안함")          # no row has both: any of them, flagged
        self.assertEqual((env["relaxed"], env["normalized_query"]), (True, "회의 OR 존재안"))
        self.assertEqual(sorted(ids(env)), ["pa01", "pa15"])
        self.assertEqual(search(self.cfg, "회의는 존재안함", "--literal")["total"], 0)
        self.assertEqual(search(self.cfg, "회의는 존재안함", "--mode", "count")["total"], 0)     # counts never relax

    def test_hidden_and_kept_text(self):
        self.assertEqual(search(self.cfg, "답글 둘", "--mode", "count")["total"], 0)
        env = search(self.cfg, "답글 둘")                  # relaxed to 답글: still not the deleted reply
        self.assertTrue(env["relaxed"])
        self.assertNotIn("pa07", ids(env))
        env = search(self.cfg, "답글 둘", "--include-deleted")
        self.assertEqual((keys(env), env["rows"][0]["deleted"], env["rows"][0]["text"]),
                         ([k("pa07")], True, "배포 스레드 답글 둘"))
        self.assertEqual(search(self.cfg, "지운")["total"], 0)
        self.assertEqual(keys(search(self.cfg, "지운", "--include-deleted")), [k("pb03")])
        self.assertEqual(search(self.cfg, "joined")["total"], 0)
        env = search(self.cfg, "joined", "--include-system")
        self.assertEqual((keys(env), env["rows"][0]["system"], env["rows"][0]["type"]),
                         ([k("pa08")], True, "system_join_channel"))
        self.assertEqual(keys(search(self.cfg, "header", "--include-system")), [k("pa16")])

    def test_versions_search(self):
        for q in ("수정 전", "초안"):          # a replaced text is not searchable, deleted or not
            self.assertEqual(search(self.cfg, q, "--include-deleted", "--include-system", "--mode", "count")["total"],
                             0, q)
        h = search(self.cfg, "최종 문장")["rows"][0]
        self.assertEqual((h["post_id"], h["edited"]), ("pa17", True))
        self.assertEqual(search(self.cfg, "rapid")["rows"][0]["reply_count"], 3)

    def test_attachments_and_file_names(self):
        h = search(self.cfg, "보고서")["rows"][0]
        self.assertEqual((h["post_id"], h["body_from"], h["files"], h["file_ids"]),
                         ("pa11", "files", ["보고서_최종.pdf"], ["fa11"]))
        h = search(self.cfg, "diagram")["rows"][0]
        self.assertEqual((h["post_id"], h["files"]), ("pa15", ["회의록.hwp", "diagram.png"]))
        self.assertEqual(keys(search(self.cfg, "빌드 결과")), [k("pa10")])
        self.assertEqual(keys(search(self.cfg, "브랜치 main")), [k("pa10")])
        self.assertEqual(keys(search(self.cfg, "배포 완료")), [k("pa14")])
        self.assertEqual(search(self.cfg, "빌드")["rows"][0]["body_from"], "attachment")

    def test_hit_fields_and_text_output(self):
        h = search(self.cfg, "가나다")["rows"][0]
        self.assertEqual((h["key"], h["post_id"], h["channel_id"], h["channel"], h["team"], h["sender"], h["user_id"]),
                         (k("pa03"), "pa03", "ca", "개발 채널", "team-a", "alpha", "ua"))
        self.assertEqual((h["date"], h["ts_ms"], h["cursor"], h["root_id"], h["type"], h["edited"], h["deleted"]),
                         ("2024-10-03 09:30", L.kst_ms(2024, 10, 3, 9, 30), f"{L.kst_ms(2024, 10, 3, 9, 30)}.pa03", "",
                          "", False, False))
        h = search(self.cfg, "모르는")["rows"][0]
        self.assertEqual((h["sender"], h["user_id"]), ("", "u9"))
        self.assertEqual(search(self.cfg, "답글 하나")["rows"][0]["root_id"], "pa05")
        rc, out, _ = rs("mattermost", "--config", self.cfg, "가나다")
        self.assertEqual(rc, 0)
        for s in ("alpha", "개발 채널", "post=pa03 root=- channel=ca team=team-a", "[가나다]"):
            self.assertIn(s, out)
        rc, out, _ = rs("mattermost", "--config", self.cfg, "모르는")
        self.assertIn(" | u9 ", out)                       # no user record: the user id is shown
        rc, out, _ = rs("mattermost", "--config", self.cfg, "답글 하나")
        self.assertIn("[reply]", out)
        self.assertIn("root=pa05", out)
        rc, out, _ = rs("mattermost", "--config", self.cfg, "diagram")
        self.assertNotIn("[+", out)                        # the file names are the text: no extra files flag
        rc, out, _ = rs("mattermost", "--config", self.cfg, "배포", "--limit", "1")
        self.assertIn("(showing 1 best)", out)

    def test_count_timeline_senders_channels(self):
        self.assertEqual(search(self.cfg, "10월", "--mode", "count")["total"], 2)
        tl = search(self.cfg, "메시지", "--mode", "timeline")
        self.assertEqual([(r["bucket"], r["n"]) for r in tl["rows"]], [("2024-11", 1), ("2025-01", 2)])
        tl = search(self.cfg, "메시지", "--mode", "timeline", "--limit", "1")
        self.assertEqual((tl["rows"], tl["omitted_buckets"]), ([{"bucket": "2025-01", "n": 2}], 1))
        rc, out, _ = rs("mattermost", "--config", self.cfg, "메시지", "--mode", "timeline", "--limit", "1")
        self.assertIn("1 earlier month(s) omitted", out)
        snd = search(self.cfg, "메시지", "--mode", "senders")
        self.assertEqual([(r["label"], r["user_id"], r["name"], r["nickname"], r["n"]) for r in snd["rows"]],
                         [("u9", "u9", "", "", 1), ("bravo", "ub", "", "브라보닉", 1), ("charlie2", "uc", "Charlie", "", 1)])
        rc, out, _ = rs("mattermost", "--config", self.cfg, "메시지", "--mode", "senders")
        self.assertIn("bravo (브라보닉)", out)
        ch = search(self.cfg, "메시지", "--mode", "channels")
        self.assertEqual([(r["label"], r["channel_id"], r["team"], r["n"]) for r in ch["rows"]],
                         [("개발 채널", "ca", "team-a", 1), ("@bravo", "cc", "", 1), ("webXdev 잡담", "cd", "team-a", 1)])
        rc, out, _ = rs("mattermost", "--config", self.cfg, "메시지", "--mode", "channels")
        self.assertIn("channel=cd", out)

    def test_aggregates_without_query(self):
        self.assertEqual(search(self.cfg, "--channel", "개발", "--mode", "count")["total"], 14)
        self.assertEqual(search(self.cfg, "--channel", "개발", "--mode", "count", "--include-system",
                                "--include-deleted")["total"], 17)
        env = search(self.cfg, "--channel", "ca", "--mode", "senders")
        self.assertEqual(sum(r["n"] for r in env["rows"]), 14)

    def test_json_envelope(self):
        env = search(self.cfg, "10월 -회의")
        for key in ("source", "mode", "query", "fts", "exclude_fts", "weak", "verify", "exclude", "total", "rows",
                    "built_at", "updated_at", "index", "filters", "tz"):
            self.assertIn(key, env)
        self.assertEqual((env["source"], env["mode"], env["query"], env["total"]), ("mattermost", "hits", "10월 -회의", 1))
        self.assertEqual(env["index"], {"seq": 3, "ledger_seq": 3, "stale": False})
        self.assertEqual(env["filters"]["include_system"], False)
        self.assertEqual(env["tz"], "+09:00")
        env = search(self.cfg, '"가나 다라" -마바')          # the separator token is shown as <S>, as in --explain
        self.assertEqual((env["fts"], env["exclude_fts"]), ('"가나 <S> 다라"', '"마바"'))
        self.assertNotIn(common.ti.SEP, json.dumps(env, ensure_ascii=False))

    def test_filters(self):
        self.assertEqual(keys(search(self.cfg, "메시지", "--channel", "개발")), [k("pa09")])
        self.assertEqual(keys(search(self.cfg, "메시지", "--channel", "cc")), [k("pc01")])           # a channel id
        self.assertEqual(keys(search(self.cfg, "메시지", "--channel", "cc", "--channel", "webXdev")),
                         [k("pc01"), k("pd01")])
        self.assertEqual({r["channel_id"] for r in search(self.cfg, "--channel", "notice")["rows"]}, {"cb"})  # URL name
        self.assertEqual({r["channel_id"] for r in search(self.cfg, "--channel", "web_dev")["rows"]}, {"cb"})
        self.assertEqual(search(self.cfg, "--channel", "web%dev")["total"], 0)                     # escaped
        self.assertEqual(search(self.cfg, "메시지", "--channel", "잡담방")["total"], 0)               # renamed away
        env = search(self.cfg, "--team", "team-b")
        self.assertEqual((env["total"], {r["channel_id"] for r in env["rows"]}), (3, {"cb"}))
        self.assertEqual({r["sender"] for r in search(self.cfg, "--sender", "alpha", "--limit", "100")["rows"]},
                         {"alpha"})
        self.assertEqual({r["user_id"] for r in search(self.cfg, "--sender", "브라보닉", "--limit", "100")["rows"]},
                         {"ub"})                                                                  # nickname
        self.assertEqual({r["user_id"] for r in search(self.cfg, "--sender", "Alpha One", "--limit", "100")["rows"]},
                         {"ua"})                                                                  # full name
        self.assertEqual(keys(search(self.cfg, "--sender", "u9")), [k("pa09")])                    # a user id
        self.assertEqual(keys(search(self.cfg, "가나")), [k("pa02"), k("pa03")])
        self.assertEqual(keys(search(self.cfg, "가나", "--since", "2024-10", "--until", "2024-10")), [k("pa03")])
        self.assertEqual(keys(search(self.cfg, "가나", "--until", "2024-09")), [k("pa02")])            # inclusive month
        self.assertEqual(keys(search(self.cfg, "가나", "--since", "2024-09-15", "--until", "2024-09-15")),
                         [k("pa02")])
        self.assertEqual(keys(search(self.cfg, "가나", "--until", "2024-10-03 09:30")), [k("pa02"), k("pa03")])
        self.assertEqual(keys(search(self.cfg, "가나", "--until", "2024-10-03 09:29")), [k("pa02")])
        self.assertEqual(keys(search(self.cfg, "가나", "--until", "2024-10-03 00:29", "--tz", "UTC")), [k("pa02")])
        self.assertEqual(keys(search(self.cfg, "가나", "--until", "2024-10-03 00:30", "--tz", "UTC")),
                         [k("pa02"), k("pa03")])
        # create_at is in ms: --until covers the whole last second (pc02 is at 09:05:59.500)
        self.assertEqual(keys(search(self.cfg, "디엠", "--until", "2025-01-02 09:05")), [k("pc01"), k("pc02")])
        self.assertEqual(keys(search(self.cfg, "디엠", "--until", "2025-01-02 09:04")), [k("pc01")])
        self.assertEqual(keys(search(self.cfg, "디엠", "--since", "2025-01-02 09:05")), [k("pc02")])

    def test_context(self):
        h = search(self.cfg, "수정", "--context", "2")["rows"][0]
        self.assertEqual([c["post_id"] for c in h["context"]], ["pa10", "pa11", "pa12", "pa13", "pa14"])
        self.assertEqual([c["hit"] for c in h["context"]], [False, False, True, False, False])
        h = search(self.cfg, "답글 하나", "--context", "1")["rows"][0]           # channel-wide, not just the thread
        self.assertEqual([c["post_id"] for c in h["context"]], ["pa05", "pa06", "pa09"])     # pa07 deleted: hidden
        h = search(self.cfg, "답글 하나", "--context", "1", "--include-deleted")["rows"][0]
        self.assertEqual([c["post_id"] for c in h["context"]], ["pa05", "pa06", "pa07"])
        h = search(self.cfg, "회의는", "--context", "1")["rows"][0]
        self.assertEqual([c["post_id"] for c in h["context"]], ["pa01", "pa02"])           # pa08 system: hidden
        h = search(self.cfg, "회의는", "--context", "1", "--include-system")["rows"][0]
        self.assertEqual([c["post_id"] for c in h["context"]], ["pa08", "pa01", "pa02"])
        rc, out, _ = rs("mattermost", "--config", self.cfg, "수정", "--context", "1")
        self.assertIn(">>", out)

    def test_thread(self):
        env = search(self.cfg, "--thread", "pa07")         # a deleted reply names its thread; it is hidden itself
        self.assertEqual((env["mode"], env["root_id"], env["root_found"], env["channel"], env["hidden_deleted"]),
                         ("thread", "pa05", True, "개발 채널", 1))
        self.assertEqual(ids(env), ["pa05", "pa06", "pa13"])
        self.assertEqual([r["root"] for r in env["rows"]], [True, False, False])
        env = search(self.cfg, "--thread", "pa05", "--include-deleted")
        self.assertEqual(ids(env), ["pa05", "pa06", "pa07", "pa13"])
        self.assertEqual((env["rows"][2]["deleted"], env["rows"][2]["text"]), (True, "배포 스레드 답글 둘"))
        self.assertEqual(ids(search(self.cfg, "--thread", "pa01")), ["pa01"])          # a post without replies
        rc, out, err = rs("mattermost", "--config", self.cfg, "--thread", "pa06", "배포", "--sender", "alpha", "--mode",
                          "count", "--channel", "cb")
        self.assertEqual(rc, 0)
        self.assertIn("QUERY, --sender, --channel, --mode count not applied", err)
        self.assertIn("1 deleted post(s) hidden", err)
        self.assertIn("thread pa05 in 개발 채널 (3 post(s))", out)
        self.assertIn("↳ reply  #pa13", out)
        self.assertIn("    새 답글 10월에", out)
        rc, out, err = rs("mattermost", "--config", self.cfg, "--thread", "nope")
        self.assertEqual(rc, 2)
        self.assertIn("no post", err)

    def test_browse_paging(self):
        newest = search(self.cfg, "--channel", "ca", "--limit", "5")
        self.assertEqual((newest["mode"], newest["total"], newest["rows"][-1]["post_id"]), ("browse", 14, "pa17"))
        ts = [(r["ts_ms"], r["post_id"]) for r in newest["rows"]]
        self.assertEqual(ts, sorted(ts))                         # displayed ascending
        seen = ids(newest)
        page = newest
        while page["page"]["more_older"]:
            page = search(self.cfg, "--channel", "ca", "--limit", "5", "--before", page["page"]["before"])
            seen = ids(page) + seen
        self.assertEqual((len(seen), len(set(seen)), seen[0]), (14, 14, "pa01"))
        again = search(self.cfg, "--channel", "ca", "--limit", "5", "--after", page["page"]["after"])
        self.assertEqual(again["rows"][0]["post_id"], seen[len(ids(page))])
        rc, out, _ = rs("mattermost", "--config", self.cfg, "--channel", "ca", "--limit", "3")
        self.assertIn("--before", out)
        self.assertIn("#pa17", out)
        q = search(self.cfg, "배포", "--channel", "ca", "--before", newest["page"]["before"], "--limit", "5")
        self.assertEqual(q["mode"], "browse")
        self.assertTrue(all(r["ts_ms"] < newest["rows"][0]["ts_ms"] for r in q["rows"]))
        day = search(self.cfg, "--since", "2025-01-02", "--until", "2025-01-02")
        self.assertEqual(ids(day), ["pc01", "pc02"])

    def test_list_channels(self):
        env = search(self.cfg, "--list-channels")
        self.assertEqual([(r["channel_id"], r["label"], r["team"], r["type"], r["n"]) for r in env["rows"]],
                         [("ca", "개발 채널", "team-a", "O", 14), ("cb", "web_dev 공지", "team-b", "O", 3),
                          ("cc", "@bravo", "", "D", 2), ("cd", "webXdev 잡담", "team-a", "P", 1)])
        self.assertEqual(ids_of(search(self.cfg, "--list-channels", "--channel", "web_dev")), ["cb"])
        self.assertEqual(ids_of(search(self.cfg, "--list-channels", "--team", "team-a")), ["ca", "cd"])
        self.assertEqual(ids_of(search(self.cfg, "--list-channels", "--channel", "chat")), ["cd"])    # URL name
        rc, out, _ = rs("mattermost", "--config", self.cfg, "--list-channels")
        self.assertIn("id=ca", out)
        self.assertIn("[team-a]", out)

    def test_explain(self):
        rc, out, _ = rs("mattermost", "--config", self.cfg, "--explain", "\"가나 나다\"")
        self.assertEqual(rc, 0)
        self.assertIn("# fts5 index expr", out)
        self.assertNotIn(chr(0xE000), out)
        self.assertIn("<S>", out)
        rc, out, err = rs("mattermost", "--config", self.cfg, "--explain", "\"가 나\"")
        self.assertIn("(none -- full scan)", out)
        self.assertIn("full scan", err)

    def test_usage_errors(self):
        for argv in (["\"unbalanced"], ["-q", "-x"], ["a", "-q", "b"], [], ["x", "--limit", "0"],
                     ["--channel", "ca", "--before", "nope"], ["x", "--before", "1.a", "--after", "1.b"],
                     ["x", "--mode", "count", "--before", "1.a"], ["x", "--since", "yesterday"], ["x", "--tz", "KST"],
                     ["owner:x"], ["--bogus"], ["x", "--mode", "rooms"], ["--thread", "nope"],
                     ["--thread", "pa05", "--list-channels"], ["--thread", " "]):
            rc, _out, err = rs("mattermost", "--config", self.cfg, *argv)
            self.assertEqual(rc, 2, argv)
            self.assertTrue(err.startswith("search:"), argv)
        self.assertEqual(rs("mattermost", "--config", self.cfg, "--help")[0], 0)


def ids_of(env):
    return [r["channel_id"] for r in env["rows"]]


class TestEdges(unittest.TestCase):
    def test_missing_index_and_empty_ledger(self):
        base = Path(tempfile.mkdtemp(prefix="rsx-", dir=TEST_PARENT))
        try:
            dr = base / "data"
            dr.mkdir(parents=True)
            cfg = base / "config.json"
            with cfg.open("w", encoding="utf-8") as fh:
                json.dump({"v": 1, "data_root": str(dr)}, fh)
            rc, _out, err = rs("mattermost", "--config", cfg, "x")
            self.assertEqual(rc, 2)
            self.assertIn("manage.py collect", err)
            rc, out, _ = rs("index", "--config", cfg, "--source", "mattermost", "--json")
            rep = json.loads(out)["mattermost"]
            self.assertEqual((rc, rep["action"], rep["seq"], rep["rows"], rep["channels"]), (0, "rebuilt", 0, 0, 0))
            env = search(cfg, "가나다")
            self.assertEqual((env["total"], env["index"]["stale"]), (0, False))
            self.assertEqual(search(cfg, "--list-channels")["rows"], [])
            self.assertEqual(rs("index", "--config", cfg, "--source", "mattermost")[0], 0)
        finally:
            L.force_rmtree(base)

    def test_odd_values_are_counted_not_raised(self):
        fx = Fixture()
        try:
            posts = [
                L.mm_post("po1", "cx", "uz", 5, None, props="x", user_id=None, reply_count="x"),
                L.mm_post("po2", "cx", "uz", "정상 메시지", "1700000000000", type=7, file_ids="x",
                          metadata={"files": [1, {"name": 7}, mm_file("fo2", "po2", "cx", name="ok.txt")]}),
                L.mm_post("po3", "cx", "uz", "", 1_700_000_100_000, delete_at="1", edit_at="x"),
                L.mm_post("po4", "cx", "uz", "채널 없는 글", 1_700_000_200_000, channel_id=7),
                L.mm_post("po5", "cx", "uz", "뿌리 없는 답글", 1_700_000_300_000, root="pmissing"),
            ]
            L.MatterSources(fx).write({"cx": {"display": "", "name": "only-name", "path": "o/t/x__cx.jsonl",
                                              "posts": posts}},
                                      {"uz": {"username": None, "first_name": 3, "last_name": "", "nickname": ""}})
            self.assertEqual(L.capture(fx, ["mm_archive"]), 0)
            rc, out, err = rs("index", "--config", fx.config_path, "--source", "mattermost", "--json")
            self.assertEqual(rc, 0, err)
            rep = json.loads(out)["mattermost"]
            self.assertEqual(rep["rows"], 5)
            self.assertGreaterEqual(rep["body_other"], 1)
            self.assertEqual((rep["no_channel"], rep["ts_unknown"]), (1, 1))
            con = ro(fx.data_root)
            rows = {r["post_id"]: r for r in con.execute("SELECT * FROM matter")}
            chan = con.execute("SELECT label, n FROM channels WHERE channel_id='cx'").fetchone()
            con.close()
            self.assertEqual((rows["po1"]["ts"], rows["po1"]["message"], rows["po1"]["user_id"],
                              rows["po1"]["reply_count"], rows["po1"]["body_from"]), (0, "", "", 0, ""))
            self.assertEqual((rows["po2"]["ts"], rows["po2"]["type"], rows["po2"]["file_ids"],
                              json.loads(rows["po2"]["files"])), (1_700_000_000, "", "", ["ok.txt"]))
            self.assertEqual((rows["po3"]["deleted"], rows["po3"]["edited"]), (1, 0))
            self.assertEqual((rows["po4"]["channel_id"], rows["po4"]["channel"]), ("", ""))
            self.assertEqual(tuple(chan), ("only-name", 3))         # po1 po2 po5 (po3 deleted, po4 has no channel)
            env = search(fx.config_path, "정상")
            self.assertEqual((env["rows"][0]["sender"], env["rows"][0]["channel"]), ("", "only-name"))
            self.assertEqual(search(fx.config_path, "--list-channels")["total"], 2)       # cx and the '' channel
            env = search(fx.config_path, "--thread", "po5")          # a reply whose root was never archived
            self.assertEqual((env["root_id"], env["root_found"], ids(env)), ("pmissing", False, ["po5"]))
            rc, _out, err = rs("mattermost", "--config", fx.config_path, "--thread", "po5")
            self.assertIn("root post is not in the index", err)
        finally:
            L.force_rmtree(fx.base)


class TestApplier(unittest.TestCase):
    """The fold on hand-made records: stale revs, purge, a post moving channel, records capture never writes."""

    def setUp(self):
        self.con = self.fresh(full=False)
        self.app = matter.SPEC.applier(self.con, full=False)

    def tearDown(self):
        self.con.close()

    @staticmethod
    def fresh(full):
        con = sqlite3.connect(":memory:", isolation_level=None)
        con.executescript(common.META_DDL)
        matter.SPEC.create_tables(con)
        if not full:
            matter.SPEC.create_triggers(con)
        return con

    @staticmethod
    def rec(key, rev, op="put", native=None, kind=None, origin="mm_archive", ts_ms=None):
        return {"v": 1, "kind": kind or key.split(":")[1], "origin": origin, "key": key, "rev": rev, "op": op,
                "native": native, "derived": {} if op == "put" else None, "ts_ms": ts_ms}

    def row(self, con=None):
        return (con or self.con).execute("SELECT held, gone, message, deleted, channel_id, edited FROM matter").fetchone()

    def test_purge_absent_stale_and_bad_records(self):
        key = "mm:post:p1"
        self.app.record(self.rec(key, 1, native={"message": "원문 텍스트", "channel_id": "c1", "create_at": 1}), 1)
        self.app.end_manifest(1)
        self.assertEqual(self.row(), (1, 0, "원문 텍스트", 0, "c1", 0))
        self.app.record(self.rec(key, 1, native={"message": "다른 것"}), 2)               # stale rev
        for bad in (None, [], self.rec(key, 2, origin="edb"), self.rec(key, 2, op="erase"), self.rec(key, True),
                    self.rec("mm:post:p1", 2, kind="file"), self.rec("mm:thing:x", 1, kind="thing"),
                    self.rec("mm:ch:c1", 1, native="not a dict"), self.rec("mm:file:f1", 1, native={"name": "a"})):
            self.app.record(bad, 2)
        self.app.end_manifest(2)
        counts = common.read_counts(self.con)
        self.assertEqual((counts["stale_rev"], counts["bad_record"], counts["bad_key"]), (1, 5, 2))
        self.assertEqual((counts["bad_native"], counts["skipped_file"], counts["ts_unknown"]), (1, 1, 1))
        self.app.record(self.rec(key, 2, op="absent"), 3)
        self.app.end_manifest(3)
        self.assertEqual(self.row(), (0, 1, "원문 텍스트", 0, "c1", 0))
        self.app.record(self.rec(key, 3, op="purged"), 4)
        self.app.end_manifest(4)
        self.assertEqual(self.row(), (0, 1, "", 0, "c1", 0))
        self.con.execute("INSERT INTO matter_fts(matter_fts, rank) VALUES('integrity-check', 1)")
        self.assertEqual(self.con.execute("SELECT n, n_gone FROM channels WHERE channel_id='c1'").fetchone(), (1, 1))

    def test_moved_channel_matches_full_build(self):
        recs = [(1, self.rec("mm:post:p1", 1, native={"message": "옮길 글", "channel_id": "cx"})),
                (1, self.rec("mm:post:p2", 1, native={"message": "남는 글", "channel_id": "cy"})),
                (2, self.rec("mm:post:p1", 2, native={"message": "옮길 글", "channel_id": "cy"})),
                (2, self.rec("mm:user:u1", 1, native={"username": "kilo"}))]
        full = self.fresh(full=True)
        fapp = matter.SPEC.applier(full, full=True)
        for app, con in ((self.app, self.con), (fapp, full)):
            for seq in (1, 2):
                for s, r in recs:
                    if s == seq:
                        app.record(r, seq)
                app.end_manifest(seq)
        fapp.finish()
        for t in ("channels", "users", "ch_src"):
            self.assertEqual(sorted(self.con.execute(f"SELECT * FROM {t}")), sorted(full.execute(f"SELECT * FROM {t}")), t)
        self.assertEqual(sorted(self.con.execute(f"SELECT {ROWS_COLS} FROM matter")),
                         sorted(full.execute(f"SELECT {ROWS_COLS} FROM matter")))
        self.assertEqual([r[0] for r in self.con.execute("SELECT channel_id FROM channels")], ["cy"])
        full.close()

    def test_edit_detection(self):
        key = "mm:post:p1"
        self.app.record(self.rec(key, 1, native={"message": "같은 글", "reply_count": 1}), 1)
        self.app.record(self.rec(key, 2, native={"message": "  같은 글 ", "reply_count": 2}), 1)     # same text
        self.app.end_manifest(1)
        self.assertEqual(self.row()[5], 0)
        self.app.record(self.rec(key, 3, native={"message": ""}), 2)          # no text: the kept one stays
        self.app.end_manifest(2)
        self.assertEqual(self.row()[2::3], ("같은 글", 0))
        self.app.record(self.rec(key, 4, native={"message": "다른 글"}), 3)
        self.app.end_manifest(3)
        self.assertEqual(self.row()[2::3], ("다른 글", 1))
        self.app.record(self.rec("mm:post:p2", 1, native={"message": "그대로", "edit_at": 5}), 4)   # edited, same text
        self.app.end_manifest(4)
        self.assertEqual(self.con.execute("SELECT edited FROM matter WHERE post_id='p2'").fetchone()[0], 1)

    def test_flags(self):
        self.assertEqual(matter.DRIVER.flags(
            {"files": ["a", "b"], "body_from": "message", "root_id": "r", "edited": True}),
            " [reply] [edited] [+2 files]")
        self.assertEqual(matter.DRIVER.flags(
            {"files": ["a"], "body_from": "files", "deleted": True, "system": True}),
            " [deleted] [system]")
        self.assertEqual(matter.DRIVER.parse_cursor(" 1727758980123.abc ", "--before"), (1727758980123, "abc"))

    def test_post_text(self):
        pt = matter.post_text
        self.assertEqual(pt({"message": "  본문  "}), ("본문", "message"))
        self.assertEqual(pt({"message": " ", "props": {"attachments": [
            "x", {"pretext": "a", "title": "a", "text": 3, "fallback": "b",
                  "fields": [{"title": "t", "value": 5}, {"title": None, "value": ""}, "y"]}]}}),
            ("a\nb\nt 5", "attachment"))
        self.assertEqual(pt({"message": "", "_files": [{"name": "slim.txt"}, {"name": ""}]}), ("slim.txt", "files"))
        self.assertEqual(pt({"metadata": {"files": [{"name": "a.png"}, {"name": "b.png"}]}}), ("a.png b.png", "files"))
        self.assertEqual(pt({"props": [], "metadata": "x"}), ("", ""))
        c = matter.Counter()
        self.assertEqual(pt({"message": ["x"]}, c), ("", ""))
        self.assertEqual(c["body_other"], 1)


if __name__ == "__main__":
    unittest.main()
