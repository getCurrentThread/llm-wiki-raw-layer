"""인증 — Personal Access Token 또는 ID/비밀번호 로그인으로 세션 토큰을 확보한다.

Mattermost 는 `POST /api/v4/users/login` 의 **응답 헤더** `Token` 으로 세션 토큰을 준다.
이후 모든 요청에는 `Authorization: Bearer <token>` 을 붙인다.
"""

from __future__ import annotations

import getpass
import json
import sys
import time
from dataclasses import dataclass, asdict
from pathlib import Path

import httpx

from .config import Settings, session_path


class AuthError(RuntimeError):
    pass


@dataclass(slots=True)
class Session:
    base_url: str
    token: str
    user_id: str
    username: str
    obtained_at: int  # epoch seconds
    source: str  # "pat" | "login" | "cache"

    def redacted(self) -> str:
        tail = self.token[-4:] if len(self.token) > 4 else "?"
        return f"…{tail}"


def _error_detail(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text[:200]
    parts = [str(body.get("message") or "").strip(), str(body.get("id") or "").strip()]
    return " | ".join(p for p in parts if p) or response.text[:200]


def verify_token(base_url: str, token: str, *, timeout: float = 20.0) -> dict | None:
    """토큰이 아직 살아있는지 확인하고, 유효하면 내 user 객체를 돌려준다."""
    try:
        response = httpx.get(
            f"{base_url}/api/v4/users/me",
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
        )
    except httpx.HTTPError:
        return None
    if response.status_code == 200:
        return response.json()
    return None


def _load_cached(settings: Settings) -> Session | None:
    path = session_path(settings.host)
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        cached = Session(**raw)
    except (ValueError, TypeError):
        return None
    if cached.base_url.rstrip("/") != settings.base_url.rstrip("/"):
        return None
    return cached


def _save(session: Session) -> Path:
    path = session_path(session.base_url.split("://", 1)[-1].split("/", 1)[0])
    path.write_text(json.dumps(asdict(session), ensure_ascii=False, indent=2), encoding="utf-8")
    _restrict_permissions(path)
    return path


def _restrict_permissions(path: Path) -> None:
    """토큰 파일 접근을 최소한으로 제한 (실패해도 치명적이지 않음).

    현재 사용자만 남기면 안 된다. 같은 아카이브를 대화형으로도 돌리고 서비스(LocalSystem)로도
    돌리는데, 나중에 저장한 쪽이 앞의 쪽을 잠가버린다 — 실제로 서비스가 세션 파일을 못 읽어
    PermissionError 로 죽었다. 그래서 SYSTEM 과 Administrators 를 함께 남긴다. 둘 다 어차피
    소유권을 가져올 수 있는 주체라 실질적인 노출 증가는 없다.
    """
    try:
        if sys.platform == "win32":
            import subprocess

            grants: list[str] = []
            for principal in (getpass.getuser(), "SYSTEM", "Administrators"):
                grants += ["/grant:r", f"{principal}:F"]
            subprocess.run(
                ["icacls", str(path), "/inheritance:r", *grants],
                check=False,
                capture_output=True,
            )
        else:
            path.chmod(0o600)
    except Exception:  # noqa: BLE001 - 권한 강화 실패는 무시
        pass


def _prompt(label: str, hint: str, *, interactive: bool, secret: bool = False) -> str:
    """대화형 입력. stdin 이 없으면 EOFError 대신 설명이 붙은 AuthError 를 던진다.

    isatty() 는 일부 환경(래핑된 콘솔 등)에서 True 를 돌려주고도 읽으면 EOF 가 나므로
    실제 읽기 실패까지 잡아야 안전하다.
    """
    if interactive:
        try:
            value = (getpass.getpass(label) if secret else input(label)).strip()
        except (EOFError, KeyboardInterrupt):
            value = ""
        if value:
            return value
    raise AuthError(hint)


def clear_session(settings: Settings) -> bool:
    path = session_path(settings.host)
    if path.is_file():
        path.unlink()
        return True
    return False


def login(settings: Settings, *, interactive: bool = True) -> Session:
    """ID/비밀번호로 새 세션을 발급받는다."""
    login_id = settings.login_id
    password = settings.password
    mfa = settings.mfa_token

    interactive = interactive and sys.stdin is not None

    if not login_id:
        login_id = _prompt(
            "로그인 ID (이메일 또는 사용자명): ",
            "로그인 ID 가 없습니다. .env 에 MM_LOGIN_ID 를 넣거나, "
            "직접 터미널을 열어 `mattertok login` 을 실행하세요.",
            interactive=interactive,
        )
    if not password:
        password = _prompt(
            "비밀번호 (입력해도 화면에 안 보입니다): ",
            "비밀번호가 없습니다. .env 에 MM_PASSWORD 를 넣거나, "
            "직접 터미널을 열어 `mattertok login` 을 실행하세요.",
            interactive=interactive,
            secret=True,
        )

    body: dict[str, str] = {"login_id": login_id, "password": password}
    if mfa:
        body["token"] = mfa

    for attempt in range(2):
        try:
            response = httpx.post(
                f"{settings.base_url}/api/v4/users/login", json=body, timeout=30.0
            )
        except httpx.HTTPError as exc:
            raise AuthError(f"서버에 연결하지 못했습니다: {exc}") from exc

        if response.status_code == 200:
            break

        detail = _error_detail(response)
        if "mfa" in detail.lower() and "token" not in body and attempt == 0:
            body["token"] = _prompt(
                "MFA 6자리 코드: ",
                "MFA 코드가 필요합니다. .env 의 MM_MFA_TOKEN 에 6자리 코드를 넣으세요.",
                interactive=interactive,
            )
            continue
        if response.status_code == 429:
            raise AuthError("로그인 시도가 서버 rate limit 에 걸렸습니다. 잠시 후 다시 시도하세요.")
        if response.status_code == 401:
            raise AuthError(
                f"로그인 실패 (401): {detail}\n"
                "  ID/비밀번호를 확인하세요. 서버에 로그인 시도 횟수 제한이 있으니 "
                "반복 실패는 계정 잠금으로 이어질 수 있습니다."
            )
        raise AuthError(f"로그인 실패 ({response.status_code}): {detail}")
    else:
        raise AuthError("로그인에 실패했습니다.")

    token = response.headers.get("Token")
    if not token:
        raise AuthError("응답에 Token 헤더가 없습니다. 서버 설정을 확인하세요.")

    me = response.json()
    session = Session(
        base_url=settings.base_url,
        token=token,
        user_id=me["id"],
        username=me.get("username", "?"),
        obtained_at=int(time.time()),
        source="login",
    )
    _save(session)
    return session


def get_session(settings: Settings, *, force_login: bool = False, interactive: bool = True) -> Session:
    """PAT → 캐시된 세션 → 새 로그인 순으로 사용 가능한 토큰을 확보한다."""
    if settings.token and not force_login:
        me = verify_token(settings.base_url, settings.token)
        if me is None:
            raise AuthError(
                "MM_TOKEN 이 설정돼 있지만 서버가 거부했습니다. "
                "토큰이 만료·폐기됐거나 서버에서 Personal Access Token 이 꺼져 있습니다."
            )
        return Session(
            base_url=settings.base_url,
            token=settings.token,
            user_id=me["id"],
            username=me.get("username", "?"),
            obtained_at=int(time.time()),
            source="pat",
        )

    if not force_login:
        cached = _load_cached(settings)
        if cached is not None:
            me = verify_token(settings.base_url, cached.token)
            if me is not None:
                cached.source = "cache"
                cached.username = me.get("username", cached.username)
                return cached

    return login(settings, interactive=interactive)

