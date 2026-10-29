"""mattertok — 웹훅 없이 Mattermost 메시지를 읽고 보내는 클라이언트.

라이브러리로 쓸 때:

    from mattertok import connect

    with connect() as mm:                       # .env 또는 저장된 세션으로 인증
        ch = mm.resolve_channel("#공지")
        for post in mm.posts(ch["id"], limit=20):
            print(mm.username(post["user_id"]), post["message"])
        mm.create_post(ch["id"], "안녕하세요")

실시간 수신:

    import asyncio
    from mattertok import connect, watch

    with connect() as mm:
        asyncio.run(watch(mm, lambda m: print(m.sender_name, m.message)))
"""

from .archive import Archiver, Manifest, download_attachments, render_markdown
from .auth import AuthError, Session, get_session
from .client import ApiError, MattermostClient, NotFound
from .config import Settings, load_settings
from .stream import Incoming, MattermostStream, watch

__all__ = [
    "ApiError",
    "Archiver",
    "AuthError",
    "Incoming",
    "Manifest",
    "download_attachments",
    "render_markdown",
    "MattermostClient",
    "MattermostStream",
    "NotFound",
    "Session",
    "Settings",
    "connect",
    "get_session",
    "load_settings",
    "watch",
]

__version__ = "0.1.0"


def connect(*, force_login: bool = False, interactive: bool = True) -> MattermostClient:
    """설정을 읽어 인증된 클라이언트를 만든다."""
    settings = load_settings()
    session = get_session(settings, force_login=force_login, interactive=interactive)
    return MattermostClient(session)

