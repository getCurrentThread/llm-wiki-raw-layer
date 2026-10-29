"""`live` 아카이빙 자체 점검 — 서버 없이 돈다. selfcheck.py 가 이어서 호출한다.

핵심 질문 하나만 본다: **신호가 겹치거나 중복되거나 재시작해도 같은 글이 두 번 저장되는가.**
답이 "아니오"인 근거는 메모리 상의 id 집합이 아니라 커서 불변식이다 — 쓰기는 언제나
`?after=<newest_post_id>` 로만 일어나고, 서버는 그 이후 것만 준다.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import os
import tempfile
import time
from pathlib import Path

from mattertok.archive import Archiver
from mattertok.live import ArchiveLock, LiveArchiver, LockBusy

CHANNEL = "chan__________________lll"
PER_PAGE = 200


class _FakeSession:
    base_url = "https://x"
    user_id = "me"
    username = "me"
    token = "t"


class FakeServer:
    """`?after=<post_id>` 를 마터모스트와 같은 의미로 흉내낸다 — 그 id 보다 뒤엣것만."""

    def __init__(self) -> None:
        self.posts: list[dict] = []
        self.session = _FakeSession()

    def add(self, count: int) -> None:
        base = len(self.posts)
        for i in range(count):
            idx = base + i
            self.posts.append({
                "id": f"p{idx:05d}", "create_at": 1_700_000_000_000 + idx, "user_id": "u",
                "channel_id": CHANNEL, "message": f"m{idx}", "type": "", "root_id": "",
            })

    def get(self, _path: str, params: dict | None = None) -> dict:
        params = params or {}
        after, before = params.get("after"), params.get("before")
        per = int(params.get("per_page", PER_PAGE))
        if after:
            idx = next((i for i, p in enumerate(self.posts) if p["id"] == after), None)
            sel = self.posts[idx + 1:] if idx is not None else []
            window = sel[:per]
        elif before:
            idx = next((i for i, p in enumerate(self.posts) if p["id"] == before), None)
            sel = self.posts[:idx] if idx is not None else []
            window = sel[-per:]
        else:
            sel = self.posts
            window = sel[-per:]
        newest_first = list(reversed(window))
        return {
            "order": [p["id"] for p in newest_first],
            "posts": {p["id"]: p for p in newest_first},
            "next_post_id": "x" if after and len(sel) > per else "",
            "prev_post_id": "x" if not after and len(sel) > per else "",
            "first_inaccessible_post_time": 0,
        }

    def my_teams(self) -> list[dict]:
        return [{"id": "t1", "name": "team", "display_name": "Team"}]

    def my_channels(self, _team_id: str) -> list[dict]:
        return [{"id": CHANNEL, "type": "O", "display_name": "공지", "name": "notice",
                 "total_msg_count": len(self.posts)}]

    all_my_channels = lambda self: self.my_channels("t1")  # noqa: E731

    def users_by_ids(self, _ids):
        return {}

    def channel_label(self, _channel_id: str) -> str:
        return "공지"


def _ids(path: Path) -> list[str]:
    if not path.is_file():
        return []
    return [json.loads(line)["id"]
            for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def run(check) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp)
        server = FakeServer()
        server.add(450)  # 여러 페이지 분량

        archiver = Archiver(server, out, rate=0, log=lambda _m: None)
        states = archiver.plan(server.my_channels("t1"))
        archiver.run(states)
        target = out / "posts" / states[0].path
        check("live: 초기 백필 450건", len(_ids(target)) == 450 == len(set(_ids(target))))

        live = LiveArchiver(server, archiver, debounce=0, sweep=0, log=lambda _m: None)

        for _ in range(20):
            live.mark(CHANNEL)
        asyncio.run(live._drain())
        check("live: 새 글 없이 신호 20회 → 증가 없음", len(_ids(target)) == 450)

        server.add(3)
        for _ in range(10):          # 신호 폭주 + 재연결 신호를 동시에
            live.mark(CHANNEL)
            live.mark_all()
        asyncio.run(live._drain())
        got = _ids(target)
        check("live: 신호 폭주해도 3건만 추가", len(got) == 453 and len(set(got)) == 453)

        for _ in range(3):           # WS 와 무관한 안전망 스윕
            asyncio.run(live._full_sweep(rediscover=True))
        got = _ids(target)
        check("live: 전체 스윕 3회 반복해도 중복 없음", len(got) == 453 == len(set(got)))

        server.add(5)                # 프로세스 재시작 (manifest 를 디스크에서 복원)
        restarted = Archiver(server, out, rate=0, log=lambda _m: None)
        live2 = LiveArchiver(server, restarted, debounce=0, sweep=0, log=lambda _m: None)
        live2.mark_all()
        asyncio.run(live2._drain())
        got = _ids(target)
        check("live: 재시작 후 이어받기 (중복 없음)", len(got) == 458 and len(set(got)) == 458)

        posts = [json.loads(x) for x in target.read_text(encoding="utf-8").splitlines() if x.strip()]
        tail = posts[-8:]
        check("live: 실시간 꼬리가 시간순",
              all(a["create_at"] <= b["create_at"] for a, b in zip(tail, tail[1:])))

        _, removed = restarted.finalize()
        check("live: finalize 가 지울 중복이 없다", removed == 0)
        posts = [json.loads(x) for x in target.read_text(encoding="utf-8").splitlines() if x.strip()]
        check("live: finalize 후 458건 유지", len(posts) == 458 == len({p["id"] for p in posts}))

        # ---- 자기 관측(status.json): 밖에서 "돌고 있는지" 를 파일 하나로 알 수 있어야 한다 ----
        from mattertok.cli import build_parser, cmd_status
        from mattertok.live import STATUS_STALE_AFTER_SEC, read_status, status_health

        live2.write_status()
        status = read_status(out)
        fields = {
            "updated_at", "pid", "host", "started_at", "uptime_sec", "saved_session",
            "synced_channels", "dirty_count", "last_signal_at", "last_write_at",
            "sweep_count", "last_error", "channels_total",
        }
        check("status: 스키마 필드 전부 존재", status is not None and fields <= set(status))
        check("status: 원자적 저장 (tmp 잔여 없음)", not (out / "status.tmp").exists())
        check(
            "status: 세션 실적 반영",
            status["saved_session"] == 5 and status["synced_channels"] == 1
            and status["channels_total"] == 1 and status["dirty_count"] == 0
            and status["last_write_at"] > 0 and status["last_signal_at"] > 0
            and status["last_error"] == "",
        )
        check(
            "status: 신호 전에는 last_signal_at 0",
            LiveArchiver(server, restarted, debounce=0, sweep=0,
                         log=lambda _m: None)._last_signal_at == 0,
        )

        live.write_status()  # 앞에서 전체 스윕을 3회 돌린 인스턴스
        check("status: 스윕 횟수 누적", (read_status(out) or {}).get("sweep_count") == 3)

        broken = LiveArchiver(server, archiver, debounce=0, sweep=0, log=lambda _m: None)
        broken.status_path = out / "없는폴더" / "status.json"
        broken.write_status()  # 예외가 새면 라이브 루프가 여기서 죽는다
        check("status: 기록 실패해도 예외가 새지 않는다", not broken.status_path.exists())

        # 신선도 판정은 시계에 의존하므로 now 를 주입해 결정적으로 본다.
        now = 1_800_000_000
        check("status: 신선 판정", status_health({"updated_at": now - 5}, now=now) == ("fresh", 5))
        check("status: 경계값은 신선",
              status_health({"updated_at": now - STATUS_STALE_AFTER_SEC}, now=now)[0] == "fresh")
        check("status: STALE 판정",
              status_health({"updated_at": now - STATUS_STALE_AFTER_SEC - 1}, now=now)[0] == "stale")
        check("status: 파일 없음 판정", status_health(None) == ("missing", 0))
        check("status: 없는 폴더 읽기 안전", read_status(out / "없는폴더") is None)

        def run_status(*argv: str) -> tuple[int, str]:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                code = cmd_status(build_parser().parse_args(["status", *argv]))
            return code, buf.getvalue()

        code, text = run_status("-o", str(out))
        check("status: CLI 정상 가동 표시", code == 0 and "정상 가동" in text)

        code, text = run_status("-o", str(out / "없는폴더"))
        check("status: CLI 안 돈 적 없으면 안내", code == 1 and "돈 적이 없습니다" in text)

        code, text = run_status("-o", str(out), "--json")
        check("status: CLI --json 은 순수 JSON", code == 0 and json.loads(text)["pid"] == os.getpid())

        stale_status = dict(read_status(out) or {})
        stale_status["updated_at"] = int(time.time()) - STATUS_STALE_AFTER_SEC - 60
        (out / "status.json").write_text(json.dumps(stale_status), encoding="utf-8")
        code, text = run_status("-o", str(out))
        check("status: CLI STALE 경고", code == 1 and "STALE" in text)

        first = ArchiveLock(out)
        first.acquire()
        second = ArchiveLock(out)
        try:
            second.acquire()
            check("live: 두 번째 수집기 거부", False)
        except LockBusy:
            check("live: 두 번째 수집기 거부", True)
        first.release()

        stale = ArchiveLock(out)
        stale.path.write_text(json.dumps({"pid": 999999, "host": "h", "heartbeat": 0}),
                              encoding="utf-8")
        try:
            stale.acquire()
            check("live: 죽은 잠금은 인수", True)
            stale.release()
        except LockBusy:
            check("live: 죽은 잠금은 인수", False)

