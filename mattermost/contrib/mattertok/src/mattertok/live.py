"""실시간 아카이빙 — WebSocket 으로 알림만 받고, 쓰기는 전부 커서 경로로 보낸다.

중복이 나지 않는 이유
--------------------
WebSocket 프레임을 직접 파일에 쓰지 않는다. 프레임은 "이 채널에 뭔가 생겼다"는 **신호**로만 쓰고,
실제 저장은 언제나 `Archiver._forward()` 가 한다. 그 함수는 `?after=<newest_post_id>` 로만 조회하고
서버는 그 post id **이후** 것만 돌려주므로, 같은 신호가 몇 번 오든 이미 쓴 글은 다시 오지 않는다.
커서는 채널마다 manifest 에 원자적으로 저장되므로 재시작해도 이어진다.

즉 중복 방지는 메모리 상의 "본 적 있는 id" 집합(크기 제한이 있고 재시작하면 사라진다)이 아니라
서버가 강제하는 커서 불변식에 기대고 있다. 신호는 놓쳐도 되고(주기적 스윕이 줍는다) 중복돼도 된다
(커서가 무시한다) — 어느 쪽으로 틀려도 안전한 방향으로만 틀린다.

세 겹으로 받는다
---------------
  1. 시작 시 전체 동기화 — 지난번 종료 이후 쌓인 것을 먼저 따라잡는다(백필 미완료 채널은 백필).
  2. WebSocket 신호 → 해당 채널만 debounce 후 증분 동기화 (평상시 경로, 몇 초 지연).
  3. 주기적 전체 스윕 — WS 가 조용히 멎거나, 시스템 메시지처럼 신호가 안 오는 글, 새로 생긴 채널을
     줍는다. WS 를 신뢰하지 않는 안전망이라 이게 없으면 조용히 뒤처진다.

같은 아카이브 폴더에 두 프로세스가 붙으면 커서 불변식이 깨지므로 잠금 파일로 막는다.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import time
from pathlib import Path
from typing import Callable, Iterable

from .archive import Archiver, ChannelState
from .client import ApiError, MattermostClient
from .stream import MattermostStream

HEARTBEAT_SEC = 30
STALE_AFTER_SEC = 150  # 하트비트 3회 + 여유. 이보다 오래된 잠금은 죽은 프로세스가 남긴 것으로 본다

STATUS_FILE = "status.json"
STATUS_STALE_AFTER_SEC = 120  # 하트비트 4회. 이보다 오래 갱신이 없으면 수집기가 멎은 것으로 본다


def write_json_atomic(path: Path, payload: dict) -> None:
    """tmp 에 쓰고 replace — 읽는 쪽이 반쯤 쓰인 파일을 볼 일이 없다 (Manifest.save 와 같은 방식)."""
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def read_status(out_dir: Path) -> dict | None:
    """아카이브 폴더의 status.json. 없거나 깨졌으면 None — 읽는 쪽은 예외를 볼 일이 없다."""
    try:
        data = json.loads((out_dir / STATUS_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def status_health(status: dict | None, *, now: float | None = None) -> tuple[str, int]:
    """('missing' | 'fresh' | 'stale', 마지막 갱신 후 경과 초).

    살아있는지를 pid 로 확인하지 않는 이유는 ArchiveLock 과 같다 — Windows 에서 믿을 수 없다.
    대신 수집기가 하트비트마다 남기는 갱신 시각이 얼마나 오래됐는지만 본다.
    """
    if not status:
        return "missing", 0
    reference = time.time() if now is None else now
    age = max(0, int(reference - int(status.get("updated_at") or 0)))
    return ("fresh" if age <= STATUS_STALE_AFTER_SEC else "stale"), age


class LockBusy(RuntimeError):
    pass


class ArchiveLock:
    """아카이브 폴더 단위 잠금.

    두 수집기가 같은 채널 파일에 동시에 append 하면 커서가 서로를 덮어써서 중복이 난다.
    커서 불변식으로 막을 수 없는 유일한 경우라 여기서 막는다.

    살아있는지는 pid 로 확인하지 않는다 — Windows 의 os.kill 은 신호 0 을 지원하지 않고 프로세스를
    실제로 죽인다. 대신 잠금 파일의 하트비트 시각을 본다.
    """

    def __init__(self, directory: Path) -> None:
        self.path = directory / ".live.lock"
        self._held = False

    def _read(self) -> dict:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _write(self) -> None:
        payload = {
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "heartbeat": int(time.time()),
        }
        tmp = self.path.with_suffix(".lock.tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        tmp.replace(self.path)

    def acquire(self, *, force: bool = False) -> None:
        if self.path.exists() and not force:
            info = self._read()
            age = int(time.time()) - int(info.get("heartbeat") or 0)
            if age < STALE_AFTER_SEC:
                raise LockBusy(
                    f"이미 다른 수집기가 이 폴더를 쓰고 있습니다 "
                    f"(pid {info.get('pid')} @ {info.get('host')}, {age}초 전 신호). "
                    f"동시에 돌리면 중복이 생깁니다. 정말 아무것도 안 돌고 있다면 "
                    f"--force-unlock 을 쓰거나 {self.path} 를 지우세요."
                )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._write()
        self._held = True

    def beat(self) -> None:
        if self._held:
            self._write()

    def release(self) -> None:
        if self._held:
            self.path.unlink(missing_ok=True)
            self._held = False

    def __enter__(self) -> "ArchiveLock":
        return self

    def __exit__(self, *_exc) -> None:
        self.release()


class LiveArchiver:
    def __init__(
        self,
        client: MattermostClient,
        archiver: Archiver,
        *,
        debounce: float = 2.0,
        sweep: float = 300.0,
        log: Callable[[str], None] = print,
    ) -> None:
        self.client = client
        self.archiver = archiver
        self.debounce = max(0.0, debounce)
        self.sweep = max(0.0, sweep)
        self.log = log
        self._dirty: set[str] = set()
        self._sync_lock = asyncio.Lock()  # 동기화는 항상 한 번에 하나만
        self._saved = 0
        self._synced_channels = 0
        # 자기 관측. 밖에서 "제대로 도는지" 를 로그 tail 이나 파일 mtime 없이 알 수 있게 남긴다.
        self.status_path = self.archiver.out / STATUS_FILE
        self._started_at = int(time.time())
        self._last_signal_at = 0
        self._last_write_at = 0
        self._sweep_count = 0
        self._last_error = ""

    # ------------------------------------------------------------------

    @property
    def stats(self) -> tuple[int, int]:
        return self._saved, self._synced_channels

    def _states(self) -> list[ChannelState]:
        return list(self.archiver.manifest.channels.values())

    def mark(self, channel_id: str) -> None:
        self._last_signal_at = int(time.time())
        if channel_id:
            self._dirty.add(channel_id)

    def mark_all(self) -> None:
        self._last_signal_at = int(time.time())  # 재연결도 WS 가 살아있다는 신호다
        self._dirty.update(self.archiver.manifest.channels.keys())

    def write_status(self) -> None:
        """관측용 사이드카를 원자적으로 갱신한다.

        수집이 이것 때문에 죽으면 안 된다 — 상태를 못 남겨도 아카이빙 자체는 멀쩡하므로 예외는
        전부 삼킨다. 수집기가 쥐고 있는 `.live.lock` 과는 별개 파일이라 잠금과 다투지 않는다.
        """
        now = int(time.time())
        try:
            write_json_atomic(self.status_path, {
                "updated_at": now,
                "pid": os.getpid(),
                "host": socket.gethostname(),
                "started_at": self._started_at,
                "uptime_sec": now - self._started_at,
                "saved_session": self._saved,
                "synced_channels": self._synced_channels,
                "dirty_count": len(self._dirty),
                "last_signal_at": self._last_signal_at,
                "last_write_at": self._last_write_at,
                "sweep_count": self._sweep_count,
                "last_error": self._last_error,
                "channels_total": len(self.archiver.manifest.channels),
            })
        except Exception:  # noqa: BLE001 - 관측이 수집을 막으면 본말전도
            pass

    # ------------------------------------------------------------------

    def _sync_one(self, state: ChannelState) -> int:
        """한 채널을 커서 기준으로 따라잡는다. 새로 쓴 글 수를 돌려준다."""
        before = state.post_count
        try:
            self.archiver._sync_channel(state)
            state.error = ""
        except ApiError as exc:
            state.error = str(exc)
            self._last_error = f"{state.display}: {exc}"
            self.log(f"  ! {state.display}: {exc}")
            return 0
        gained = state.post_count - before
        if gained:
            self.archiver.manifest.save(self.archiver.manifest_path)
        return gained

    async def _drain(self) -> None:
        """더러워진 채널만 동기화."""
        async with self._sync_lock:
            while self._dirty:
                channel_id = self._dirty.pop()
                state = self.archiver.manifest.channels.get(channel_id)
                if state is None:
                    continue  # 계획에 없는 채널(제외됐거나 타입 밖) — 신호는 무시
                gained = await asyncio.to_thread(self._sync_one, state)
                if gained:
                    self._saved += gained
                    self._synced_channels += 1
                    self._last_write_at = int(time.time())
                    self.log(f"  + {state.display}: {gained}건")

    async def _full_sweep(self, *, rediscover: bool = True) -> None:
        """WS 를 신뢰하지 않는 안전망. 새 채널까지 포함해 전 채널을 훑는다."""
        self._sweep_count += 1
        async with self._sync_lock:
            if rediscover:
                try:
                    channels = await asyncio.to_thread(self.archiver.discover)
                    states = await asyncio.to_thread(self.archiver.plan, channels)
                    for state in states:
                        self.archiver.manifest.channels.setdefault(state.channel_id, state)
                except ApiError as exc:
                    self.log(f"  ! 채널 재탐색 실패: {exc}")
            gained_total = 0
            for state in self._states():
                gained = await asyncio.to_thread(self._sync_one, state)
                gained_total += gained
                if gained:
                    self._saved += gained
                    self._last_write_at = int(time.time())
                    self.log(f"  + {state.display}: {gained}건")
            if gained_total:
                self.log(f"  스윕에서 {gained_total}건 추가")

    # ------------------------------------------------------------------ 루프

    async def _debounce_loop(self) -> None:
        while True:
            await asyncio.sleep(self.debounce or 0.5)
            if self._dirty:
                await self._drain()

    async def _sweep_loop(self, lock: ArchiveLock) -> None:
        elapsed = 0.0
        while True:
            await asyncio.sleep(HEARTBEAT_SEC)
            lock.beat()
            self.write_status()  # 잠금 하트비트와 같은 박자로. 밖에서는 이 파일만 보면 된다
            elapsed += HEARTBEAT_SEC
            if self.sweep and elapsed >= self.sweep:
                elapsed = 0.0
                await self._full_sweep()

    async def run(self, lock: ArchiveLock, *, on_status: Callable[[str], None]) -> None:
        stream = MattermostStream(
            self.client,
            include_own=True,      # 내가 쓴 글도 아카이브 대상이다
            gap_fill=False,        # 시간 기반 갭 메우기는 겹친다. 커서 기반 스윕이 정확하다
            on_status=on_status,
            on_reconnect=self.mark_all,
            exclude=self.archiver.exclude,
        )
        self.write_status()  # 첫 하트비트까지 기다리지 않고 시작 즉시 관측 가능하게
        tasks = [
            asyncio.create_task(stream.run(lambda msg: self.mark(msg.channel_id))),
            asyncio.create_task(self._debounce_loop()),
            asyncio.create_task(self._sweep_loop(lock)),
        ]
        try:
            await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                task.cancel()
            # 종료는 빨라야 한다. 서비스 관리자는 몇 초만 기다려주고 그 다음엔 프로세스를 죽인다.
            # 죽어도 데이터는 안전하지만(커서가 지켜준다) 잠금 파일을 못 지우고 나가게 된다.
            #
            # 그래서 전체 스윕도 finalize 도 하지 않는다. 남은 신호만 비우면 충분하다:
            #   - 신호가 안 온 채널은 새 글이 없다는 뜻이고, 혹시 놓쳤어도 다음 시작 때 따라잡는다.
            #   - finalize 는 중복 제거 + 정렬인데, 중복은 애초에 없고 실시간 추가분은 이미 시간순이다.
            #     전체 정렬이 필요하면 `mattertok archive` 가 한다.
            await self._drain()
            self.archiver.manifest.save(self.archiver.manifest_path)
            self.write_status()  # 마지막 숫자를 남긴다. 이후 갱신이 멎으므로 곧 STALE 로 보인다


def build(
    client: MattermostClient,
    out_dir: Path,
    *,
    rate: float = 4.0,
    slim: bool = False,
    types: Iterable[str] = ("O", "P", "D", "G"),
    include_empty: bool = False,
    exclude: Iterable[str] | None = None,
    log: Callable[[str], None] = print,
) -> Archiver:
    out_dir.mkdir(parents=True, exist_ok=True)
    return Archiver(
        client, out_dir, rate=rate, slim=slim, types=types,
        include_empty=include_empty, log=log, exclude=exclude,
    )

