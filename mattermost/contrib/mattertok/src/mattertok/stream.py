"""WebSocket 실시간 수신.

주의할 점 두 가지 (둘 다 Mattermost 의 알려진 함정):
  1. `posted` 이벤트의 data.post 는 JSON 문자열로 한 번 더 감싸여 온다 (이중 인코딩).
  2. 연결이 끊긴 사이의 메시지는 그냥 사라진다. 재연결 후 REST `?since=` 로 갭을 메워야 한다.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Iterable

import websockets
from websockets.asyncio.client import connect

from .client import MattermostClient
from .config import excluded_channel_patterns, is_excluded_channel

Handler = Callable[["Incoming"], None | Awaitable[None]]


@dataclass(slots=True)
class Incoming:
    post: dict
    channel_id: str
    user_id: str
    message: str
    create_at: int
    channel_name: str = ""
    channel_type: str = ""
    sender_name: str = ""
    team_id: str = ""
    is_own: bool = False
    replayed: bool = False  # 재연결 갭 메우기로 뒤늦게 들어온 메시지

    @property
    def post_id(self) -> str:
        return self.post.get("id", "")

    @property
    def root_id(self) -> str:
        return self.post.get("root_id", "") or ""


@dataclass
class _Seen:
    """중복 방지용 최근 post id 집합 (크기 제한)."""

    maxlen: int = 4000
    _ids: set[str] = field(default_factory=set)
    _order: deque[str] = field(default_factory=deque)

    def add(self, post_id: str) -> bool:
        """처음 보는 id 면 True."""
        if not post_id or post_id in self._ids:
            return False
        self._ids.add(post_id)
        self._order.append(post_id)
        while len(self._order) > self.maxlen:
            self._ids.discard(self._order.popleft())
        return True


class MattermostStream:
    def __init__(
        self,
        client: MattermostClient,
        *,
        channel_ids: Iterable[str] | None = None,
        include_own: bool = False,
        gap_fill: bool = True,
        on_status: Callable[[str], None] | None = None,
        exclude: Iterable[str] | None = None,
        on_reconnect: Callable[[], None] | None = None,
    ) -> None:
        self.client = client
        self.channel_ids = set(channel_ids) if channel_ids else None
        self.include_own = include_own
        # 빈 튜플이면 제외 없음. None 이면 config 의 기본값.
        self.exclude = tuple(exclude) if exclude is not None else excluded_channel_patterns()
        self.gap_fill = gap_fill
        self.on_status = on_status or (lambda _msg: None)
        # 재연결은 "끊긴 동안 뭔가 놓쳤다"는 신호다. 상태 문자열을 파싱해서 알아내게 하지 말고
        # 명시적으로 알린다 (live 아카이버가 이때 전 채널을 다시 훑는다).
        self.on_reconnect = on_reconnect or (lambda: None)
        self._seen = _Seen()
        self._last_ms = int(time.time() * 1000)
        self._ws_url = client.session.base_url.replace("https://", "wss://").replace(
            "http://", "ws://"
        ) + "/api/v4/websocket"

    # ------------------------------------------------------------------

    async def run(self, handler: Handler) -> None:
        """끊겨도 계속 재연결하며 메시지를 흘려보낸다. Ctrl+C 로 종료."""
        backoff = 1.0
        first = True
        while True:
            try:
                async with connect(
                    self._ws_url,
                    ping_interval=30,
                    ping_timeout=20,
                    max_size=8 * 1024 * 1024,
                    open_timeout=20,
                ) as ws:
                    await self._authenticate(ws)
                    self.on_status("연결됨" if first else "재연결됨")
                    if not first:
                        self.on_reconnect()
                        if self.gap_fill:
                            await self._fill_gap(handler)
                    first = False
                    backoff = 1.0
                    await self._consume(ws, handler)
            except (KeyboardInterrupt, asyncio.CancelledError):
                raise
            except Exception as exc:  # noqa: BLE001 - 어떤 실패든 재연결로 흡수
                self.on_status(f"연결 끊김 ({type(exc).__name__}: {exc}). {backoff:.0f}초 후 재시도")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)

    # ------------------------------------------------------------------

    async def _authenticate(self, ws) -> None:
        await ws.send(
            json.dumps(
                {
                    "seq": 1,
                    "action": "authentication_challenge",
                    "data": {"token": self.client.session.token},
                }
            )
        )
        # 인증 성공이면 {"status":"OK","seq_reply":1} 다음 {"event":"hello"} 가 온다.
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            raw = await asyncio.wait_for(ws.recv(), timeout=20)
            frame = json.loads(raw)
            if frame.get("seq_reply") == 1:
                if frame.get("status") != "OK":
                    raise RuntimeError(f"WebSocket 인증 거부: {frame}")
                return
            if frame.get("event") == "hello":
                return
        raise RuntimeError("WebSocket 인증 응답이 오지 않았습니다.")

    async def _consume(self, ws, handler: Handler) -> None:
        async for raw in ws:
            try:
                frame = json.loads(raw)
            except ValueError:
                continue
            if frame.get("event") != "posted":
                continue
            incoming = self._parse_posted(frame)
            if incoming is None:
                continue
            await self._emit(handler, incoming)

    def _parse_posted(self, frame: dict) -> Incoming | None:
        data = frame.get("data") or {}
        raw_post = data.get("post")
        if not raw_post:
            return None
        try:
            # ← 이중 인코딩: data.post 는 dict 가 아니라 JSON 문자열이다.
            post = json.loads(raw_post) if isinstance(raw_post, str) else raw_post
        except ValueError:
            return None

        channel_id = post.get("channel_id", "")
        if self.channel_ids is not None and channel_id not in self.channel_ids:
            return None
        # 채널 이름이 프레임에 실려 오므로 추가 요청 없이 제외 판정이 된다.
        if is_excluded_channel(
            data.get("channel_display_name"), data.get("channel_name"), patterns=self.exclude
        ):
            return None

        user_id = post.get("user_id", "")
        is_own = user_id == self.client.session.user_id
        if is_own and not self.include_own:
            return None
        if post.get("type", "").startswith("system_"):
            return None

        self._last_ms = max(self._last_ms, int(post.get("create_at", 0)))
        if not self._seen.add(post.get("id", "")):
            return None

        return Incoming(
            post=post,
            channel_id=channel_id,
            user_id=user_id,
            message=post.get("message", ""),
            create_at=int(post.get("create_at", 0)),
            channel_name=data.get("channel_display_name") or data.get("channel_name") or "",
            channel_type=data.get("channel_type", ""),
            sender_name=(data.get("sender_name") or "").lstrip("@"),
            team_id=data.get("team_id", ""),
            is_own=is_own,
        )

    async def _fill_gap(self, handler: Handler) -> None:
        """끊겨 있던 동안의 메시지를 REST 로 긁어와 순서대로 흘려보낸다."""
        since = max(0, self._last_ms - 1000)  # 경계 여유 1초
        if self.channel_ids:
            targets = list(self.channel_ids)
        else:
            try:
                # 제외 채널은 요청 자체를 보내지 않는다. 걸러서 버릴 데이터를 굳이 받아올 이유가 없다.
                targets = [
                    c["id"]
                    for c in self.client.all_my_channels()
                    if not is_excluded_channel(
                        c.get("display_name"), c.get("name"), patterns=self.exclude
                    )
                ][:60]
            except Exception:  # noqa: BLE001
                return

        recovered: list[Incoming] = []
        for channel_id in targets:
            try:
                posts = await asyncio.to_thread(
                    self.client.posts, channel_id, since=since, limit=200
                )
            except Exception:  # noqa: BLE001 - 한 채널 실패가 전체를 막지 않게
                continue
            for post in posts:
                user_id = post.get("user_id", "")
                is_own = user_id == self.client.session.user_id
                if is_own and not self.include_own:
                    continue
                if post.get("type", "").startswith("system_"):
                    continue
                if not self._seen.add(post.get("id", "")):
                    continue
                # 명시적 --channels 로 지정된 경로는 위 필터를 안 거치므로 여기서 한 번 더 본다.
                label = self.client.channel_label(post.get("channel_id", ""))
                if is_excluded_channel(label, patterns=self.exclude):
                    continue
                self._last_ms = max(self._last_ms, int(post.get("create_at", 0)))
                recovered.append(
                    Incoming(
                        post=post,
                        channel_id=post.get("channel_id", ""),
                        user_id=user_id,
                        message=post.get("message", ""),
                        create_at=int(post.get("create_at", 0)),
                        channel_name=label,
                        sender_name=self.client.username(user_id),
                        is_own=is_own,
                        replayed=True,
                    )
                )

        if recovered:
            self.on_status(f"끊긴 동안 놓친 메시지 {len(recovered)}건 복구")
            for incoming in sorted(recovered, key=lambda m: m.create_at):
                await self._emit(handler, incoming)

    async def _emit(self, handler: Handler, incoming: Incoming) -> None:
        result = handler(incoming)
        if asyncio.iscoroutine(result):
            await result


async def watch(
    client: MattermostClient,
    handler: Handler,
    *,
    channel_ids: Iterable[str] | None = None,
    include_own: bool = False,
    on_status: Callable[[str], None] | None = None,
    exclude: Iterable[str] | None = None,
) -> None:
    stream = MattermostStream(
        client, channel_ids=channel_ids, include_own=include_own, on_status=on_status,
        exclude=exclude,
    )
    with contextlib.suppress(KeyboardInterrupt, asyncio.CancelledError):
        await stream.run(handler)

