"""Mattermost REST API v4 클라이언트 — 조회 / 검색 / 전송.

웹훅을 전혀 쓰지 않는다. 전부 공식 REST API v4 엔드포인트다.
"""

from __future__ import annotations

import time
from typing import Any, Iterator

import httpx

from .auth import Session

CHANNEL_TYPE_LABEL = {"O": "공개", "P": "비공개", "D": "DM", "G": "그룹DM"}


class ApiError(RuntimeError):
    def __init__(self, status: int, detail: str, path: str) -> None:
        super().__init__(f"[{status}] {path} — {detail}")
        self.status = status
        self.detail = detail
        self.path = path


class NotFound(ApiError):
    pass


class MattermostClient:
    def __init__(self, session: Session, *, timeout: float = 30.0) -> None:
        self.session = session
        self.base_url = session.base_url.rstrip("/")
        self._http = httpx.Client(
            base_url=f"{self.base_url}/api/v4",
            headers={"Authorization": f"Bearer {session.token}"},
            timeout=timeout,
        )
        self._user_cache: dict[str, dict] = {}
        self._channel_cache: dict[str, dict] = {}

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "MattermostClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------ 저수준

    def request(self, method: str, path: str, **kwargs: Any) -> Any:
        """429/5xx 는 지수 백오프로 재시도한다. Mattermost 는 rate limit 이 있다."""
        delay = 1.0
        last: httpx.Response | None = None
        for attempt in range(4):
            try:
                response = self._http.request(method, path, **kwargs)
            except httpx.HTTPError as exc:
                if attempt == 3:
                    raise ApiError(0, f"연결 실패: {exc}", path) from exc
                time.sleep(delay)
                delay *= 2
                continue

            if response.status_code < 400:
                if response.status_code == 204 or not response.content:
                    return None
                return response.json()

            last = response
            if response.status_code == 429:
                retry_after = float(response.headers.get("Retry-After") or delay)
                time.sleep(min(retry_after, 30.0))
                delay *= 2
                continue
            if 500 <= response.status_code < 600 and attempt < 3:
                time.sleep(delay)
                delay *= 2
                continue
            break

        assert last is not None
        detail = _detail(last)
        if last.status_code == 404:
            raise NotFound(404, detail, path)
        if last.status_code == 401:
            raise ApiError(401, f"{detail} — 세션이 만료됐을 수 있습니다. `mattertok login` 으로 재로그인하세요.", path)
        raise ApiError(last.status_code, detail, path)

    def get(self, path: str, **kwargs: Any) -> Any:
        return self.request("GET", path, **kwargs)

    def post_json(self, path: str, payload: Any) -> Any:
        return self.request("POST", path, json=payload)

    # ------------------------------------------------------------------ 신원

    def me(self) -> dict:
        return self.get("/users/me")

    def user(self, user_id: str) -> dict:
        if user_id not in self._user_cache:
            try:
                self._user_cache[user_id] = self.get(f"/users/{user_id}")
            except ApiError:
                self._user_cache[user_id] = {"id": user_id, "username": user_id[:8]}
        return self._user_cache[user_id]

    def users_by_ids(self, user_ids: list[str]) -> dict[str, dict]:
        """여러 사용자를 한 번에 조회해 캐시에 채운다 (N+1 요청 방지)."""
        missing = [uid for uid in set(user_ids) if uid and uid not in self._user_cache]
        for i in range(0, len(missing), 100):
            chunk = missing[i : i + 100]
            try:
                for user in self.post_json("/users/ids", chunk) or []:
                    self._user_cache[user["id"]] = user
            except ApiError:
                for uid in chunk:
                    self._user_cache.setdefault(uid, {"id": uid, "username": uid[:8]})
        return self._user_cache

    def username(self, user_id: str) -> str:
        return self.user(user_id).get("username", user_id[:8])

    def user_by_username(self, username: str) -> dict:
        return self.get(f"/users/username/{username.lstrip('@')}")

    # ------------------------------------------------------------------ 팀/채널

    def my_teams(self) -> list[dict]:
        return self.get("/users/me/teams") or []

    def my_channels(self, team_id: str) -> list[dict]:
        channels = self.get(f"/users/me/teams/{team_id}/channels") or []
        for channel in channels:
            self._channel_cache[channel["id"]] = channel
        return channels

    def all_my_channels(self) -> list[dict]:
        result: list[dict] = []
        for team in self.my_teams():
            for channel in self.my_channels(team["id"]):
                channel = dict(channel)
                channel["_team_name"] = team.get("name", "")
                channel["_team_display"] = team.get("display_name", "")
                result.append(channel)
        return result

    def channel(self, channel_id: str) -> dict:
        if channel_id not in self._channel_cache:
            self._channel_cache[channel_id] = self.get(f"/channels/{channel_id}")
        return self._channel_cache[channel_id]

    def channel_label(self, channel_id: str) -> str:
        """사람이 읽을 수 있는 채널 이름. DM 이면 상대 사용자명으로 바꿔준다."""
        try:
            channel = self.channel(channel_id)
        except ApiError:
            return channel_id[:8]
        if channel.get("type") == "D":
            members = channel.get("name", "").split("__")
            other = next((m for m in members if m != self.session.user_id), None)
            return f"@{self.username(other)}" if other else "@DM"
        if channel.get("type") == "G":
            return channel.get("display_name") or "그룹DM"
        return f"#{channel.get('name', channel_id[:8])}"

    def dm_channel(self, other_user_id: str) -> dict:
        """상대와의 DM 채널을 얻는다 (없으면 생성). 이미 있으면 기존 채널을 돌려준다."""
        channel = self.post_json("/channels/direct", [self.session.user_id, other_user_id])
        self._channel_cache[channel["id"]] = channel
        return channel

    def resolve_channel(self, spec: str) -> dict:
        """채널 지정자를 채널 객체로 해석한다.

        지원 형식:
          - 26자 channel_id
          - "@username"            → 그 사람과의 DM
          - "team/channel-name"    → 팀 지정 조회
          - "#channel-name" 또는 "channel-name" → 내 채널 중 이름/표시명 부분일치
        """
        spec = spec.strip()
        if not spec:
            raise NotFound(404, "채널을 지정하지 않았습니다.", "/channels")

        if spec.startswith("@"):
            user = self.user_by_username(spec)
            return self.dm_channel(user["id"])

        if len(spec) == 26 and spec.isalnum():
            try:
                return self.channel(spec)
            except NotFound:
                pass

        if "/" in spec:
            team_name, _, channel_name = spec.partition("/")
            channel = self.get(
                f"/teams/name/{team_name}/channels/name/{channel_name.lstrip('#')}"
            )
            self._channel_cache[channel["id"]] = channel
            return channel

        needle = spec.lstrip("#").lower()
        channels = self.all_my_channels()
        exact = [c for c in channels if c.get("name", "").lower() == needle]
        if len(exact) == 1:
            return exact[0]
        partial = [
            c
            for c in channels
            if needle in c.get("name", "").lower()
            or needle in (c.get("display_name") or "").lower()
        ]
        if len(partial) == 1:
            return partial[0]
        if not partial:
            raise NotFound(404, f"'{spec}' 과 일치하는 채널을 찾지 못했습니다. `mattertok channels` 로 목록을 확인하세요.", "/channels")
        names = ", ".join(f"{c.get('display_name') or c.get('name')}({c.get('name')})" for c in partial[:8])
        raise ApiError(409, f"'{spec}' 이 여러 채널과 일치합니다: {names}", "/channels")

    # ------------------------------------------------------------------ 메시지 조회

    def posts(
        self,
        channel_id: str,
        *,
        limit: int = 60,
        since: int | None = None,
        after: str | None = None,
        before: str | None = None,
    ) -> list[dict]:
        """채널 메시지를 오래된 순으로 돌려준다.

        since: epoch milliseconds. 그 시각 이후 생성/수정된 글만.
        """
        collected: dict[str, dict] = {}
        order: list[str] = []
        page = 0
        per_page = min(200, max(1, limit))

        while True:
            params: dict[str, Any] = {"per_page": per_page}
            if since is not None:
                params["since"] = int(since)
            else:
                params["page"] = page
                if after:
                    params["after"] = after
                if before:
                    params["before"] = before

            data = self.get(f"/channels/{channel_id}/posts", params=params) or {}
            posts = data.get("posts") or {}
            batch_order = data.get("order") or []
            for post_id in batch_order:
                if post_id not in collected:
                    collected[post_id] = posts[post_id]
                    order.append(post_id)

            # since 모드는 서버가 한 번에 전부 돌려주므로 페이징하지 않는다.
            if since is not None or len(batch_order) < per_page or len(collected) >= limit:
                break
            page += 1

        result = [collected[pid] for pid in order]
        result.sort(key=lambda p: p.get("create_at", 0))
        if since is None and len(result) > limit:
            result = result[-limit:]
        self.users_by_ids([p.get("user_id", "") for p in result])
        return result

    def thread(self, post_id: str) -> list[dict]:
        data = self.get(f"/posts/{post_id}/thread") or {}
        posts = list((data.get("posts") or {}).values())
        posts.sort(key=lambda p: p.get("create_at", 0))
        self.users_by_ids([p.get("user_id", "") for p in posts])
        return posts

    def search(
        self, team_id: str, terms: str, *, is_or: bool = False, per_page: int = 40
    ) -> list[dict]:
        data = self.post_json(
            f"/teams/{team_id}/posts/search",
            {"terms": terms, "is_or_search": is_or, "per_page": per_page, "page": 0},
        ) or {}
        posts = data.get("posts") or {}
        order = data.get("order") or list(posts.keys())
        result = [posts[pid] for pid in order if pid in posts]
        self.users_by_ids([p.get("user_id", "") for p in result])
        return result

    def search_all_teams(self, terms: str, *, is_or: bool = False, per_page: int = 40) -> list[dict]:
        result: list[dict] = []
        for team in self.my_teams():
            try:
                result.extend(self.search(team["id"], terms, is_or=is_or, per_page=per_page))
            except ApiError:
                continue
        result.sort(key=lambda p: p.get("create_at", 0), reverse=True)
        return result

    def unread_channels(self) -> Iterator[tuple[dict, int]]:
        """읽지 않은 메시지가 있는 채널과 그 개수를 뽑는다."""
        for team in self.my_teams():
            members = self.get(f"/users/me/teams/{team['id']}/channels/members") or []
            by_id = {m["channel_id"]: m for m in members}
            for channel in self.my_channels(team["id"]):
                member = by_id.get(channel["id"])
                if not member:
                    continue
                unread = channel.get("total_msg_count", 0) - member.get("msg_count", 0)
                if unread > 0:
                    enriched = dict(channel)
                    enriched["_team_name"] = team.get("name", "")
                    yield enriched, unread

    # ------------------------------------------------------------------ 메시지 전송

    def create_post(
        self,
        channel_id: str,
        message: str,
        *,
        root_id: str | None = None,
        props: dict | None = None,
    ) -> dict:
        payload: dict[str, Any] = {"channel_id": channel_id, "message": message}
        if root_id:
            payload["root_id"] = root_id
        if props:
            payload["props"] = props
        return self.post_json("/posts", payload)

    def send_dm(self, username: str, message: str) -> dict:
        user = self.user_by_username(username)
        channel = self.dm_channel(user["id"])
        return self.create_post(channel["id"], message)

    def add_reaction(self, post_id: str, emoji_name: str) -> dict:
        return self.post_json(
            "/reactions",
            {"user_id": self.session.user_id, "post_id": post_id, "emoji_name": emoji_name.strip(":")},
        )

    def mark_viewed(self, channel_id: str) -> None:
        self.post_json(
            f"/channels/members/{self.session.user_id}/view", {"channel_id": channel_id}
        )


def _detail(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text[:200]
    return str(body.get("message") or body.get("id") or response.text[:200])

