"""전체 대화 아카이브 — 중단·재개 가능한 백필 + 증분 동기화.

전략
----
채널마다 두 방향으로 커서를 굴린다.

  백필(과거로)  : `?before=<지금까지 받은 가장 오래된 post_id>` 를 반복.
                  응답의 prev_post_id 가 빈 문자열이면 채널 끝.
  증분(미래로)  : 백필이 끝난 채널은 `?after=<가장 최신 post_id>` 로 새 글만.

진행 상태는 manifest.json 에 채널별로 남기므로 Ctrl+C 로 끊어도 이어서 받는다.
`?page=` 기반 페이징은 깊은 이력에서 어긋날 수 있어 쓰지 않는다.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from .client import ApiError, MattermostClient
from .config import excluded_channel_patterns, is_excluded_channel

PER_PAGE = 200
MANIFEST_VERSION = 1

TYPE_DIR = {"O": "public", "P": "private", "D": "dm", "G": "group"}

_INVALID = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_name(name: str, fallback: str = "unnamed", limit: int = 60) -> str:
    """Windows 파일명으로 안전하게. 예약어와 길이 제한까지 고려."""
    cleaned = _INVALID.sub("_", name).strip().strip(". ")
    cleaned = re.sub(r"\s+", " ", cleaned)[:limit].strip()
    if not cleaned:
        return fallback
    if cleaned.upper().split(".")[0] in {
        "CON", "PRN", "AUX", "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }:
        cleaned = "_" + cleaned
    return cleaned


class RateLimiter:
    """초당 요청 수 제한. Mattermost 는 사용자당 rate limit 이 걸려 있다."""

    def __init__(self, per_second: float) -> None:
        self.interval = 1.0 / per_second if per_second > 0 else 0.0
        self._next = 0.0

    def wait(self) -> None:
        if self.interval <= 0:
            return
        now = time.monotonic()
        if now < self._next:
            time.sleep(self._next - now)
            now = time.monotonic()
        self._next = now + self.interval


@dataclass
class ChannelState:
    channel_id: str
    display: str
    channel_name: str = ""
    team_name: str = ""
    type: str = "O"
    path: str = ""
    oldest_cursor: str = ""       # 백필 재개 지점 (before=)
    newest_post_id: str = ""      # 증분 재개 지점 (after=)
    newest_create_at: int = 0
    backfill_done: bool = False
    post_count: int = 0
    expected: int = 0             # 서버의 total_msg_count (참고값)
    inaccessible_before: int = 0  # first_inaccessible_post_time (0이면 제한 없음)
    error: str = ""

    @property
    def truncated(self) -> bool:
        return self.inaccessible_before > 0


@dataclass
class Manifest:
    version: int = MANIFEST_VERSION
    base_url: str = ""
    user_id: str = ""
    username: str = ""
    created_at: int = 0
    updated_at: int = 0
    channels: dict[str, ChannelState] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> "Manifest":
        if not path.is_file():
            return cls()
        raw = json.loads(path.read_text(encoding="utf-8"))
        channels = {
            cid: ChannelState(**state) for cid, state in (raw.pop("channels", None) or {}).items()
        }
        raw.pop("version", None)
        return cls(version=MANIFEST_VERSION, channels=channels, **raw)

    def save(self, path: Path) -> None:
        self.updated_at = int(time.time())
        payload = {
            "version": self.version,
            "base_url": self.base_url,
            "user_id": self.user_id,
            "username": self.username,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "channels": {cid: asdict(st) for cid, st in self.channels.items()},
        }
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(path)  # 원자적 교체 — 중간에 죽어도 manifest 가 깨지지 않는다


SLIM_DROP = ("metadata", "props", "participants", "hashtags", "pending_post_id", "remote_id")


def slim_post(post: dict) -> dict:
    out = {k: v for k, v in post.items() if k not in SLIM_DROP}
    files = (post.get("metadata") or {}).get("files")
    if files:
        out["_files"] = [
            {"id": f.get("id"), "name": f.get("name"), "size": f.get("size")} for f in files
        ]
    reactions = (post.get("metadata") or {}).get("reactions")
    if reactions:
        out["_reactions"] = [
            {"user_id": r.get("user_id"), "emoji": r.get("emoji_name")} for r in reactions
        ]
    return out


class Archiver:
    def __init__(
        self,
        client: MattermostClient,
        out_dir: Path,
        *,
        rate: float = 4.0,
        slim: bool = False,
        types: Iterable[str] = ("O", "P", "D", "G"),
        include_empty: bool = False,
        log: Callable[[str], None] = print,
        exclude: Iterable[str] | None = None,
    ) -> None:
        self.client = client
        self.out = out_dir
        self.posts_dir = out_dir / "posts"
        self.limiter = RateLimiter(rate)
        self.slim = slim
        self.types = set(types)
        self.include_empty = include_empty
        self.log = log
        # 빈 튜플을 넘기면 제외 없음. None 이면 설정된 기본값(config.DEFAULT_EXCLUDED_CHANNELS).
        self.exclude = tuple(exclude) if exclude is not None else excluded_channel_patterns()
        self.manifest_path = out_dir / "manifest.json"
        self.manifest = Manifest.load(self.manifest_path)

    # ------------------------------------------------------------------ 준비

    def discover(self) -> list[dict]:
        """내가 속한 모든 고유 채널. DM 은 팀마다 중복 등장하므로 id 로 dedupe."""
        seen: dict[str, dict] = {}
        for team in self.client.my_teams():
            self.limiter.wait()
            for ch in self.client.my_channels(team["id"]):
                if ch["id"] in seen:
                    continue
                enriched = dict(ch)
                enriched["_team_name"] = team.get("name", "") if ch.get("team_id") else ""
                enriched["_team_display"] = team.get("display_name", "") if ch.get("team_id") else ""
                seen[ch["id"]] = enriched

        # DM 상대 이름을 개별 조회하면 채널 수만큼 요청이 나간다. 한 번에 받아 캐시에 채운다.
        counterparts: list[str] = []
        for ch in seen.values():
            if ch.get("type") == "D":
                counterparts.extend(
                    uid for uid in ch.get("name", "").split("__") if uid != self.client.session.user_id
                )
        if counterparts:
            self.limiter.wait()
            self.client.users_by_ids(counterparts)
        return list(seen.values())

    def plan(self, channels: list[dict] | None = None) -> list[ChannelState]:
        raw = channels if channels is not None else self.discover()
        states: list[ChannelState] = []
        excluded = 0
        for ch in raw:
            ctype = ch.get("type", "O")
            if ctype not in self.types:
                continue
            # 제외는 여기서 한 번만 한다. plan() 은 아카이브되는 모든 채널이 지나는 유일한 관문이라
            # 여기서 막으면 백필·증분·재개 어느 경로로도 새지 않는다. DM 은 표시 이름을 얻는 데
            # 요청이 들어가므로, 먼저 요청이 필요 없는 필드로 판정하고 기존 상태의 display 만 덧본다.
            known = self.manifest.channels.get(ch["id"])
            if is_excluded_channel(
                ch.get("display_name"), ch.get("name"),
                known.display if known else None,
                patterns=self.exclude,
            ):
                excluded += 1
                continue
            expected = int(ch.get("total_msg_count", 0) or 0)
            state = known
            if state is None:
                if expected == 0 and not self.include_empty:
                    continue  # 메시지 0건인 채널(주로 자동 생성된 DM)은 요청 낭비
                state = ChannelState(
                    channel_id=ch["id"],
                    display=self._label(ch),
                    channel_name=ch.get("name", ""),
                    team_name=ch.get("_team_name", ""),
                    type=ctype,
                    expected=expected,
                )
                state.path = self._path_for(state)
            else:
                state.expected = expected or state.expected
            states.append(state)
        if excluded:
            # 조용히 빼면 나중에 "왜 이 채널이 없지" 로 돌아온다. 항상 말한다.
            self.log(f"  제외 {excluded}개 채널 (이름에 {', '.join(self.exclude)} 포함)")
        return states

    def _label(self, ch: dict) -> str:
        if ch.get("type") == "D":
            return self.client.channel_label(ch["id"])
        return ch.get("display_name") or ch.get("name") or ch["id"][:8]

    def _path_for(self, state: ChannelState) -> str:
        bucket = TYPE_DIR.get(state.type, "other")
        if state.type in ("D", "G"):
            # DM 의 raw name 은 'userid1__userid2' (53자) 라 경로가 쓸데없이 길어진다.
            # Windows 260자 제한도 있으니 표시 이름(@username)을 쓴다.
            folder = bucket
            stem = safe_name(state.display, state.channel_id[:8], 40)
        else:
            folder = f"{bucket}/{safe_name(state.team_name or 'no-team', 'no-team', 40)}"
            stem = safe_name(state.channel_name or state.display, state.channel_id[:8])
        return f"{folder}/{stem}__{state.channel_id[:8]}.jsonl"

    # ------------------------------------------------------------------ 실행

    def run(
        self,
        states: list[ChannelState],
        *,
        on_progress: Callable[[int, int, ChannelState], None] | None = None,
    ) -> Manifest:
        me = self.client.session
        if not self.manifest.created_at:
            self.manifest.created_at = int(time.time())
        self.manifest.base_url = me.base_url
        self.manifest.user_id = me.user_id
        self.manifest.username = me.username

        total = len(states)
        for index, state in enumerate(states, start=1):
            self.manifest.channels[state.channel_id] = state
            try:
                self._sync_channel(state)
                state.error = ""
            except ApiError as exc:
                state.error = str(exc)
                self.log(f"  ! {state.display}: {exc}")
            except KeyboardInterrupt:
                self.manifest.save(self.manifest_path)
                raise
            if on_progress:
                on_progress(index, total, state)
            self.manifest.save(self.manifest_path)  # 채널 단위로 진행 저장
        return self.manifest

    def _sync_channel(self, state: ChannelState) -> None:
        target = self.posts_dir / state.path
        target.parent.mkdir(parents=True, exist_ok=True)

        if not state.backfill_done:
            self._backfill(state, target)
        else:
            self._forward(state, target)

    def _fetch(self, channel_id: str, params: dict) -> dict:
        self.limiter.wait()
        return self.client.get(f"/channels/{channel_id}/posts", params=params) or {}

    def _backfill(self, state: ChannelState, target: Path) -> None:
        """과거 방향으로 끝까지 긁는다."""
        while True:
            params: dict = {"per_page": PER_PAGE}
            if state.oldest_cursor:
                params["before"] = state.oldest_cursor

            data = self._fetch(state.channel_id, params)
            order = data.get("order") or []
            posts = data.get("posts") or {}

            inaccessible = int(data.get("first_inaccessible_post_time") or 0)
            if inaccessible:
                state.inaccessible_before = inaccessible

            if not order:
                state.backfill_done = True
                return

            batch = [posts[pid] for pid in order if pid in posts]
            self._append(target, batch)
            state.post_count += len(batch)

            newest = batch[0]  # order 는 최신→과거
            if newest.get("create_at", 0) > state.newest_create_at:
                state.newest_create_at = int(newest.get("create_at", 0))
                state.newest_post_id = newest.get("id", "")

            state.oldest_cursor = order[-1]

            if not data.get("prev_post_id") or len(order) < PER_PAGE:
                state.backfill_done = True
                return

    def _forward(self, state: ChannelState, target: Path) -> None:
        """백필이 끝난 채널에 새로 올라온 글만 이어붙인다."""
        if not state.newest_post_id:
            return
        cursor = state.newest_post_id
        while True:
            data = self._fetch(state.channel_id, {"per_page": PER_PAGE, "after": cursor})
            order = data.get("order") or []
            posts = data.get("posts") or {}
            if not order:
                return

            batch = [posts[pid] for pid in order if pid in posts]
            self._append(target, batch)
            state.post_count += len(batch)

            newest = batch[0]
            state.newest_create_at = int(newest.get("create_at", 0))
            state.newest_post_id = newest.get("id", "")
            cursor = state.newest_post_id

            if not data.get("next_post_id") or len(order) < PER_PAGE:
                return

    def _append(self, target: Path, posts: list[dict]) -> None:
        # 서버는 최신→과거 순으로 준다. 그대로 붙이면 배치 안이 거꾸로 남는다. 백필은 페이지가
        # 과거로 가므로 어차피 finalize 가 정렬해야 하지만, 실시간 수집은 배치가 작고 계속 이어져서
        # 여기서 한 번 뒤집어 두면 파일 꼬리가 항상 시간순이 된다.
        ordered = sorted(posts, key=lambda p: (p.get("create_at", 0), p.get("id", "")))
        with target.open("a", encoding="utf-8") as fh:
            for post in ordered:
                fh.write(json.dumps(slim_post(post) if self.slim else post, ensure_ascii=False))
                fh.write("\n")

    # ------------------------------------------------------------------ 마무리

    def finalize(self) -> tuple[int, int]:
        """받은 순서(최신→과거, 여러 세션 뒤섞임)를 id 중복 제거 후 시간순으로 정리.

        파일을 두 번 읽는다. 첫 패스는 줄마다 `(create_at, 파일 안 위치)` 만 기억하고 본문은
        버린다. 둘째 패스에서 정렬된 순서대로 그 위치로 seek 해 한 줄씩 다시 읽어 쓴다.
        본문 전체를 메모리에 드는 대신 파일을 한 번 더 읽는 거래다 — 채널이 클수록 남는 장사고,
        피크 메모리가 채널 용량이 아니라 글 수에만 비례하게 된다.

        위치는 **바이트** 오프셋이라 읽기는 바이너리로 한다. 텍스트 모드의 tell() 은 Windows
        개행 변환(\\n ↔ \\r\\n) 때문에 실제 바이트 위치와 어긋나 엉뚱한 줄을 집는다.
        반대로 쓰기는 예전 그대로 텍스트 모드다 — 개행 변환까지 같아야 기존 파일과 한 바이트도
        달라지지 않는다.
        """
        files = 0
        removed = 0
        for state in self.manifest.channels.values():
            target = self.posts_dir / state.path
            if not target.is_file():
                continue

            # 패스 1: 무엇이 어디에 있는지만. 같은 id 가 여러 번 나오면 마지막 것이 이긴다.
            index: dict[str, tuple[object, int]] = {}
            offset = 0
            with target.open("rb") as fh:
                for raw in fh:
                    start = offset
                    offset += len(raw)
                    try:
                        post = json.loads(raw.decode("utf-8").strip())
                    except ValueError:
                        continue  # 빈 줄, 깨진 줄, 쓰다 만 마지막 줄 (UnicodeDecodeError 포함)
                    index[post.get("id", "")] = (post.get("create_at", 0), start)

            ordered = sorted(index.items(), key=lambda kv: (kv[1][0], kv[0]))
            removed += max(0, state.post_count - len(ordered))

            # 패스 2: 정렬된 순서대로 원본에서 그 줄만 다시 꺼내 쓴다.
            tmp = target.with_suffix(".jsonl.tmp")
            with target.open("rb") as src, tmp.open("w", encoding="utf-8") as fh:
                for _post_id, (_create_at, start) in ordered:
                    src.seek(start)
                    post = json.loads(src.readline().decode("utf-8").strip())
                    fh.write(json.dumps(post, ensure_ascii=False))
                    fh.write("\n")
            tmp.replace(target)
            state.post_count = len(ordered)
            files += 1
        self.manifest.save(self.manifest_path)
        return files, removed

    def write_sidecars(self) -> None:
        """사용자·채널 메타를 함께 저장해야 나중에 id 를 사람 이름으로 되돌릴 수 있다."""
        user_ids: set[str] = set()
        for state in self.manifest.channels.values():
            target = self.posts_dir / state.path
            if not target.is_file():
                continue
            with target.open("r", encoding="utf-8") as fh:
                for line in fh:
                    try:
                        user_ids.add(json.loads(line).get("user_id", ""))
                    except ValueError:
                        continue
        user_ids.discard("")
        self.client.users_by_ids(sorted(user_ids))
        users = {
            uid: {
                "username": u.get("username", ""),
                "nickname": u.get("nickname", ""),
                "first_name": u.get("first_name", ""),
                "last_name": u.get("last_name", ""),
            }
            for uid, u in self.client._user_cache.items()
            if uid in user_ids
        }
        (self.out / "users.json").write_text(
            json.dumps(users, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        (self.out / "channels.json").write_text(
            json.dumps(
                {cid: asdict(st) for cid, st in self.manifest.channels.items()},
                ensure_ascii=False,
                indent=1,
            ),
            encoding="utf-8",
        )


# --------------------------------------------------------------------- 대조

# 서버 카운트와 아카이브 건수는 원래 정확히 같지 않다. 세는 대상이 다르기 때문이다(아래 REASONS).
# 그래서 "몇 건 이상 벌어지면 이상하다" 를 상대오차로 잡되, 작은 채널에서 한두 건에 놀라지
# 않도록 바닥값을 둔다.
RECONCILE_TOLERANCE = 0.02
RECONCILE_FLOOR = 5

VERDICTS = ("ok", "over", "gap", "retention", "unknown")

REASONS = {
    "over": "아카이브가 더 많다 — expected 는 plan 시점 스냅샷이라 그 뒤 들어온 글만큼 뒤처지고, "
            "아카이브는 서버 카운터가 세지 않는 system_* 메시지도 그대로 저장한다.",
    "gap":  "아카이브가 적은데 보관 정책으로는 설명되지 않는다 — 백필 미완료·채널 오류를 의심할 것.",
    "retention": "아카이브가 적지만 서버가 과거를 안 주는 채널이다(first_inaccessible_post_time). 정상.",
    "unknown": "서버 카운트(total_msg_count)를 모르는 채널이라 판정을 보류했다.",
}


@dataclass
class ChannelDrift:
    """채널 하나의 건수 대조 결과."""

    channel_id: str
    display: str
    type: str
    path: str
    archived: int          # manifest 가 기억하는 건수
    on_disk: int           # jsonl 에 실제로 있는 건수 (-1 = 세지 않음/읽기 실패)
    expected: int          # 서버 total_msg_count (plan 시점 스냅샷)
    drift: int             # archived - expected
    verdict: str
    truncated: bool
    partial_tail: bool     # 마지막 줄이 잘려 있다 (쓰는 중이거나 프로세스가 중간에 죽었다)
    error: str

    @property
    def disk_drift(self) -> int:
        return 0 if self.on_disk < 0 else self.on_disk - self.archived


def count_posts_on_disk(path: Path) -> tuple[int, bool]:
    """jsonl 의 글 수를 센다. (건수, 마지막 줄이 잘렸는지).

    줄마다 JSON 을 파싱하면 큰 아카이브에서 오래 걸린다. 파일은 append 로만 자라므로 중간 줄은
    온전하고 깨질 수 있는 건 마지막 줄뿐이다 — 그 한 줄만 검사하고 나머지는 세기만 한다.
    """
    if not path.is_file():
        return 0, False
    count = 0
    last = ""
    try:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    count += 1
                    last = line
    except OSError:
        return -1, False
    if not last:
        return 0, False
    try:
        json.loads(last)
    except ValueError:
        return count - 1, True  # 쓰다 만 줄은 글로 치지 않는다
    return count, False


def reconcile(
    manifest: Manifest,
    posts_dir: Path,
    *,
    tolerance: float = RECONCILE_TOLERANCE,
    count_disk: bool = True,
) -> list[ChannelDrift]:
    """서버에 붙지 않고 manifest 만으로 채널별 드리프트를 분류한다. 읽기 전용.

    manifest 는 plan 시점에 받은 서버 카운트(expected)와 보관 정책 컷(inaccessible_before)을
    이미 채널마다 들고 있어서, 그것만으로 "어느 채널이 왜 어긋났는지" 까지 나온다.
    """
    rows: list[ChannelDrift] = []
    for state in manifest.channels.values():
        on_disk, partial = count_posts_on_disk(posts_dir / state.path) if count_disk else (-1, False)
        drift = state.post_count - state.expected
        allowed = max(RECONCILE_FLOOR, int(state.expected * tolerance))
        if state.expected <= 0:
            verdict = "unknown"          # 비교 대상이 없다. 0 과 비교하면 전부 over 로 보인다
        elif abs(drift) <= allowed:
            verdict = "ok"
        elif drift > 0:
            verdict = "over"
        else:
            verdict = "retention" if state.truncated else "gap"
        rows.append(ChannelDrift(
            channel_id=state.channel_id,
            display=state.display,
            type=state.type,
            path=state.path,
            archived=state.post_count,
            on_disk=on_disk,
            expected=state.expected,
            drift=drift,
            verdict=verdict,
            truncated=state.truncated,
            partial_tail=partial,
            error=state.error,
        ))
    rows.sort(key=lambda r: (-abs(r.drift), r.display))
    return rows


def reconcile_summary(rows: list[ChannelDrift]) -> dict:
    return {
        "channels": len(rows),
        "archived": sum(r.archived for r in rows),
        "expected": sum(r.expected for r in rows),
        "drift": sum(r.drift for r in rows),
        "verdicts": {v: sum(1 for r in rows if r.verdict == v) for v in VERDICTS},
        "disk_mismatch": sum(1 for r in rows if r.disk_drift),
        "disk_drift": sum(r.disk_drift for r in rows),
        "truncated": sum(1 for r in rows if r.truncated),
        "partial_tail": sum(1 for r in rows if r.partial_tail),
        "failed": sum(1 for r in rows if r.error),
    }


def refresh_expected(
    client: MattermostClient,
    manifest: Manifest,
    *,
    rate: float = 4.0,
    log: Callable[[str], None] = print,
) -> tuple[int, int]:
    """서버에서 채널별 total_msg_count 를 다시 받아 expected 를 갱신한다. (갱신, 실패) 건수.

    **메모리에서만 갱신하고 manifest 를 저장하지 않는다.** 그 파일의 주인은 돌고 있는 라이브
    수집기다. 밖에서 덮어쓰면 수집기가 들고 있던 커서가 뒤로 밀려 중복이나 누락이 생긴다.
    캐시를 타면 갱신이 되지 않으므로 client.channel() 대신 직접 조회한다.
    """
    limiter = RateLimiter(rate)
    updated = failed = 0
    for state in manifest.channels.values():
        limiter.wait()
        try:
            channel = client.get(f"/channels/{state.channel_id}") or {}
        except ApiError as exc:
            failed += 1
            log(f"  ! {state.display}: {exc}")
            continue
        count = int(channel.get("total_msg_count", 0) or 0)
        if count:
            state.expected = count
            updated += 1
    return updated, failed


# --------------------------------------------------------------------- 첨부파일


def download_attachments(
    client: MattermostClient,
    out_dir: Path,
    *,
    rate: float = 4.0,
    max_mb: float = 25.0,
    log: Callable[[str], None] = print,
) -> tuple[int, int, int]:
    """아카이브된 글에서 참조하는 첨부파일을 내려받는다. 이미 있으면 건너뛴다."""
    posts_dir = out_dir / "posts"
    files_dir = out_dir / "files"
    files_dir.mkdir(parents=True, exist_ok=True)
    limiter = RateLimiter(rate)

    wanted: dict[str, dict] = {}
    for path in posts_dir.rglob("*.jsonl"):
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                try:
                    post = json.loads(line)
                except ValueError:
                    continue
                metas = (post.get("metadata") or {}).get("files") or post.get("_files") or []
                for meta in metas:
                    if meta.get("id"):
                        wanted[meta["id"]] = meta
                for fid in post.get("file_ids") or []:
                    wanted.setdefault(fid, {"id": fid, "name": fid})

    downloaded = skipped = failed = 0
    limit_bytes = int(max_mb * 1024 * 1024)
    for fid, meta in wanted.items():
        name = safe_name(str(meta.get("name") or fid), fid, 80)
        dest = files_dir / f"{fid[:8]}__{name}"
        if dest.exists():
            skipped += 1
            continue
        size = int(meta.get("size") or 0)
        if size and size > limit_bytes:
            skipped += 1
            continue
        try:
            limiter.wait()
            response = client._http.get(f"/files/{fid}")
            if response.status_code >= 400:
                failed += 1
                continue
            dest.write_bytes(response.content)
            downloaded += 1
        except Exception:  # noqa: BLE001 - 개별 첨부 실패가 전체를 막지 않게
            failed += 1
    return downloaded, skipped, failed


# --------------------------------------------------------------------- 렌더링


def render_markdown(out_dir: Path, dest_dir: Path, *, log: Callable[[str], None] = print) -> int:
    """JSONL 아카이브를 사람이 읽는 Markdown 전사본으로 변환한다."""
    from datetime import datetime

    posts_dir = out_dir / "posts"
    users_path = out_dir / "users.json"
    users = json.loads(users_path.read_text(encoding="utf-8")) if users_path.is_file() else {}

    channels_path = out_dir / "channels.json"
    channels = json.loads(channels_path.read_text(encoding="utf-8")) if channels_path.is_file() else {}
    by_path = {st.get("path", ""): st for st in channels.values()}

    def who(user_id: str) -> str:
        info = users.get(user_id) or {}
        return info.get("username") or user_id[:8]

    written = 0
    for path in sorted(posts_dir.rglob("*.jsonl")):
        rel = path.relative_to(posts_dir).as_posix()
        meta = by_path.get(rel, {})
        target = dest_dir / rel.replace(".jsonl", ".md")
        target.parent.mkdir(parents=True, exist_ok=True)

        lines: list[str] = []
        title = meta.get("display") or path.stem
        lines.append(f"# {title}\n")
        if meta.get("team_name"):
            lines.append(f"- 팀: `{meta['team_name']}`")
        lines.append(f"- 채널 ID: `{meta.get('channel_id', '?')}`")
        if meta.get("truncated") or meta.get("inaccessible_before"):
            cut = int(meta.get("inaccessible_before") or 0)
            if cut:
                stamp = datetime.fromtimestamp(cut / 1000).strftime("%Y-%m-%d")
                lines.append(f"- ⚠️ 서버 보관 정책으로 **{stamp} 이전 메시지는 조회 불가**")
        lines.append("")

        last_day = ""
        count = 0
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                try:
                    post = json.loads(line)
                except ValueError:
                    continue
                if str(post.get("type", "")).startswith("system_"):
                    continue
                stamp = datetime.fromtimestamp(post.get("create_at", 0) / 1000)
                day = stamp.strftime("%Y-%m-%d")
                if day != last_day:
                    lines.append(f"\n## {day}\n")
                    last_day = day
                prefix = "  ↳ " if post.get("root_id") else ""
                lines.append(f"**{stamp:%H:%M}** `@{who(post.get('user_id', ''))}`{'  (스레드 답글)' if post.get('root_id') else ''}")
                body = (post.get("message") or "").rstrip()
                lines.append("")
                for text_line in (body.splitlines() or ["*(빈 메시지)*"]):
                    lines.append(f"{prefix}{text_line}")
                attachments = (post.get("metadata") or {}).get("files") or post.get("_files") or []
                if attachments:
                    names = ", ".join(str(a.get("name") or a.get("id")) for a in attachments)
                    lines.append(f"\n> 📎 {names}")
                lines.append("")
                count += 1

        if count == 0:
            continue
        target.write_text("\n".join(lines), encoding="utf-8")
        written += 1
    return written

