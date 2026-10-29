"""mattertok CLI — 조회 / 검색 / 실시간 수신 / 전송."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
import unicodedata
from datetime import datetime, timedelta

from . import auth
from .client import ApiError, MattermostClient
from .config import is_excluded_channel, load_dotenv, load_settings
from .stream import Incoming, watch

_COLOR = sys.stdout.isatty() and not os.environ.get("NO_COLOR")


def c(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _COLOR else text


dim = lambda s: c(s, "2")  # noqa: E731
bold = lambda s: c(s, "1")  # noqa: E731
cyan = lambda s: c(s, "36")  # noqa: E731
green = lambda s: c(s, "32")  # noqa: E731
yellow = lambda s: c(s, "33")  # noqa: E731
red = lambda s: c(s, "31")  # noqa: E731


def ts(epoch_ms: int, *, with_date: bool = True) -> str:
    dt = datetime.fromtimestamp(epoch_ms / 1000)
    return dt.strftime("%Y-%m-%d %H:%M" if with_date else "%H:%M:%S")


def cells(text: str) -> int:
    """터미널에서 차지하는 칸 수. 한글·전각 문자는 2칸이라 len() 으로는 표가 밀린다."""
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)


def pad(text: str, width: int, *, right: bool = False) -> str:
    """표시 폭 기준 정렬. 넘치면 자른다 — 한글 채널 이름이 섞여도 열이 맞는다."""
    out, used = "", 0
    for ch in text:
        step = 2 if unicodedata.east_asian_width(ch) in "WF" else 1
        if used + step > width:
            break
        out, used = out + ch, used + step
    fill = " " * (width - used)
    return fill + out if right else out + fill


def human_seconds(seconds: float) -> str:
    """초를 사람이 읽는 길이로. 큰 단위 두 개까지만 (95 → '1분 35초')."""
    total = max(0, int(seconds))
    if total < 60:
        return f"{total}초"
    minutes, sec = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}분 {sec}초"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}시간 {minutes}분"
    days, hours = divmod(hours, 24)
    return f"{days}일 {hours}시간"


_DURATION = re.compile(r"^(\d+)\s*([smhdw])$", re.I)
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def parse_since(value: str | None) -> int | None:
    """'2h', '30m', '3d' 또는 'YYYY-MM-DD' 를 epoch ms 로."""
    if not value:
        return None
    match = _DURATION.match(value.strip())
    if match:
        amount, unit = int(match.group(1)), match.group(2).lower()
        return int((time.time() - amount * _UNIT_SECONDS[unit]) * 1000)
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return int(datetime.strptime(value.strip(), fmt).timestamp() * 1000)
        except ValueError:
            continue
    raise SystemExit(f"--since 형식을 이해하지 못했습니다: {value!r} (예: 2h, 30m, 3d, 2026-08-01)")


def open_client(*, force_login: bool = False) -> MattermostClient:
    settings = load_settings()
    session = auth.get_session(settings, force_login=force_login)
    return MattermostClient(session)


# --------------------------------------------------------------------- 명령들


def cmd_doctor(args: argparse.Namespace) -> int:
    import httpx

    env_file = load_dotenv()
    settings = load_settings()
    print(bold("접속 대상"), settings.base_url)
    print(bold(".env"), str(env_file) if env_file else dim("(없음 — 환경변수만 사용)"))

    try:
        ping = httpx.get(f"{settings.base_url}/api/v4/system/ping", timeout=15).json()
        print(green("✓"), "서버 ping:", ping.get("status", "?"))
    except Exception as exc:  # noqa: BLE001
        print(red("✗"), "서버에 연결하지 못했습니다:", exc)
        return 1

    try:
        cfg = httpx.get(
            f"{settings.base_url}/api/v4/config/client?format=old", timeout=20
        ).json()
        print(green("✓"), "서버 버전:", cfg.get("Version", "?"))
        for key in ("EnableSignUpWithEmail", "EnableBotAccountCreation", "EnforceMultifactorAuthentication"):
            print("   ", dim(f"{key} = {cfg.get(key)}"))
    except Exception as exc:  # noqa: BLE001
        print(yellow("!"), "클라이언트 설정 조회 실패:", exc)

    cached = auth.session_path(settings.host)
    creds = (
        "MM_TOKEN (Personal Access Token)"
        if settings.token
        else "MM_LOGIN_ID/MM_PASSWORD (.env)"
        if settings.login_id and settings.password
        else "저장된 세션"
        if cached.is_file()
        else None
    )
    if creds is None:
        print(bold("자격증명"), red("없음"))
        print()
        print(yellow("다음 단계:"), "`.env.example` 를 `.env` 로 복사해 MM_LOGIN_ID / MM_PASSWORD 를 채우거나,")
        print("          터미널에서", bold("mattertok login"), "을 실행해 대화형으로 로그인하세요.")
        return 1
    print(bold("자격증명"), creds)

    try:
        with open_client() as client:
            me = client.me()
            print(green("✓"), f"인증 성공: @{me['username']} ({me.get('email', '')})")
            teams = client.my_teams()
            print(green("✓"), f"소속 팀 {len(teams)}개:", ", ".join(t.get("display_name", "?") for t in teams[:5]))
    except auth.AuthError as exc:
        print(red("✗"), exc)
        return 1
    except ApiError as exc:
        print(red("✗"), exc)
        return 1
    return 0


def cmd_login(args: argparse.Namespace) -> int:
    settings = load_settings()
    session = auth.get_session(settings, force_login=args.force)
    label = {"pat": "Personal Access Token", "login": "새 로그인", "cache": "저장된 세션"}[session.source]
    print(green("✓"), f"@{session.username} 로 인증됨 ({label}, 토큰 {session.redacted()})")
    if session.source == "login":
        print(dim(f"  세션 저장 위치: {auth.session_path(settings.host)}"))
    return 0


def cmd_logout(args: argparse.Namespace) -> int:
    settings = load_settings()
    print(green("✓ 저장된 세션을 지웠습니다.") if auth.clear_session(settings) else dim("지울 세션이 없습니다."))
    return 0


def cmd_whoami(args: argparse.Namespace) -> int:
    with open_client() as client:
        me = client.me()
        print(f"{bold('@' + me['username'])}  {me.get('first_name', '')} {me.get('last_name', '')}".rstrip())
        print(dim(f"  id={me['id']}  email={me.get('email', '')}"))
    return 0


def cmd_teams(args: argparse.Namespace) -> int:
    with open_client() as client:
        for team in client.my_teams():
            print(f"{bold(team.get('display_name', '?'))}  {dim(team.get('name', ''))}")
    return 0


def cmd_channels(args: argparse.Namespace) -> int:
    from .client import CHANNEL_TYPE_LABEL

    with open_client() as client:
        teams = client.my_teams()
        if args.team:
            teams = [t for t in teams if args.team.lower() in (t.get("name", "") + t.get("display_name", "")).lower()]
        for team in teams:
            print(bold(f"\n[{team.get('display_name', '?')}]"))
            channels = sorted(
                client.my_channels(team["id"]),
                key=lambda ch: (ch.get("type", ""), ch.get("display_name") or ch.get("name", "")),
            )
            for ch in channels:
                if ch.get("type") == "D" and not args.all:
                    continue
                if args.filter and args.filter.lower() not in (
                    (ch.get("display_name") or "") + ch.get("name", "")
                ).lower():
                    continue
                kind = CHANNEL_TYPE_LABEL.get(ch.get("type", ""), "?")
                name = ch.get("display_name") or client.channel_label(ch["id"])
                mark = ""
                if is_excluded_channel(ch.get("display_name"), ch.get("name")):
                    mark = dim(" [수집 제외]")
                print(f"  {cyan(name):<40} {dim(kind):<6} {dim(team.get('name', '') + '/' + ch.get('name', ''))}{mark}")
    return 0


def cmd_unread(args: argparse.Namespace) -> int:
    with open_client() as client:
        found = False
        for channel, count in client.unread_channels():
            found = True
            name = channel.get("display_name") or client.channel_label(channel["id"])
            print(f"{yellow(str(count) + '건'):>8}  {cyan(name)}  {dim(channel['_team_name'] + '/' + channel.get('name', ''))}")
        if not found:
            print(dim("읽지 않은 메시지가 없습니다."))
    return 0


def print_post(client: MattermostClient, post: dict, *, show_channel: bool = False) -> None:
    author = client.username(post.get("user_id", ""))
    head = f"{dim(ts(post.get('create_at', 0)))}  {green('@' + author)}"
    if show_channel:
        head += f"  {cyan(client.channel_label(post.get('channel_id', '')))}"
    if post.get("root_id"):
        head += dim("  ↳스레드")
    print(head)
    body = post.get("message", "").strip()
    for line in (body.splitlines() or [dim("(빈 메시지)")]):
        print(f"    {line}")
    files = post.get("metadata", {}).get("files") or post.get("file_ids") or []
    if files:
        print(dim(f"    📎 첨부 {len(files)}개"))
    print()


def cmd_read(args: argparse.Namespace) -> int:
    with open_client() as client:
        channel = client.resolve_channel(args.channel)
        label = channel.get("display_name") or client.channel_label(channel["id"])
        since = parse_since(args.since)
        posts = client.posts(channel["id"], limit=args.number, since=since)
        scope = f"최근 {len(posts)}건" if since is None else f"{args.since} 이후 {len(posts)}건"
        print(bold(f"── {label} — {scope} ──\n"))
        for post in posts:
            print_post(client, post)
        if args.mark_read and posts:
            client.mark_viewed(channel["id"])
            print(dim("읽음 처리했습니다."))
    return 0


def cmd_thread(args: argparse.Namespace) -> int:
    with open_client() as client:
        posts = client.thread(args.post_id)
        print(bold(f"── 스레드 ({len(posts)}건) ──\n"))
        for post in posts:
            print_post(client, post)
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    with open_client() as client:
        if args.team:
            teams = [t for t in client.my_teams() if args.team.lower() in (t.get("name", "") + t.get("display_name", "")).lower()]
            if not teams:
                print(red(f"'{args.team}' 팀을 찾지 못했습니다."))
                return 1
            posts = client.search(teams[0]["id"], args.query, is_or=args.any, per_page=args.number)
        else:
            posts = client.search_all_teams(args.query, is_or=args.any, per_page=args.number)
        posts = posts[: args.number]
        print(bold(f"── '{args.query}' 검색 결과 {len(posts)}건 ──\n"))
        for post in posts:
            print_post(client, post, show_channel=True)
        if not posts:
            print(dim("일치하는 메시지가 없습니다."))
    return 0


def cmd_send(args: argparse.Namespace) -> int:
    message = " ".join(args.message).strip()
    if not message and not sys.stdin.isatty():
        message = sys.stdin.read().strip()
    if not message:
        print(red("보낼 메시지가 비어 있습니다."))
        return 1

    with open_client() as client:
        channel = client.resolve_channel(args.channel)
        label = channel.get("display_name") or client.channel_label(channel["id"])
        if args.dry_run:
            print(yellow("[dry-run]"), f"{cyan(label)} 로 보낼 내용:")
            for line in message.splitlines():
                print(f"    {line}")
            return 0
        post = client.create_post(channel["id"], message, root_id=args.thread)
        print(green("✓"), f"{cyan(label)} 에 전송 완료  {dim(post['id'])}")
    return 0


def cmd_watch(args: argparse.Namespace) -> int:
    with open_client() as client:
        channel_ids = None
        if args.channels:
            resolved = [client.resolve_channel(spec) for spec in args.channels]
            channel_ids = [ch["id"] for ch in resolved]
            names = ", ".join(
                ch.get("display_name") or client.channel_label(ch["id"]) for ch in resolved
            )
            print(bold(f"── 실시간 수신: {names} ──"))
        else:
            print(bold("── 실시간 수신: 내가 속한 모든 채널 ──"))
        print(dim("Ctrl+C 로 종료\n"))

        def on_message(msg: Incoming) -> None:
            tag = yellow(" [놓친 메시지]") if msg.replayed else ""
            channel = msg.channel_name or client.channel_label(msg.channel_id)
            sender = msg.sender_name or client.username(msg.user_id)
            print(
                f"{dim(ts(msg.create_at, with_date=False))} "
                f"{cyan(channel)} {green('@' + sender)}{tag}"
            )
            for line in msg.message.splitlines() or [""]:
                print(f"    {line}")
            print(flush=True)

        try:
            asyncio.run(
                watch(
                    client,
                    on_message,
                    channel_ids=channel_ids,
                    include_own=args.include_own,
                    on_status=lambda s: print(dim(f"· {s}"), flush=True),
                )
            )
        except KeyboardInterrupt:
            print(dim("\n종료합니다."))
    return 0


def cmd_live(args: argparse.Namespace) -> int:
    """실시간 아카이빙. 쓰기는 전부 커서 경로로 가므로 신호가 겹쳐도 중복이 안 생긴다."""
    from pathlib import Path

    from .live import ArchiveLock, LiveArchiver, LockBusy, build

    out = Path(args.out).expanduser().resolve()
    types = [t.strip().upper() for t in args.types.split(",") if t.strip()]
    invalid = [t for t in types if t not in {"O", "P", "D", "G"}]
    if invalid:
        print(red(f"--types 값이 잘못됐습니다: {invalid} (O=공개 P=비공개 D=DM G=그룹DM)"))
        return 1

    lock = ArchiveLock(out)
    try:
        out.mkdir(parents=True, exist_ok=True)
        lock.acquire(force=args.force_unlock)
    except LockBusy as exc:
        print(red(str(exc)))
        return 1

    try:
        with open_client() as client:
            exclude = () if args.include_excluded else None
            archiver = build(
                client, out, rate=args.rate, slim=args.slim, types=types,
                include_empty=args.include_empty, exclude=exclude,
                log=lambda m: print(m, flush=True),
            )
            if archiver.exclude:
                print(dim(f"제외 정책: 이름에 {', '.join(archiver.exclude)} 가 들어간 채널은 받지 않습니다."))

            print(dim("채널 목록을 수집하는 중…"), flush=True)
            states = archiver.plan(archiver.discover())
            pending = [s for s in states if not s.backfill_done]
            print(bold(f"대상 채널 {len(states)}개") + dim(f"  (백필 필요 {len(pending)}개)"))

            print(dim("\n밀린 메시지를 먼저 따라잡는 중…"), flush=True)
            started = time.time()
            archiver.run(states)
            baseline = sum(s.post_count for s in archiver.manifest.channels.values())
            print(f"  현재 {baseline:,}건 · {(time.time() - started) / 60:.1f}분")

            live = LiveArchiver(
                client, archiver, debounce=args.debounce, sweep=args.sweep,
                log=lambda m: print(m, flush=True),
            )
            print(bold("\n── 실시간 수집 시작 ──"))
            print(dim(f"신호 후 {args.debounce:g}초 내 저장 · "
                      f"{args.sweep:g}초마다 전체 확인 · Ctrl+C 로 종료\n"))
            try:
                asyncio.run(live.run(lock, on_status=lambda st: print(dim(f"· {st}"), flush=True)))
            except KeyboardInterrupt:
                pass

            saved, _ = live.stats
            total = sum(s.post_count for s in archiver.manifest.channels.values())
            print(green(f"\n✓ 종료. 실시간으로 {saved:,}건 추가 (총 {total:,}건)"))
            print(dim("  파일은 받은 순서대로 쌓입니다. 시간순 정렬이 필요하면 archive 를 한 번 돌리세요"))
            print(dim("  (finalize 가 id 기준으로 정리합니다). 중복은 커서 때문에 애초에 생기지 않습니다."))
    finally:
        lock.release()
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    """실시간 수집기가 남긴 status.json 만 읽는다.

    서버에도 접속하지 않고 잠금 파일에도 손대지 않는다. 그래서 수집기가 돌고 있는 중에
    언제든 안전하게 부를 수 있다.
    """
    from pathlib import Path

    from .live import HEARTBEAT_SEC, STATUS_FILE, read_status, status_health

    out = Path(args.out).expanduser().resolve()
    status = read_status(out)
    health, age = status_health(status)

    if args.json:
        print(json.dumps(status or {}, ensure_ascii=False, indent=1))
        return 0 if health == "fresh" else 1

    if health == "missing":
        print(yellow("라이브 수집기가 이 폴더에서 돈 적이 없습니다."))
        print(dim(f"  찾은 위치  {out / STATUS_FILE}"))
        print(dim(f"  `mattertok live -o {args.out}` 로 시작하면 {HEARTBEAT_SEC}초마다 상태를 남깁니다."))
        return 1

    if health == "stale":
        print(red(f"⚠️ STALE — {human_seconds(age)}째 갱신이 없습니다. 수집기가 죽었을 수 있습니다."))
    else:
        print(green(f"정상 가동 — {human_seconds(age)} 전 갱신"))

    def since(epoch: int) -> str:
        if not epoch:
            return dim("없음")
        return f"{human_seconds(time.time() - epoch)} 전  {dim(ts(epoch * 1000))}"

    num = lambda key: int(status.get(key) or 0)  # noqa: E731

    print(f"  프로세스   pid {status.get('pid', '?')} @ {status.get('host', '?')}")
    print(f"  가동시간   {human_seconds(num('uptime_sec'))}  {dim('(시작 ' + ts(num('started_at') * 1000) + ')')}")
    print(f"  세션실적   저장 {num('saved_session'):,}건 · 동기화 채널 {num('synced_channels'):,}개 "
          f"· 전체 스윕 {num('sweep_count'):,}회")
    print(f"  최근신호   {since(num('last_signal_at'))}")
    print(f"  최근저장   {since(num('last_write_at'))}")
    print(f"  대기채널   {num('dirty_count')}개 · 아카이브 채널 {num('channels_total'):,}개")
    print(f"  최근오류   {status.get('last_error') or dim('(없음)')}")
    print(dim(f"  상태파일   {out / STATUS_FILE}"))
    return 0 if health == "fresh" else 1


def cmd_archive(args: argparse.Namespace) -> int:
    from pathlib import Path

    from .archive import Archiver, download_attachments, render_markdown

    out = Path(args.out).expanduser().resolve()
    types = [t.strip().upper() for t in args.types.split(",") if t.strip()]
    invalid = [t for t in types if t not in {"O", "P", "D", "G"}]
    if invalid:
        print(red(f"--types 값이 잘못됐습니다: {invalid} (O=공개 P=비공개 D=DM G=그룹DM)"))
        return 1

    with open_client() as client:
        out.mkdir(parents=True, exist_ok=True)
        # --include-excluded 는 제외 정책을 끄는 유일한 문. 기본은 config 의 정책을 따른다.
        exclude = () if args.include_excluded else None
        archiver = Archiver(
            client, out, rate=args.rate, slim=args.slim, types=types,
            include_empty=args.include_empty, log=lambda s: print(s, flush=True),
            exclude=exclude,
        )
        if archiver.exclude:
            print(dim(f"제외 정책: 이름에 {', '.join(archiver.exclude)} 가 들어간 채널은 받지 않습니다."))
        else:
            print(yellow("제외 정책 해제됨 (--include-excluded)"))

        print(dim("채널 목록을 수집하는 중…"), flush=True)
        channels = archiver.discover()
        if args.channels:
            wanted = {client.resolve_channel(spec)["id"] for spec in args.channels}
            channels = [c for c in channels if c["id"] in wanted]
        states = archiver.plan(channels)

        pending = [s for s in states if not s.backfill_done]
        done = [s for s in states if s.backfill_done]
        expected = sum(s.expected for s in states)
        est_requests = sum(max(1, -(-s.expected // 200)) for s in pending) + len(done)

        print(bold(f"\n대상 채널 {len(states)}개") + dim(f"  (전체 {len(channels)}개 중 필터 적용)"))
        print(f"  백필 필요 {len(pending)}개 / 이미 완료 {len(done)}개")
        print(f"  서버 기준 메시지 약 {expected:,}건, 예상 요청 약 {est_requests:,}회")
        if args.rate > 0:
            print(dim(f"  초당 {args.rate}회 제한 → 최소 {est_requests / args.rate / 60:.1f}분 예상"))
        print(dim(f"  저장 위치: {out}"))

        if args.dry_run:
            print(yellow("\n[dry-run] 실제로 받지 않고 계획만 표시했습니다."))
            for state in states[:20]:
                mark = green("완료") if state.backfill_done else dim("대기")
                print(f"  {mark}  {state.display:<32} {state.expected:>7,}건  {dim(state.path)}")
            if len(states) > 20:
                print(dim(f"  … 외 {len(states) - 20}개"))
            return 0

        print()
        started = time.time()
        counter = {"posts": 0}

        def on_progress(index: int, total: int, state) -> None:
            counter["posts"] += 0
            elapsed = time.time() - started
            eta = (elapsed / index) * (total - index) if index else 0
            status = red("실패") if state.error else (green("✓") if state.backfill_done else yellow("부분"))
            total_posts = sum(s.post_count for s in states)
            print(
                f"  [{index:>3}/{total}] {status} {state.display[:34]:<34} "
                f"{state.post_count:>6,}건  {dim(f'누적 {total_posts:,} · 남은시간 {eta / 60:.1f}분')}",
                flush=True,
            )

        try:
            archiver.run(states, on_progress=on_progress)
        except KeyboardInterrupt:
            print(yellow("\n중단했습니다. 진행 상황은 저장됐으니 같은 명령으로 이어받을 수 있습니다."))
            return 130

        print(dim("\n중복 제거 및 시간순 정렬 중…"), flush=True)
        files, removed = archiver.finalize()
        archiver.write_sidecars()

        total_posts = sum(s.post_count for s in archiver.manifest.channels.values())
        truncated = [s for s in archiver.manifest.channels.values() if s.truncated]
        failed = [s for s in archiver.manifest.channels.values() if s.error]

        print(green("\n✓ 아카이브 완료"))
        print(f"  파일 {files}개 · 메시지 {total_posts:,}건 · 소요 {(time.time() - started) / 60:.1f}분")
        if removed:
            print(dim(f"  중복 {removed:,}건 제거"))
        if truncated:
            print(yellow(f"  ! {len(truncated)}개 채널은 서버 보관 정책으로 과거 일부가 조회 불가"))
        if failed:
            print(red(f"  ! {len(failed)}개 채널 실패 — manifest.json 의 error 필드 확인"))

        if args.attachments:
            print(dim("\n첨부파일 내려받는 중…"), flush=True)
            got, skip, fail = download_attachments(client, out, rate=args.rate, max_mb=args.max_file_mb)
            print(f"  첨부 {got}개 저장 · {skip}개 건너뜀 · {fail}개 실패")

        if args.markdown:
            print(dim("\nMarkdown 렌더링 중…"), flush=True)
            written = render_markdown(out, out / "markdown")
            print(f"  {written}개 파일 → {out / 'markdown'}")

    return 0


def cmd_reconcile(args: argparse.Namespace) -> int:
    """아카이브 건수와 서버 카운트를 대조한다.

    기본은 서버에 붙지 않는다. manifest 가 채널마다 expected(plan 시점의 total_msg_count)와
    inaccessible_before(보관 정책 컷)를 이미 들고 있어서, 그것만으로 어디가 왜 어긋났는지 나온다.
    manifest 를 읽기만 하므로 라이브 수집기가 도는 중에도 안전하다.
    """
    from dataclasses import asdict
    from pathlib import Path

    from .archive import (
        REASONS,
        VERDICTS,
        Manifest,
        reconcile,
        reconcile_summary,
        refresh_expected,
    )

    out = Path(args.out).expanduser().resolve()
    manifest_path = out / "manifest.json"
    if not manifest_path.is_file():
        print(red(f"{manifest_path} 가 없습니다. 먼저 `mattertok archive` 를 실행하세요."))
        return 1
    manifest = Manifest.load(manifest_path)

    if args.refresh:
        # 서버에 붙는 유일한 경로다. 갱신은 메모리에서만 하고 manifest 를 저장하지 않는다 —
        # 그 파일의 주인은 돌고 있는 라이브 수집기다.
        print(dim("서버에서 채널별 total_msg_count 를 다시 받는 중…"), flush=True)
        with open_client() as client:
            updated, failed = refresh_expected(
                client, manifest, rate=args.rate, log=lambda m: print(m, flush=True)
            )
        note = f"  {updated:,}개 갱신" + (f" · {failed}개 실패" if failed else "")
        print(dim(note + " (manifest 는 건드리지 않습니다)"))

    rows = reconcile(
        manifest, out / "posts", tolerance=args.tolerance / 100, count_disk=not args.no_disk
    )
    summary = reconcile_summary(rows)

    if args.json:
        print(json.dumps(
            {"summary": summary,
             "channels": [{**asdict(r), "disk_drift": r.disk_drift} for r in rows]},
            ensure_ascii=False, indent=1,
        ))
        return 0 if not summary["verdicts"]["gap"] else 1

    verdicts = summary["verdicts"]
    print(bold("── 아카이브 대조 ──"), dim(str(out)))
    print(f"  채널 {summary['channels']:,}개 · 아카이브 {summary['archived']:,}건 "
          f"· 서버 기준 {summary['expected']:,}건 · 순 드리프트 {summary['drift']:+,}건")
    print("  판정  " + " · ".join(f"{v} {verdicts[v]:,}개" for v in VERDICTS if verdicts[v]))
    if summary["disk_mismatch"]:
        print(yellow(f"  ! manifest 와 디스크가 어긋난 채널 {summary['disk_mismatch']}개 "
                     f"(합계 {summary['disk_drift']:+,}건) — `archive` 를 한 번 돌리면 "
                     f"finalize 가 실제 파일 기준으로 다시 셉니다"))
    if summary["partial_tail"]:
        print(dim(f"  꼬리가 잘린 파일 {summary['partial_tail']}개 (수집 중이라면 정상)"))
    if summary["failed"]:
        print(red(f"  ! 마지막 동기화가 실패로 끝난 채널 {summary['failed']}개 "
                  f"— manifest.json 의 error 필드 확인"))

    flagged = [r for r in rows if r.verdict != "ok"]
    shown = flagged[: max(0, args.top)]
    if shown:
        print(bold(f"\n  드리프트 상위 {len(shown)}개") + dim(f"  (설명이 필요한 채널 {len(flagged)}개)"))
        print(dim("    " + pad("사유", 11) + pad("채널", 32)
                  + pad("아카이브", 10, right=True) + pad("서버기준", 11, right=True)
                  + pad("드리프트", 12, right=True)))
        tint = {"over": yellow, "gap": red, "retention": dim, "unknown": dim}
        for row in shown:
            # 디스크가 manifest 와 다른 채널만 표시한다. 표는 서버 대조가 주인공이라 조용히 둔다.
            mark = yellow(f"  디스크 {row.disk_drift:+,}") if row.disk_drift else ""
            print("    " + tint[row.verdict](pad(row.verdict, 11)) + pad(row.display, 32)
                  + pad(f"{row.archived:,}", 10, right=True)
                  + pad(f"{row.expected:,}", 11, right=True)
                  + pad(f"{row.drift:+,}", 12, right=True) + mark)

    present = [v for v in VERDICTS if verdicts[v] and v != "ok"]
    if present:
        print(bold("\n  왜 어긋나는가"))
        for verdict in present:
            print(f"    · {pad(verdict, 11)}{REASONS[verdict]}")
        print(dim("    · 제외 정책으로 애초에 받지 않은 채널은 manifest 에 없어 이 표에 나오지 않습니다."))
    return 0 if not verdicts["gap"] else 1


def cmd_render(args: argparse.Namespace) -> int:
    from pathlib import Path

    from .archive import render_markdown

    out = Path(args.out).expanduser().resolve()
    if not (out / "posts").is_dir():
        print(red(f"{out} 에 아카이브가 없습니다. 먼저 `mattertok archive` 를 실행하세요."))
        return 1
    dest = Path(args.to).expanduser().resolve() if args.to else out / "markdown"
    written = render_markdown(out, dest)
    print(green("✓"), f"{written}개 파일 → {dest}")
    return 0


# --------------------------------------------------------------------- 엔트리


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mattertok",
        description="Mattermost 메시지 조회 / 검색 / 실시간 수신 / 전송 (웹훅 불필요)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("doctor", help="연결·인증·서버 정책을 점검").set_defaults(func=cmd_doctor)

    p = sub.add_parser("login", help="로그인하고 세션을 저장")
    p.add_argument("-f", "--force", action="store_true", help="캐시 무시하고 새로 로그인")
    p.set_defaults(func=cmd_login)

    sub.add_parser("logout", help="저장된 세션 삭제").set_defaults(func=cmd_logout)
    sub.add_parser("whoami", help="현재 인증된 계정 표시").set_defaults(func=cmd_whoami)
    sub.add_parser("teams", help="소속 팀 목록").set_defaults(func=cmd_teams)

    p = sub.add_parser("channels", help="내 채널 목록")
    p.add_argument("-t", "--team", help="팀 이름으로 좁히기")
    p.add_argument("-f", "--filter", help="채널 이름 부분일치 필터")
    p.add_argument("-a", "--all", action="store_true", help="DM 채널도 포함")
    p.set_defaults(func=cmd_channels)

    sub.add_parser("unread", help="읽지 않은 메시지가 있는 채널").set_defaults(func=cmd_unread)

    p = sub.add_parser("read", help="채널 메시지 읽기")
    p.add_argument("channel", help="채널 id / 'team/channel' / '#이름' / '@사용자'")
    p.add_argument("-n", "--number", type=int, default=30, help="가져올 개수 (기본 30)")
    p.add_argument("-s", "--since", help="이 시각 이후만 (예: 2h, 30m, 3d, 2026-08-01)")
    p.add_argument("--mark-read", action="store_true", help="조회 후 읽음 처리")
    p.set_defaults(func=cmd_read)

    p = sub.add_parser("thread", help="스레드 전체 읽기")
    p.add_argument("post_id")
    p.set_defaults(func=cmd_thread)

    p = sub.add_parser("search", help="메시지 검색")
    p.add_argument("query")
    p.add_argument("-t", "--team", help="특정 팀에서만")
    p.add_argument("-n", "--number", type=int, default=20)
    p.add_argument("--any", action="store_true", help="OR 검색 (기본은 AND)")
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("send", help="메시지 전송")
    p.add_argument("channel", help="채널 id / 'team/channel' / '#이름' / '@사용자'(DM)")
    p.add_argument("message", nargs="*", help="보낼 내용 (생략하면 stdin 에서 읽음)")
    p.add_argument("--thread", help="이 post_id 의 스레드에 답글")
    p.add_argument("--dry-run", action="store_true", help="실제로 보내지 않고 미리보기")
    p.set_defaults(func=cmd_send)

    p = sub.add_parser("watch", help="실시간으로 새 메시지 수신")
    p.add_argument("channels", nargs="*", help="지정하지 않으면 내 모든 채널")
    p.add_argument("--include-own", action="store_true", help="내가 보낸 메시지도 표시")
    p.set_defaults(func=cmd_watch)

    p = sub.add_parser("archive", help="기존 대화 전체를 내려받아 저장 (중단/재개 가능)")
    p.add_argument("channels", nargs="*", help="지정하지 않으면 내 모든 채널")
    p.add_argument("-o", "--out", default="archive", help="저장 폴더 (기본 ./archive)")
    p.add_argument("--types", default="O,P,D,G", help="채널 타입 (O=공개 P=비공개 D=DM G=그룹DM)")
    p.add_argument("--rate", type=float, default=4.0, help="초당 요청 수 상한 (기본 4)")
    p.add_argument("--slim", action="store_true", help="metadata/props 제외해 용량 절약")
    p.add_argument("--include-empty", action="store_true", help="메시지 0건 채널도 시도")
    p.add_argument("--attachments", action="store_true", help="첨부파일도 내려받기")
    p.add_argument("--max-file-mb", type=float, default=25.0, help="첨부 최대 크기 MB (기본 25)")
    p.add_argument("--markdown", action="store_true", help="끝난 뒤 Markdown 전사본도 생성")
    p.add_argument("--dry-run", action="store_true", help="받지 않고 계획만 표시")
    p.add_argument("--include-excluded", action="store_true",
                   help="제외 정책을 무시하고 모두 받기 (기본 제외: MM_EXCLUDE_CHANNELS 패턴)")
    p.set_defaults(func=cmd_archive)

    p = sub.add_parser("live", help="실시간으로 새 메시지를 아카이브에 계속 저장 (중복 없음)")
    p.add_argument("-o", "--out", default="archive", help="저장 폴더 (기본 ./archive)")
    p.add_argument("--types", default="O,P,D,G", help="채널 타입 (O=공개 P=비공개 D=DM G=그룹DM)")
    p.add_argument("--rate", type=float, default=4.0, help="초당 요청 수 상한 (기본 4)")
    p.add_argument("--slim", action="store_true", help="metadata/props 제외해 용량 절약")
    p.add_argument("--include-empty", action="store_true", help="메시지 0건 채널도 시도")
    p.add_argument("--debounce", type=float, default=2.0, help="신호 후 저장까지 대기 초 (기본 2)")
    p.add_argument("--sweep", type=float, default=300.0, help="전체 재확인 주기 초, 0이면 끔 (기본 300)")
    p.add_argument("--include-excluded", action="store_true", help="제외 정책 무시")
    p.add_argument("--force-unlock", action="store_true", help="남아있는 잠금 파일 무시하고 시작")
    p.set_defaults(func=cmd_live)

    p = sub.add_parser("status", help="실시간 수집기가 살아있는지 확인 (서버 접속 없음)")
    p.add_argument("-o", "--out", default="archive", help="아카이브 폴더 (기본 ./archive)")
    p.add_argument("--json", action="store_true", help="status.json 원본 그대로 출력")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("reconcile", help="아카이브 건수와 서버 카운트 대조 (기본은 서버 접속 없음)")
    p.add_argument("-o", "--out", default="archive", help="아카이브 폴더 (기본 ./archive)")
    p.add_argument("--tolerance", type=float, default=2.0,
                   help="이 %% 안이면 일치로 본다 (기본 2, 최소 허용 5건)")
    p.add_argument("--top", type=int, default=15, help="표에 보여줄 채널 수 (기본 15)")
    p.add_argument("--no-disk", action="store_true", help="jsonl 실제 줄 수 세기 생략 (빠름)")
    p.add_argument("--json", action="store_true", help="채널별 결과를 JSON 으로")
    p.add_argument("--refresh", action="store_true",
                   help="서버에서 total_msg_count 를 다시 받아 비교 (유일하게 접속하는 경로, 기본 꺼짐)")
    p.add_argument("--rate", type=float, default=4.0, help="--refresh 의 초당 요청 수 상한 (기본 4)")
    p.set_defaults(func=cmd_reconcile)

    p = sub.add_parser("render", help="아카이브를 읽기 좋은 Markdown 으로 변환")
    p.add_argument("-o", "--out", default="archive", help="아카이브 폴더 (기본 ./archive)")
    p.add_argument("--to", help="출력 폴더 (기본 <아카이브>/markdown)")
    p.set_defaults(func=cmd_render)

    return parser


def main(argv: list[str] | None = None) -> int:
    # 한글 출력이 Windows 콘솔 기본 코드페이지에서 깨지지 않도록.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass

    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except auth.AuthError as exc:
        print(red("인증 오류:"), exc, file=sys.stderr)
        return 1
    except ApiError as exc:
        print(red("API 오류:"), exc, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())

