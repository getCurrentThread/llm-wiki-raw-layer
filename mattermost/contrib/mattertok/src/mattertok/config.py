"""환경설정 로딩 — .env 파일과 환경변수에서 접속 정보를 읽는다."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from urllib.parse import urlsplit

# 기본 서버 없음: MM_URL 을 반드시 지정해야 한다(예: https://mattermost.example.com).
DEFAULT_URL = ""

# 이름에 아래 문자열이 들어간 채널은 수집하지 않는다(아카이브·실시간 수신 양쪽).
# 기본값은 비어 있다. 패턴은 소유자가 MM_EXCLUDE_CHANNELS(쉼표 구분)로 설정한다.
DEFAULT_EXCLUDED_CHANNELS: tuple[str, ...] = ()

_dotenv_loaded = False


def _parse_dotenv(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        key, sep, val = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        if key:
            values[key] = val
    return values


def load_dotenv() -> Path | None:
    """cwd 에서 위로 올라가며 첫 .env 를 찾아 환경변수로 올린다 (기존 값 우선)."""
    global _dotenv_loaded
    if _dotenv_loaded:
        return None
    _dotenv_loaded = True

    candidates = [Path.cwd(), *Path.cwd().parents, Path(__file__).resolve().parents[2]]
    seen: set[Path] = set()
    for directory in candidates:
        if directory in seen:
            continue
        seen.add(directory)
        env_file = directory / ".env"
        if env_file.is_file():
            for key, val in _parse_dotenv(env_file).items():
                os.environ.setdefault(key, val)
            return env_file
    return None


@dataclass(slots=True)
class Settings:
    base_url: str
    login_id: str | None = None
    password: str | None = None
    mfa_token: str | None = None
    token: str | None = None  # Personal Access Token (있으면 최우선)

    @property
    def host(self) -> str:
        return urlsplit(self.base_url).netloc or "mattermost"

    @property
    def ws_url(self) -> str:
        parts = urlsplit(self.base_url)
        scheme = "wss" if parts.scheme == "https" else "ws"
        return f"{scheme}://{parts.netloc}/api/v4/websocket"


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None


def load_settings() -> Settings:
    load_dotenv()
    base_url = (_clean(os.environ.get("MM_URL")) or DEFAULT_URL).rstrip("/")
    if not base_url:
        raise SystemExit("MM_URL 이 설정되지 않았습니다 (예: MM_URL=https://mattermost.example.com)")
    if "://" not in base_url:
        base_url = "https://" + base_url
    return Settings(
        base_url=base_url,
        login_id=_clean(os.environ.get("MM_LOGIN_ID")),
        password=_clean(os.environ.get("MM_PASSWORD")),
        mfa_token=_clean(os.environ.get("MM_MFA_TOKEN")),
        token=_clean(os.environ.get("MM_TOKEN")),
    )


def excluded_channel_patterns() -> tuple[str, ...]:
    """수집에서 뺄 채널 이름 조각들. MM_EXCLUDE_CHANNELS 로 덮어쓴다."""
    load_dotenv()
    raw = os.environ.get("MM_EXCLUDE_CHANNELS")
    if raw is None:
        return DEFAULT_EXCLUDED_CHANNELS
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def is_excluded_channel(*names: str | None, patterns: Iterable[str] | None = None) -> bool:
    """채널의 표시 이름·raw 이름 중 하나라도 제외 패턴에 걸리면 True.

    이름은 여러 개를 받는다. 마터모스트는 표시 이름(display_name)과 slug(name)가 다르고
    둘 중 한쪽에만 제외 패턴이 들어갈 수 있어서, 한쪽만 보면 새는 채널이 생긴다.
    """
    active = excluded_channel_patterns() if patterns is None else tuple(patterns)
    if not active:
        return False
    haystack = "\n".join(n for n in names if n).casefold()
    return any(pattern.casefold() in haystack for pattern in active)


def state_dir() -> Path:
    """세션 캐시 저장 위치. 프로젝트 밖에 두어 실수로 커밋되는 일을 막는다."""
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_STATE_HOME")
    root = Path(base) if base else Path.home() / ".local" / "state"
    directory = root / "mattertok"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def session_path(host: str) -> Path:
    safe = "".join(ch if ch.isalnum() or ch in "-._" else "_" for ch in host)
    return state_dir() / f"{safe}.session.json"

