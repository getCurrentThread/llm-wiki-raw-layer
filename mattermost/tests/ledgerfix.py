# -*- coding: utf-8 -*-
"""Synthetic ledgers for the mattermost search tests, made by running the real capture pipeline (capture.run) on inputs
synthesised in code (mattermost/tests/fixtures.py), so the index sees exactly the record shapes capture writes. Every
name, title and text below is invented.

Helpers: tests/core/appfix.py (capture, run_rawsearch, clone_data_root, force_rmtree, kst, LedgerSeq).
Inputs: MatterSources (the mm_archive layout) and MatterScenario (three capture rounds).
"""
import json
import os
from pathlib import Path

from mattermost.tests import fixtures as F
from mattermost.tests.fixtures import TEST_PARENT, Fixture   # importing it points capture.paths.DATA_PARENT at %TEMP%
from tests.core import appfix

kst = appfix.kst
preserved_env = appfix.preserved_env
capture = appfix.capture
run_rawsearch = appfix.run_rawsearch
force_rmtree = appfix.force_rmtree
LedgerSeq = appfix.LedgerSeq
_force = appfix._force


def clone_data_root(src_data_root):
    return appfix.clone_data_root(src_data_root, TEST_PARENT)


# ------------------------------------------------------------------------------------------------ mattermost inputs

def kst_ms(y, mo, d, h=12, mi=0):
    return kst(y, mo, d, h, mi) * 1000


class MatterSources:
    """Writes the mm_archive input of a Fixture as mattertok lays it out: manifest.json (channels with a path),
    posts/<path> (CRLF JSON lines, ensure_ascii=False) and users.json. Each call replaces the archive; post
    files no longer referenced are removed (a channel file that moved)."""

    def __init__(self, fx):
        self.fx = fx

    def write(self, channels, users):
        root = Path(self.fx.mm)
        man = {"version": 1, "channels": {}}
        keep = set()
        for cid, c in channels.items():
            posts = c["posts"]
            man["channels"][cid] = {
                "channel_id": cid, "type": c.get("type", "O"), "team_name": c.get("team", ""),
                "channel_name": c["name"], "display": c["display"], "inaccessible_before": 0, "oldest_cursor": "",
                "newest_post_id": "", "newest_create_at": 0, "backfill_done": True,
                "post_count": len({p["id"] for p in posts}), "expected": 0, "error": "", "path": c["path"]}
            p = root.joinpath("posts", *c["path"].split("/"))
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes("".join(json.dumps(x, ensure_ascii=False) + "\r\n" for x in posts).encode("utf-8"))
            keep.add(os.path.normcase(p))
        for dp, _d, fs in (root / "posts").walk():
            for f in fs:
                p = dp / f
                if os.path.normcase(p) not in keep:
                    p.unlink()
        with (root / "manifest.json").open("w", encoding="utf-8") as fh:
            json.dump(man, fh, ensure_ascii=False)
        with (root / "users.json").open("w", encoding="utf-8") as fh:
            json.dump(users, fh, ensure_ascii=False)


U_ALPHA, U_BRAVO, U_CHARLIE, U_NOBODY = "ua", "ub", "uc", "u9"      # u9 has no users.json entry


def mm_post(pid, cid, user, msg, create_at, **extra):
    """A full-format post line (post() of mattermost/tests/fixtures.py) by `user` at `create_at` (ms)."""
    return F.post(pid, cid, msg=msg, create_at=create_at, user=user, **extra)


class MatterScenario:
    """Channels (ids, names and texts are made up):
      ca "개발 채널"     team-a, open: Korean / Latin / digit texts; a thread (root pa05, replies pa06 pa07); a system
                         post pa08; a post by a user with no users.json entry pa09; an attachment-only post pa10; a
                         file-only post pa11
      cb "web_dev 공지"  team-b, open: '3시에', 'SQL문을', '100%', and pb03 archived already deleted (delete_at > 0)
      cc "@bravo"        a DM (no team)
      cd "잡담방"         team-a, private; renamed "webXdev 잡담" in round 2 (a label an unescaped LIKE '_' matches)
    Round 2: the archive holds pa12's old line and its edited line (edit_at > 0); the root pa05 gets a new version
      (reply_count 3, same text); a late reply pa13; an attachment-only post pa14; a two-file post pa15; a system
      post pa16; pa17 appears twice in the same run (a draft line, then its edited line); cd renamed.
    Round 3: a version of reply pa07 with delete_at > 0 and an emptied message; user uc renamed; cb's file moves
      to a new path (the same posts in a new container)."""

    def __init__(self):
        self.fx = Fixture()
        self.src = MatterSources(self.fx)
        self.rounds = 0

    @property
    def config(self):
        return self.fx.config_path

    @property
    def data_root(self):
        return self.fx.data_root

    def cleanup(self):
        force_rmtree(self.fx.base)

    @staticmethod
    def channel_a(rnd):
        a, b, c, n = U_ALPHA, U_BRAVO, U_CHARLIE, U_NOBODY
        root = kst_ms(2024, 11, 5, 16)
        posts = [
            mm_post("pa01", "ca", a, "회의는 10월에 합니다", kst_ms(2024, 9, 2, 10)),
            mm_post("pa02", "ca", b, "가나 나다 라마", kst_ms(2024, 9, 15, 11)),
            mm_post("pa03", "ca", a, "가나다라 테스트", kst_ms(2024, 10, 3, 9, 30)),
            mm_post("pa04", "ca", c, "API 키 발급 완료", kst_ms(2024, 10, 20, 14)),
            mm_post("pa05", "ca", b, "rapid 배포 준비", root, reply_count=2),
            mm_post("pa06", "ca", a, "배포 스레드 답글 하나", root + 600_000, root="pa05"),
            mm_post("pa07", "ca", c, "배포 스레드 답글 둘", root + 1_200_000, root="pa05"),
            mm_post("pa08", "ca", a, "alpha joined the channel.", kst_ms(2024, 9, 1, 9), type="system_join_channel"),
            mm_post("pa09", "ca", n, "모르는 사람 메시지", kst_ms(2024, 11, 6, 8)),
            mm_post("pa10", "ca", b, "", kst_ms(2024, 11, 7, 10), props={"attachments": [
                {"pretext": "빌드 알림", "title": "빌드 결과", "text": "테스트 통과", "fallback": "빌드 결과",
                 "fields": [{"title": "브랜치", "value": "main"}, {"title": "", "value": ""}]}]}),
            mm_post("pa11", "ca", c, "", kst_ms(2024, 11, 8, 10),
                    files=[F.mm_file("fa11", "pa11", "ca", name="보고서_최종.pdf")]),
            mm_post("pa12", "ca", a, "수정 전 문장", kst_ms(2024, 12, 1, 9)),
        ]
        if rnd >= 2:
            posts += [
                mm_post("pa05", "ca", b, "rapid 배포 준비", root, reply_count=3),
                mm_post("pa12", "ca", a, "수정 후 문장", kst_ms(2024, 12, 1, 9), edit_at=kst_ms(2024, 12, 1, 9, 5)),
                mm_post("pa13", "ca", b, "새 답글 10월에", kst_ms(2024, 12, 2, 9), root="pa05"),
                mm_post("pa14", "ca", a, "", kst_ms(2024, 12, 3, 9), props={"attachments": [
                    {"title": "배포 완료 알림", "text": "버전 2 배포"}]}),
                mm_post("pa15", "ca", b, "", kst_ms(2024, 12, 4, 9), files=[
                    F.mm_file("fa15", "pa15", "ca", name="회의록.hwp"),
                    F.mm_file("fb15", "pa15", "ca", name="diagram.png")]),
                mm_post("pa16", "ca", c, "charlie updated the channel header", kst_ms(2024, 12, 5, 9),
                        type="system_header_change"),
                mm_post("pa17", "ca", a, "초안 문장", kst_ms(2024, 12, 6, 9)),
                mm_post("pa17", "ca", a, "최종 문장", kst_ms(2024, 12, 6, 9), edit_at=kst_ms(2024, 12, 6, 9, 1)),
            ]
        if rnd >= 3:
            posts.append(mm_post("pa07", "ca", c, "", root + 1_200_000, root="pa05",
                                 delete_at=kst_ms(2024, 12, 9, 9)))
        return posts

    @staticmethod
    def channel_b():
        a, b, c = U_ALPHA, U_BRAVO, U_CHARLIE
        return [
            mm_post("pb01", "cb", b, "3시에 보자", kst_ms(2024, 10, 10, 15)),
            mm_post("pb02", "cb", a, "SQL문을 고쳤어", kst_ms(2024, 10, 11, 18)),
            mm_post("pb03", "cb", c, "지운 메시지 원문", kst_ms(2024, 10, 12, 20), delete_at=kst_ms(2024, 10, 13, 1)),
            mm_post("pb04", "cb", a, "100% 출석 체크", kst_ms(2024, 10, 13, 8)),
        ]

    def channels(self, rnd):
        return {
            "ca": {"display": "개발 채널", "name": "dev", "team": "team-a", "path": "o/team-a/dev__ca.jsonl",
                   "posts": self.channel_a(rnd)},
            "cb": {"display": "web_dev 공지", "name": "notice", "team": "team-b", "posts": self.channel_b(),
                   "path": "o/team-b/notice__cb.jsonl" if rnd < 3 else "o/team-b/notice-moved__cb.jsonl"},
            "cc": {"display": "@bravo", "name": "ua__ub", "type": "D", "path": "d/bravo__cc.jsonl", "posts": [
                mm_post("pc01", "cc", U_BRAVO, "디엠 메시지 하나", kst_ms(2025, 1, 2, 9)),
                mm_post("pc02", "cc", U_ALPHA, "디엠 답장", kst_ms(2025, 1, 2, 9, 5) + 59_500)]},   # last second of 09:05
            "cd": {"display": "잡담방" if rnd < 2 else "webXdev 잡담", "name": "chat", "team": "team-a", "type": "P",
                   "path": "p/team-a/chat__cd.jsonl",
                   "posts": [mm_post("pd01", "cd", U_CHARLIE, "잡담 메시지", kst_ms(2025, 1, 4, 20))]},
        }

    @staticmethod
    def users(rnd):
        return {U_ALPHA: {"username": "alpha", "first_name": "Alpha", "last_name": "One", "nickname": ""},
                U_BRAVO: {"username": "bravo", "first_name": "", "last_name": "", "nickname": "브라보닉"},
                U_CHARLIE: {"username": "charlie" if rnd < 3 else "charlie2", "first_name": "Charlie",
                            "last_name": "", "nickname": ""}}

    def next_round(self):
        """Write the next round's archive and capture mm_archive. Returns capture's exit code."""
        self.rounds += 1
        self.src.write(self.channels(self.rounds), self.users(self.rounds))
        return capture(self.fx, ["mm_archive"])
