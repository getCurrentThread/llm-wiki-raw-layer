# mattertok

웹훅 없이 Mattermost 메시지를 **읽고 · 검색하고 · 실시간으로 받고 · 보내는** Python 클라이언트.

공식 **REST API v4 + WebSocket API** 만 사용합니다. Incoming/Outgoing Webhook은 단방향 전송 전용이라
"메시지를 읽는" 용도로는 애초에 쓸 수 없고, 여기서 하려는 일은 전부 REST/WS의 정석 기능입니다.

대상 서버: 임의의 Mattermost 서버(`MM_URL` 필수, 기본 서버 없음).

---

## 설치

```powershell
cd <PROJECT>\mattertok
uv venv --python 3.13
uv pip install -e .
```

이후 명령은 `.venv\Scripts\mattertok.exe ...` 또는 venv 활성화 후 `mattertok ...` 로 실행합니다.

```powershell
.\.venv\Scripts\Activate.ps1
mattertok doctor
```

## 설정

`.env.example` 를 `.env` 로 복사하고 채웁니다. `.env` 는 `.gitignore` 에 걸려 있습니다.

```ini
MM_URL=https://mattermost.example.com
MM_LOGIN_ID=본인_이메일_또는_사용자명
MM_PASSWORD=비밀번호
```

`.env` 에 비밀번호를 두기 싫으면 비워두고 `mattertok login` 을 실행하면 됩니다.
프롬프트로 입력받고(화면에 안 보임), **세션 토큰만** 프로젝트 밖
`%LOCALAPPDATA%\mattertok\` 에 저장합니다. 비밀번호는 저장하지 않습니다.

Personal Access Token(PAT)을 발급받을 수 있다면 `MM_TOKEN` 에 넣으세요. 그쪽이 우선 사용되고 만료가 없습니다.
(웹 > 프로필 설정 > 보안 에 해당 메뉴가 보이면 발급 가능합니다. 기본적으로 관리자가 계정별로 열어줘야 합니다.)

---

## 사용법

```powershell
mattertok doctor                       # 연결·인증·서버 정책 점검 (여기부터 시작)
mattertok login                        # 로그인 후 세션 저장
mattertok whoami                       # 현재 계정

mattertok teams                        # 소속 팀
mattertok channels                     # 내 채널 목록
mattertok channels -f 공지             # 이름으로 필터
mattertok unread                       # 안 읽은 메시지가 있는 채널

mattertok read '#공지'                 # 최근 30건
mattertok read '#공지' -n 100          # 100건
mattertok read '#공지' -s 3d           # 최근 3일치 (2h, 30m, 2026-08-01 도 가능)
mattertok read '@someone'              # DM 읽기
mattertok thread <post_id>             # 스레드 전체

mattertok search "배포 일정"           # 전체 팀에서 검색
mattertok search "배포" -t 공통 -n 50  # 특정 팀에서 50건

mattertok send '#공지' "안녕하세요"     # 채널로 전송
mattertok send '@someone' "DM 입니다"   # DM 전송
mattertok send '#공지' --dry-run "미리보기만"
mattertok send '#공지' --thread <post_id> "스레드 답글"
Get-Content report.md | mattertok send '#공지'   # stdin 파이프

mattertok watch                        # 모든 채널 실시간 수신
mattertok watch '#공지' '#일반'         # 특정 채널만
```

### 기존 대화 전체 아카이브

```powershell
mattertok archive --dry-run            # 계획만 표시 (채널 수·메시지 수·예상 시간)
mattertok archive                      # 전체 내려받기 → ./archive
mattertok archive --markdown           # 끝난 뒤 읽기 좋은 Markdown 도 생성
mattertok archive --types O,P          # DM 제외하고 공개/비공개 채널만
mattertok archive '#공지'               # 특정 채널만
mattertok archive --attachments        # 첨부파일까지
mattertok archive --slim               # metadata/props 제외해 용량 절약
mattertok archive --rate 2             # 초당 요청 수 낮추기 (기본 4)

mattertok render                       # 이미 받아둔 아카이브 → Markdown 재생성
```

**중단해도 됩니다.** Ctrl+C 로 끊고 같은 명령을 다시 실행하면 이어받습니다.
이미 다 받은 채널은 새로 올라온 글만 증분으로 붙입니다.

산출물:

```
archive/
  manifest.json      채널별 커서·진행상태 (재개의 근거)
  channels.json      채널 메타
  users.json         user_id → 사용자명 매핑
  posts/
    public/<팀>/<채널>__<id8>.jsonl     한 줄 = 메시지 1건, 과거→최신 순
    private/<팀>/<채널>__<id8>.jsonl
    dm/@상대방__<id8>.jsonl
    group/<참여자들>__<id8>.jsonl
  files/             --attachments 사용 시
  markdown/          --markdown 사용 시, 날짜별로 묶인 전사본
```

#### 어떻게 전부 받는가

채널마다 커서를 두 방향으로 굴립니다.

| 방향 | 방법 | 종료 조건 |
|---|---|---|
| 백필(과거로) | `?before=<지금까지 받은 가장 오래된 post_id>` 반복 | 응답의 `prev_post_id` 가 빈 문자열 |
| 증분(미래로) | `?after=<가장 최신 post_id>` | 응답의 `next_post_id` 가 빈 문자열 |

`?page=N` 방식은 이력이 깊어지면 경계가 어긋날 수 있어 쓰지 않습니다.
받는 도중에는 도착 순서대로 append 하고, 끝난 뒤 post id 로 중복을 제거해
`create_at` 오름차순으로 다시 씁니다. 그래서 몇 번을 중단·재개해도 결과가 같습니다.

### 실시간 수집 (`live`)

한 번 받아두고 끝내는 대신 계속 켜두면, 새 메시지가 올라오는 대로 같은 아카이브에 쌓입니다.

```bash
mattertok live                      # 밀린 것 따라잡고 → 실시간 수집 시작
mattertok live --sweep 60           # 전체 재확인을 1분마다 (기본 300초)
mattertok live --debounce 0.5       # 신호 후 0.5초 내 저장 (기본 2초)
```

#### 중복이 생기지 않는 이유

WebSocket 프레임을 파일에 직접 쓰지 않습니다. 프레임은 "이 채널에 뭔가 생겼다"는 **신호**로만 쓰고,
실제 저장은 언제나 `?after=<지금까지 받은 마지막 post id>` 조회가 합니다. 서버는 그 id **이후** 것만
돌려주므로, 같은 신호가 몇 번 오든 이미 저장한 글은 다시 오지 않습니다.

중복 방지가 "본 적 있는 id 집합"(크기 제한이 있고 재시작하면 사라짐)이 아니라 서버가 강제하는
커서에 걸려 있다는 게 핵심입니다. 그래서 이렇게 됩니다.

- 신호를 **놓쳐도** 됩니다 → 주기적 스윕이 줍습니다.
- 신호가 **중복돼도** 됩니다 → 커서가 무시합니다.
- 프로세스가 **죽어도** 됩니다 → 커서는 manifest 에 원자적으로 저장돼 있어 이어서 받습니다.

어느 쪽으로 틀려도 안전한 방향으로만 틀립니다.

#### 세 겹으로 받습니다

| 층 | 언제 | 무엇을 막나 |
|---|---|---|
| 시작 시 전체 동기화 | `live` 실행 직후 | 꺼져 있던 동안 쌓인 것 |
| WebSocket 신호 → 해당 채널 증분 | 평상시 (몇 초 지연) | — |
| 주기적 전체 스윕 | 기본 5분마다 | WS 가 조용히 멎음, 신호가 안 오는 시스템 메시지, 새로 생긴 채널 |

재연결 시에는 전 채널을 다시 훑습니다. 끊긴 동안 놓친 것을 시간(`?since=`) 대신 커서로 메우므로
겹치지 않습니다.

#### 상시 서비스로 돌리기 (nssm)

Windows 서비스로 등록해두면 로그인 없이도 부팅 때부터 계속 수집합니다.

```powershell
nssm install mattertok <PYTHON> -m mattertok.cli live -o <PROJECT>\archive
nssm set mattertok AppDirectory <PROJECT>
nssm set mattertok AppEnvironmentExtra PYTHONIOENCODING=utf-8 PYTHONUNBUFFERED=1 LOCALAPPDATA=<LOCALAPPDATA>
nssm set mattertok AppStdout <LOGS>\live.log
nssm set mattertok AppStderr <LOGS>\live.err.log
nssm set mattertok AppRotateFiles 1
nssm set mattertok AppRotateBytes 10485760
nssm set mattertok AppExit Default Restart
nssm set mattertok AppRestartDelay 10000
nssm set mattertok AppStopMethodConsole 20000
nssm set mattertok Start SERVICE_DELAYED_AUTO_START
nssm start mattertok
```

주의할 점 세 가지. 셋 다 실제로 걸려서 서비스가 죽었던 것들입니다.

- **`LOCALAPPDATA` 를 넘겨야 합니다.** 서비스는 LocalSystem 으로 돌아 기본값이 systemprofile 을
  가리킵니다. 그러면 사용자의 세션 캐시를 못 찾고, 서비스는 대화형 로그인을 할 수 없으니 그대로 끝입니다.
- **세션 파일 ACL.** `icacls` 로 사용자 단독 잠금이면 서비스가 `PermissionError` 로 즉사합니다.
  지금은 `_restrict_permissions()` 가 현재 사용자 + SYSTEM + Administrators 를 함께 부여합니다
  (어느 쪽이 저장하든 반대쪽이 안 잠깁니다).
- **PAT 가 없으면 세션 만료 시 멈춥니다.** 오래 돌릴 거라면 `.env` 에 `MM_TOKEN` 을 넣어두세요.
  멈추면 `logs/live.err.log` 에 남습니다.

상태는 `nssm status mattertok`, 로그는 `logs/live.log`. 중지하면 대기 중인 신호만 비우고 바로
나갑니다(0.1초) — 종료 시 전체 스윕이나 finalize 를 하지 않기 때문입니다. 전체 시간순 정렬이
필요하면 중지 후 `mattertok archive` 를 한 번 돌리세요.

#### 같은 폴더에 두 개는 못 돕니다

두 수집기가 같은 채널 파일에 동시에 붙으면 커서가 서로를 덮어써서 중복이 납니다. 커서로 막을 수
없는 유일한 경우라 `.live.lock` 으로 막습니다. 프로세스가 30초마다 갱신하고, 150초 넘게 갱신이
없으면 죽은 것으로 보고 인수합니다. 정말 아무것도 안 도는데 막히면 `--force-unlock`.

Ctrl+C 로 끝내면 마지막으로 한 번 더 훑고 `finalize`(id 중복 제거 + 시간순 정렬)까지 하고 나갑니다.

### 수집에서 제외되는 채널

기본 제외 패턴은 없습니다(`MM_EXCLUDE_CHANNELS` 미설정 시 아무것도 빼지 않음). 패턴이 설정되면 이름에
그 문자열이 들어간 채널은 아카이브와 실시간 수신 양쪽에서 빠집니다. 패턴 설정은 소유자 몫입니다.
삭제한 채널을 되살리지 않으려면 패턴을 먼저 설정하세요.

```bash
mattertok channels              # 목록에는 보이되 [수집 제외] 로 표시됩니다
mattertok archive               # 제외된 채널 수를 실행 시 알려줍니다
mattertok archive --include-excluded   # 정책을 끄고 모두 받기
```

패턴은 `.env` 의 `MM_EXCLUDE_CHANNELS` 로 바꿉니다(쉼표 구분). 빈 값이면 제외하지 않습니다.

```
MM_EXCLUDE_CHANNELS=임시,테스트
```

제외는 `Archiver.plan()` 한 곳에서만 걸립니다. 아카이브되는 모든 채널이 지나는 관문이라
백필·증분·재개 어느 경로로도 새지 않습니다. 실시간 쪽은 `MattermostStream._parse_posted()` 와
재연결 갭 메우기(`_fill_gap`) 양쪽에 걸려 있습니다.

### 채널 지정 방법

| 형식 | 예시 |
|---|---|
| 채널 이름 (부분일치) | `공지`, `#공지` |
| 팀 지정 | `myteam/announcements` |
| DM | `@username` |
| 채널 ID (26자) | `1a2b3c...` |

---

## 라이브러리로 쓰기

```python
from mattertok import connect

with connect() as mm:
    ch = mm.resolve_channel("#공지")

    for post in mm.posts(ch["id"], limit=20):
        print(mm.username(post["user_id"]), post["message"])

    mm.create_post(ch["id"], "스크립트에서 보냅니다")
    mm.send_dm("someone", "DM 도 됩니다")
```

실시간 수신 + 자동 반응:

```python
import asyncio
from mattertok import connect, watch

with connect() as mm:
    target = mm.resolve_channel("#일반")["id"]

    def on_message(msg):
        print(f"{msg.sender_name}: {msg.message}")
        if "핑" in msg.message:
            mm.create_post(msg.channel_id, "퐁", root_id=msg.post_id)

    asyncio.run(watch(mm, on_message, channel_ids=[target]))
```

---

## 구조

| 파일 | 역할 |
|---|---|
| `config.py` | `.env`/환경변수 로딩, 세션 저장 위치 |
| `auth.py` | PAT → 캐시된 세션 → ID/비번 로그인 순으로 토큰 확보 |
| `client.py` | REST API v4 — 팀/채널/메시지/검색/전송, 429·5xx 백오프 재시도 |
| `stream.py` | WebSocket 실시간 수신 — 재연결 + 놓친 메시지 복구 |
| `archive.py` | 전체 대화 아카이브 — 재개 가능한 백필 + 증분 동기화, Markdown 렌더링 |
| `cli.py` | 명령줄 인터페이스 |
| `selfcheck.py` | 서버 없이 도는 자체 점검 (`python selfcheck.py`) |

### 처리해둔 함정

- **이중 JSON 인코딩** — `posted` 이벤트의 `data.post` 는 dict 가 아니라 JSON *문자열*입니다
  ([mattermost#22110](https://github.com/mattermost/mattermost/issues/22110)). `stream.py` 에서 풀어줍니다.
- **재연결 중 메시지 유실** — WebSocket 이 끊긴 사이의 메시지는 그냥 사라집니다
  ([mattermost#23332](https://github.com/mattermost/mattermost/issues/23332)).
  재연결 시 REST `?since=` 로 갭을 메우고, post id 로 중복을 걸러 `[놓친 메시지]` 표시와 함께 넘겨줍니다.
- **Rate limit** — 429 는 `Retry-After` 를 존중해 지수 백오프로 재시도합니다.
  아카이브는 기본 초당 4회로 스스로 속도를 제한합니다.
- **N+1 요청** — 사용자 이름은 `POST /users/ids` 로 일괄 조회 후 캐시합니다.
  DM 상대 294명도 개별 조회하지 않고 한 번에 받습니다.
- **DM 채널 중복** — DM/그룹DM 은 모든 팀의 채널 목록에 똑같이 나타납니다.
  (팀 N개 x 팀당 수백 개처럼 보이지만 실제 고유 채널은 훨씬 적음)
  channel id 로 dedupe 하지 않으면 같은 대화를 팀 수만큼 여러 번 받게 됩니다.
- **DM 파일명 길이** — DM 채널의 raw `name` 은 `userid1__userid2` (53자) 라 Windows 260자
  경로 제한에 걸리기 쉽습니다. 표시 이름(`@사용자명`)으로 저장합니다.
- **보관 정책** — 응답의 `first_inaccessible_post_time` 이 0이 아니면 서버 라이선스의
  메시지 보관 한도 때문에 그 이전은 **API 로도 못 가져옵니다.** manifest 와 Markdown 머리말에 표시됩니다.
- **Windows 콘솔 인코딩** — stdout 을 UTF-8 로 재설정해 한글이 깨지지 않게 합니다.

---

## 주의

- **조직 서버일 수 있습니다.** 개인 계정 자동화는 보통 문제없지만 대량 전송·과도한 폴링은 피하시고,
  사내 정책은 한 번 확인하시길 권합니다.
- 서버 설정에 따라 **봇 계정 생성이 꺼져 있을 수 있습니다**(`EnableBotAccountCreation=false`). 따라서 이 도구는 본인 계정으로
  동작하고, 보낸 메시지는 **본인 이름으로** 나갑니다.
- `.env` 와 세션 파일에는 자격증명이 들어갑니다. 커밋하지 마세요 (`.gitignore` 에 이미 포함).
- 로그인 실패를 반복하면 서버 정책에 따라 **계정이 잠길 수 있습니다.**

