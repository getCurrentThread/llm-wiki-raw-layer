"""서버 접속 없이 도는 자체 점검. `python selfcheck.py` 로 실행.

Mattermost 의 두 가지 함정(이중 JSON 인코딩, 중복/시스템 메시지 필터)이
회귀하지 않았는지 확인하는 게 핵심이다.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import os
import sys

from mattertok.auth import Session
from mattertok.cli import build_parser, parse_since
from mattertok.client import ApiError, MattermostClient
from mattertok.config import load_settings
from mattertok.stream import MattermostStream, _Seen

MY_ID = "me____________________aaa"
CHANNEL = "chan__________________aaa"


def make_frame(post_id: str, user_id: str, message: str, post_type: str = "",
               channel_display: str = "공지사항") -> dict:
    return {
        "event": "posted",
        "data": {
            "channel_display_name": channel_display,
            "channel_name": "town-square",
            "channel_type": "O",
            "sender_name": "@hong",
            "team_id": "team__________________aaa",
            # data.post 는 dict 가 아니라 JSON '문자열'로 온다. 이게 핵심 함정.
            "post": json.dumps(
                {
                    "id": post_id,
                    "create_at": 1754006400000,
                    "user_id": user_id,
                    "channel_id": CHANNEL,
                    "message": message,
                    "root_id": "",
                    "type": post_type,
                }
            ),
        },
        "broadcast": {},
        "seq": 7,
    }


class FakeWS:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.inbox = [
            json.dumps({"status": "OK", "seq_reply": 1}),
            json.dumps({"event": "hello", "data": {"server_version": "11.7.2"}}),
        ]

    async def send(self, payload: str) -> None:
        self.sent.append(json.loads(payload))

    async def recv(self) -> str:
        return self.inbox.pop(0)


def main() -> int:
    checks: list[tuple[str, bool]] = []

    def check(name: str, ok: bool) -> None:
        checks.append((name, bool(ok)))

    os.environ.setdefault("MM_URL", "https://mattermost.example.com")  # 접속하지 않음(가짜 클라이언트)
    settings = load_settings()
    check("설정 로딩", settings.base_url.startswith("http"))
    check("WebSocket URL 파생", settings.ws_url.startswith(("wss://", "ws://")) and settings.ws_url.endswith("/api/v4/websocket"))

    check("--since 상대시간", parse_since("2h") is not None and parse_since("3d") < parse_since("2h"))
    check("--since 절대날짜", parse_since("2026-08-01") is not None)
    check("--since 미지정", parse_since(None) is None)

    parser = build_parser()
    ns = parser.parse_args(["read", "#공지", "-n", "50", "-s", "2h"])
    check("인자 파싱", ns.channel == "#공지" and ns.number == 50 and ns.since == "2h")

    seen = _Seen(maxlen=3)
    check("중복 제거", [seen.add(x) for x in ["a", "b", "a", "c"]] == [True, True, False, True])

    session = Session(
        base_url=settings.base_url, token="x" * 26, user_id=MY_ID,
        username="me", obtained_at=0, source="login",
    )
    client = MattermostClient(session)
    try:
        stream = MattermostStream(client)

        frame = make_frame("post__________________aaa", "other_________________bbb", "실시간 메시지\n둘째 줄")
        msg = stream._parse_posted(frame)
        check("이중 인코딩 해제", msg is not None and msg.message == "실시간 메시지\n둘째 줄")
        check("발신자/채널 추출", msg is not None and msg.sender_name == "hong" and msg.channel_name == "공지사항")
        check("같은 메시지 재수신 무시", stream._parse_posted(frame) is None)
        check(
            "시스템 메시지 제외",
            stream._parse_posted(
                make_frame("post__________________bbb", "x", "참여했습니다", "system_join_channel")
            ) is None,
        )
        check(
            "내 메시지 기본 제외",
            stream._parse_posted(make_frame("post__________________ccc", MY_ID, "내 글")) is None,
        )

        own_stream = MattermostStream(client, include_own=True)
        check(
            "--include-own 시 포함",
            own_stream._parse_posted(make_frame("post__________________ddd", MY_ID, "내 글")) is not None,
        )

        filtered = MattermostStream(client, channel_ids=["다른채널________________"])
        check("채널 필터", filtered._parse_posted(make_frame("post__________________eee", "z", "무시")) is None)

        # ---- 채널 제외 정책 (2026-08-02 MM_EXCLUDE_CHANNELS 패턴 정책) ----
        from mattertok.config import is_excluded_channel

        PAT = ("피드백",)  # 테스트용 명시 패턴(기본값은 비어 있음)

        check("제외: 표시 이름", is_excluded_channel("홍길동(피드백)", None, patterns=PAT))
        check("제외: slug 에만 있어도", is_excluded_channel("Team", "team-피드백-2026", patterns=PAT))
        check("제외: 무관한 채널은 통과 안 함", not is_excluded_channel("1. 공지사항", "town-square", patterns=PAT))
        check("제외: None 안전", not is_excluded_channel(None, None, patterns=PAT))
        check("제외: 빈 패턴이면 제외 없음",
              not is_excluded_channel("홍길동(피드백)", patterns=()))
        check(
            "제외: 실시간 수신에서도 걸린다",
            MattermostStream(client, exclude=PAT)._parse_posted(
                make_frame("post__________________fff", "z", "피드백 내용",
                           channel_display="홍길동(피드백)")
            ) is None,
        )
        check(
            "제외: 정책 해제 시 통과",
            MattermostStream(client, exclude=())._parse_posted(
                make_frame("post__________________ggg", "z", "피드백 내용",
                           channel_display="홍길동(피드백)")
            ) is not None,
        )

        ws = FakeWS()
        asyncio.run(stream._authenticate(ws))
        sent = ws.sent[0]
        check(
            "WebSocket 인증 프레임",
            sent["action"] == "authentication_challenge" and sent["seq"] == 1 and sent["data"]["token"] == session.token,
        )

        # ---- 아카이브 ----
        from mattertok.archive import Archiver, ChannelState, Manifest, safe_name, slim_post

        check("파일명 정리 (금지문자)", safe_name('a/b:c*d?"e<f>g|h') == "a_b_c_d__e_f_g_h")
        check("파일명 정리 (예약어)", safe_name("CON").startswith("_"))
        check("파일명 정리 (길이)", len(safe_name("가" * 200, limit=40)) <= 40)
        check("파일명 정리 (빈 값)", safe_name("   ", "fallback") == "fallback")

        archiver = Archiver(client, __import__("pathlib").Path("."), rate=0)
        dm = ChannelState(
            channel_id="chan__________________aaa", display="@someone",
            channel_name=MY_ID + "__other_________________bbb", type="D",
        )
        dm_path = archiver._path_for(dm)
        check("DM 경로에 raw name 미사용", MY_ID not in dm_path and "@someone" in dm_path)
        check("DM 경로 길이 안전", len(dm_path) < 80)

        pub = ChannelState(
            channel_id="chan__________________bbb", display="공지사항",
            channel_name="town-square", team_name="team-a", type="O",
        )
        check("공개채널 경로", archiver._path_for(pub) == "public/team-a/town-square__chan____.jsonl")

        raw = {"id": "p1", "message": "hi", "metadata": {"files": [{"id": "f1", "name": "a.png", "size": 10}]}, "props": {"x": 1}}
        slim = slim_post(raw)
        check("--slim 이 첨부 정보는 보존", "metadata" not in slim and slim["_files"][0]["name"] == "a.png")

        man = Manifest(base_url="https://x", user_id="u", username="n")
        man.channels["c1"] = ChannelState(channel_id="c1", display="d")
        import tempfile
        from pathlib import Path as _P

        with tempfile.TemporaryDirectory() as tmp:
            mp = _P(tmp) / "manifest.json"
            man.save(mp)
            reloaded = Manifest.load(mp)
            check(
                "manifest 저장/복원",
                reloaded.username == "n" and reloaded.channels["c1"].display == "d",
            )
            # 제외 정책은 plan() 한 곳에서만 걸린다. 아카이브되는 모든 채널이 지나는
            # 관문이라, 여기가 새면 백필·증분·재개 어느 경로로도 다시 수집된다.
            raw_channels = [
                {"id": "c1____________________aaa", "type": "P", "display_name": "홍길동(피드백)",
                 "name": "hong-feedback", "total_msg_count": 100},
                {"id": "c2____________________aaa", "type": "O", "display_name": "1. 공지사항",
                 "name": "notice", "total_msg_count": 100},
            ]
            planned = Archiver(client, _P(tmp), log=lambda _m: None, exclude=PAT).plan(raw_channels)
            check("제외: 아카이브 계획에서 빠진다",
                  [st.channel_id for st in planned] == ["c2____________________aaa"])
            kept = Archiver(client, _P(tmp), log=lambda _m: None, exclude=()).plan(raw_channels)
            check("제외: --include-excluded 면 계획에 남는다", len(kept) == 2)

            check("manifest 원자적 저장", not (_P(tmp) / "manifest.tmp").exists())

            # ---- 대조(reconcile): 서버 없이 manifest 만으로 드리프트를 분류한다 ----
            # 서버 카운트(expected)와 보관 정책 컷은 이미 manifest 에 있으므로 오프라인으로 된다.
            from mattertok.archive import reconcile, reconcile_summary
            from mattertok.cli import cmd_reconcile

            base = _P(tmp) / "recon"
            posts = base / "posts"
            posts.mkdir(parents=True)

            def fixture(cid, display, archived, expected, *, cut=0, lines=None, tail=""):
                state = ChannelState(
                    channel_id=cid, display=display, type="O", path=f"{cid}.jsonl",
                    post_count=archived, expected=expected, inaccessible_before=cut,
                )
                rows = archived if lines is None else lines
                body = "".join(json.dumps({"id": f"{cid}-{i}"}) + "\n" for i in range(rows))
                (posts / state.path).write_text(body + tail, encoding="utf-8")
                return state

            recon = Manifest()
            for state in (
                fixture("ok", "정확", 100, 100),
                fixture("over", "초과", 1300, 1000),                    # +300
                fixture("gap", "누락", 700, 1000),                      # -300, 보관컷 없음
                fixture("ret", "보관컷", 700, 1000, cut=1_700_000_000_000),
                fixture("unk", "카운트없음", 50, 0),
                fixture("edge", "허용오차내", 1010, 1000),               # +10 = 정확히 2%
                fixture("disk", "디스크불일치", 100, 100, lines=97),
                fixture("tail", "꼬리잘림", 10, 10, tail='{"id": "쓰다 만'),
            ):
                recon.channels[state.channel_id] = state

            rows = {r.channel_id: r for r in reconcile(recon, posts)}
            check("reconcile: 일치", rows["ok"].verdict == "ok" and rows["ok"].drift == 0)
            check("reconcile: 초과", rows["over"].verdict == "over" and rows["over"].drift == 300)
            check("reconcile: 누락(설명 안 됨)", rows["gap"].verdict == "gap")
            check("reconcile: 누락(보관 정책)",
                  rows["ret"].verdict == "retention" and rows["ret"].truncated)
            check("reconcile: 서버 카운트 없으면 판정 보류", rows["unk"].verdict == "unknown")
            check("reconcile: 허용오차 안은 일치", rows["edge"].verdict == "ok")
            check("reconcile: 허용오차를 0 으로 주면 걸린다",
                  {r.channel_id: r for r in reconcile(recon, posts, tolerance=0)}["edge"].verdict
                  == "over")
            check("reconcile: manifest 와 디스크 불일치 탐지",
                  rows["disk"].on_disk == 97 and rows["disk"].disk_drift == -3)
            check("reconcile: 쓰다 만 마지막 줄은 안 센다",
                  rows["tail"].on_disk == 10 and rows["tail"].partial_tail)
            check("reconcile: 디스크 세기 생략", reconcile(recon, posts, count_disk=False)[0].on_disk == -1)

            ordered = reconcile(recon, posts)
            check("reconcile: 드리프트 큰 순 정렬",
                  all(abs(a.drift) >= abs(b.drift) for a, b in zip(ordered, ordered[1:])))

            summary = reconcile_summary(ordered)
            check("reconcile: 총계와 순 드리프트",
                  summary["channels"] == 8 and summary["archived"] == 3970
                  and summary["expected"] == 4210 and summary["drift"] == -240)
            check("reconcile: 분류 집계",
                  summary["verdicts"] == {"ok": 4, "over": 1, "gap": 1, "retention": 1, "unknown": 1}
                  and summary["disk_mismatch"] == 1 and summary["disk_drift"] == -3
                  and summary["partial_tail"] == 1 and summary["truncated"] == 1)

            recon.save(base / "manifest.json")

            def run_reconcile(*argv):
                ns = parser.parse_args(["reconcile", *argv])
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    code = cmd_reconcile(ns)
                return code, buf.getvalue()

            code, text = run_reconcile("-o", str(base))
            check("reconcile: CLI 요약 출력", code == 1 and "순 드리프트 -240건" in text)
            code, text = run_reconcile("-o", str(base), "--json")
            payload = json.loads(text)
            check("reconcile: CLI --json 은 순수 JSON",
                  payload["summary"]["drift"] == -240 and len(payload["channels"]) == 8
                  and payload["channels"][0]["disk_drift"] == 0)
            code, text = run_reconcile("-o", str(_P(tmp) / "없는폴더"))
            check("reconcile: 아카이브 없으면 안내", code == 1 and "manifest.json" in text)

            ns = parser.parse_args(["reconcile"])
            check("reconcile: --refresh 는 기본 꺼짐 (서버 접속 없음)",
                  ns.refresh is False and ns.out == "archive" and ns.func is cmd_reconcile)

            # --refresh 는 실제 서버로 돌려볼 수 없으므로 가짜 클라이언트로 경로만 고정한다.
            # 특히 "manifest 를 저장하지 않는다" 는 라이브 수집기의 커서를 지키는 불변식이다.
            from mattertok.archive import refresh_expected

            class CountServer:
                def __init__(self) -> None:
                    self.asked: list[str] = []

                def get(self, path: str, **_kw):
                    self.asked.append(path)
                    if path.endswith("gap"):
                        raise ApiError(404, "channel not found", path)
                    return {"total_msg_count": 777}

            fake = CountServer()
            stamp = (base / "manifest.json").stat().st_mtime_ns
            updated, failed = refresh_expected(fake, recon, rate=0, log=lambda _m: None)
            on_disk = json.loads((base / "manifest.json").read_text(encoding="utf-8"))
            check("reconcile: --refresh 가 expected 를 갱신",
                  updated == 7 and failed == 1 and recon.channels["ok"].expected == 777
                  and fake.asked[0] == "/channels/ok")
            check("reconcile: --refresh 는 실패한 채널의 expected 를 지킨다",
                  recon.channels["gap"].expected == 1000)
            check("reconcile: --refresh 가 manifest 를 저장하지 않는다",
                  (base / "manifest.json").stat().st_mtime_ns == stamp
                  and on_disk["channels"]["ok"]["expected"] == 100)

            # ---- finalize: 2-패스(메타만 메모리) 리팩터가 기존 출력과 한 바이트도 다르지 않은지 ----
            # 합성 테스트만 믿고 넘어가면 실데이터에서 터진다. 그래서 "새 결과가 그럴듯한가" 가
            # 아니라 "리팩터 전 구현이 뱉던 바이트와 같은가" 를 직접 대조한다.
            def legacy_finalize(path, post_count):
                """리팩터 전 구현 그대로 — 본문을 전부 메모리에 든다. 비교 기준일 뿐이다."""
                by_id = {}
                with path.open("r", encoding="utf-8") as fh:
                    for text in fh:
                        text = text.strip()
                        if not text:
                            continue
                        try:
                            post = json.loads(text)
                        except ValueError:
                            continue
                        by_id[post.get("id", "")] = post
                ordered = sorted(by_id.values(),
                                 key=lambda p: (p.get("create_at", 0), p.get("id", "")))
                tmp = path.with_suffix(".jsonl.tmp")
                with tmp.open("w", encoding="utf-8") as fh:
                    for post in ordered:
                        fh.write(json.dumps(post, ensure_ascii=False))
                        fh.write("\n")
                tmp.replace(path)
                return len(ordered), max(0, post_count - len(ordered))

            def post_line(pid, create_at, body):
                return json.dumps({"id": pid, "create_at": create_at, "message": body,
                                   "user_id": "u"}, ensure_ascii=False)

            # 오프셋이 글자 수 기준이면 여기서 깨진다: 한글·이모지로 바이트 ≠ 글자, 줄 길이도 제각각.
            raw_lines = [
                post_line("p3", 300, "세 번째 — 한글이라 바이트와 글자 수가 다르다"),
                post_line("p1", 100, "첫 번째"),
                "",                                            # 빈 줄
                post_line("p2", 200, "두 번째 🎉 이모지는 4바이트"),
                "   ",                                         # 공백만 있는 줄
                post_line("p1", 100, "첫 번째 — 나중 판이 이긴다"),  # 같은 id 재등장
                post_line("p0", 0, "가장 오래된 글"),
                json.dumps({"create_at": 50, "message": "id 가 없는 글"}, ensure_ascii=False),
                json.dumps({"id": "px", "message": "create_at 이 없는 글"}, ensure_ascii=False),
                post_line("p2", 200, "두 번째 — 이것도 나중 판 🎉"),
                '{"id": "쓰다 만 마지',                          # 잘린 마지막 줄
            ]
            body = "\n".join(raw_lines)

            fin = _P(tmp) / "finalize"
            (fin / "posts").mkdir(parents=True)
            fresh = fin / "posts" / "ch.jsonl"
            fresh.write_text(body, encoding="utf-8")
            legacy = fin / "legacy.jsonl"
            legacy.write_text(body, encoding="utf-8")

            raw = legacy.read_bytes()
            check("finalize: fixture 에 멀티바이트 본문이 있다", any(b > 0x7F for b in raw))
            check("finalize: fixture 가 쓰기 개행 규약을 그대로 탄다",
                  raw.count(b"\n") == len(raw_lines) - 1
                  and (os.linesep != "\r\n" or b"\r\n" in raw))
            check("finalize: 두 입력이 같은 바이트로 출발", raw == fresh.read_bytes())

            legacy_kept, legacy_removed = legacy_finalize(legacy, 11)

            finalizer = Archiver(client, fin, rate=0, log=lambda _m: None)
            fin_state = ChannelState(channel_id="c", display="채널", type="O",
                                     path="ch.jsonl", post_count=11)
            finalizer.manifest.channels["c"] = fin_state
            fin_files, fin_removed = finalizer.finalize()

            check("finalize: 출력이 리팩터 전과 바이트 단위로 동일", fresh.read_bytes() == legacy.read_bytes())
            check("finalize: 건수·중복 제거 수치도 동일",
                  fin_files == 1 and fin_removed == legacy_removed == 5
                  and fin_state.post_count == legacy_kept == 6)
            check("finalize: tmp 잔여 없음", not (fin / "posts" / "ch.jsonl.tmp").exists())

            got = [json.loads(x) for x in fresh.read_text(encoding="utf-8").splitlines() if x.strip()]
            check("finalize: (create_at, id) 순 정렬",
                  [p.get("id", "") for p in got] == ["p0", "px", "", "p1", "p2", "p3"])
            check("finalize: 같은 id 는 마지막 판이 이긴다",
                  "나중 판이 이긴다" in got[3]["message"] and "이것도 나중 판" in got[4]["message"])
            check("finalize: 빈 줄·잘린 줄은 버린다", len(got) == 6)
            check("finalize: 멀티바이트 본문이 온전하다",
                  got[5]["message"].endswith("다르다") and "🎉" in got[4]["message"])

            # 두 번 돌려도 같은 결과 (이미 정리된 파일에 대해 멱등)
            again = Archiver(client, fin, rate=0, log=lambda _m: None)
            again.manifest.channels["c"] = ChannelState(
                channel_id="c", display="채널", type="O", path="ch.jsonl", post_count=6)
            before_bytes = fresh.read_bytes()
            _, again_removed = again.finalize()
            check("finalize: 멱등 (재실행해도 바이트 동일, 지울 중복 없음)",
                  fresh.read_bytes() == before_bytes and again_removed == 0)

        # ---- 실시간 아카이빙: 중복이 안 생기는지 (별도 모듈) ----
        import selfcheck_live

        selfcheck_live.run(check)
    finally:
        client.close()

    width = max(len(name) for name, _ in checks)
    failed = 0
    for name, ok in checks:
        print(f"  {'PASS' if ok else 'FAIL'}  {name.ljust(width)}")
        failed += not ok

    print()
    if failed:
        print(f"{failed}/{len(checks)} 실패")
        return 1
    print(f"{len(checks)}개 점검 모두 통과")
    return 0


if __name__ == "__main__":
    sys.exit(main())

