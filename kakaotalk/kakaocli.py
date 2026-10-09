# -*- coding: utf-8 -*-
"""kakaocli.py — macOS 카카오톡 DB 키 유도 + SQLCipher 복호화 (Python 포트).

Jaeha0526/kakaocli 의 kakao-mcp 브랜치(Sources/KakaoCore/Database/{KeyDerivation,DeviceInfo}.swift,
MIT License, Copyright (c) 2026 Silver Flight Group, LLC — 원 알고리즘은
https://gist.github.com/blluv/8418e3ef4f4aa86004657ea524f2de14)를 kakaotalk app으로 옮겼다
(macOS 지원, 2026-10). Windows 쪽(katok_decrypt/keychain/keystore_v2)과 대비된다:

  macOS(kakaocli)                                  Windows(25.7.2+, kakatok)
  ------------------------------------------       ------------------------------------------
  단일 통합 SQLCipher DB(NT* 테이블)               방마다 독립된 chatLogs_<id>.edb
  키 = PBKDF2-HMAC-SHA256(UUID+userId 조합)        방마다 다른 raw DEK(루트 언랩/메모리 스캔)
  오프라인 정적 유도 가능(레지스트리 불필요)        Themida VM — 정적 유도 불가

키 유도(Swift 구현의 문자열 연산을 그대로 옮김 — 하나라도 바뀌면 키가 달라진다):
  database_name : DB 파일명(78자 hex) — 컨테이너 디렉토리에서 이 이름의 파일을 찾는다
  secure_key    : PRAGMA KEY='<hex 256자>' 암구문(원문 그대로; hex 디코딩 아님 — kakaocli 방식)

SQLCipher 복호화: 표준 라이브러리 + pycryptodome만으로 페이지 단위 AES-CBC 복호화를 직접 구현한다
(sqlcipher 바이너리 불필요). compatibility 3(v3: page 1024/reserve 48/PBKDF2-SHA1 64000)과
4(v4: page 4096/reserve 80/PBKDF2-SHA512 256000)를 page-1 헤더 오라클로 자동 판별하고, WAL
프레임도 반영한다(헤더/프레임헤더는 평문, 체크섬은 저장 바이트 기반 — readers/edb.py 와 같은 규칙).
HMAC 검증은 생략한다(복호화에 불필요) — 무결성은 복원 이미지의 SQLite 헤더 오라클과 quick_check로 본다.

프라이버시: secure_key 는 키 자격증명 — 로그에 절대 찍지 않는다(파일명/개수만).
의존성: 표준 라이브러리(hashlib/base64/plistlib/subprocess) + pycryptodome(AES).
"""
from __future__ import annotations

import base64
import hashlib
import os
import re
import plistlib
import struct
import subprocess
import time

from Crypto.Cipher import AES

PAGE_MAGIC = b"SQLite format 3\x00"
UUID_RE = re.compile(r"[0-9A-F]{8}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{12}")
HEX_DB_RE = re.compile(r"^[0-9a-f]{78}(\.db)?$")
EMPTY_SHA512 = ("31bca02094eb78126a517b206a88c73cfa9ec6f704c7030d18212cace820f025f00bf0ea68dbf3f3a5436"
                "ca63b53bf7bf80ad8d5de7d8359d0b7fed9dbc3ab99")      # SHA-512("0"): 기본/빈 계정
BRUTE_DEFAULT = 5_000_000_000        # kakaocli 와 같은 상한(파이썬에선 수십 분 걸릴 수 있다)

# SQLCipher 기본 파라미터(cipher_default_compatibility 3/4). 판별 순서 = 최신 먼저.
CIPHERS = (
    {"name": "sqlcipher4", "page": 4096, "reserve": 80, "hash": "sha512", "iters": 256000},
    {"name": "sqlcipher3", "page": 1024, "reserve": 48, "hash": "sha1", "iters": 64000},
)


# ----------------------------------------------------------------- 키 유도 (KeyDerivation.swift)
def pbkdf2_sha256(password: bytes, salt: bytes, iterations: int = 100_000, length: int = 128) -> bytes:
    """kakaocli 의 PBKDF2-HMAC-SHA256(10만 회, 128바이트) — hashlib.pbkdf2_hmac 와 동일 알고리즘."""
    return hashlib.pbkdf2_hmac("sha256", password, salt, iterations, length)


def hashed_device_uuid(uuid: str) -> str:
    """SHA1(uuid) ‖ SHA256(uuid) 를 base64."""
    d = uuid.encode("utf-8")
    return base64.b64encode(hashlib.sha1(d).digest() + hashlib.sha256(d).digest()).decode("ascii")


def database_name(user_id: int, uuid: str) -> str:
    """암호화된 DB 파일명(확장자 없음, 78자 hex)."""
    hawawa = ".".join([".", "F", str(user_id), "A", "F", uuid[::-1], ".", "|"])
    salt = hashed_device_uuid(uuid)[::-1]
    return pbkdf2_sha256(hawawa.encode("utf-8"), salt.encode("utf-8")).hex()[28:106]


def secure_key(user_id: int, uuid: str) -> str:
    """SQLCipher 암호문(hex 256자) — PRAGMA KEY='<이 문자열 그대로>'."""
    hashed = hashed_device_uuid(uuid)
    parts = ["A", hashed, "|", "F", uuid[:5], "H", str(user_id), "|", uuid[7:]]
    hawawa = "F".join(parts)
    salt = uuid[int(len(uuid) * 0.3):]
    return pbkdf2_sha256(hawawa[::-1].encode("utf-8"), salt.encode("utf-8")).hex()


# ----------------------------------------------------------------- 장치/경로 (DeviceInfo.swift)
def platform_uuid() -> str | None:
    """IOPlatformUUID (ioreg). macOS 가 아니면 None."""
    try:
        out = subprocess.run(["/usr/sbin/ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
                             capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r'"IOPlatformUUID" = "([0-9A-F]{8}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{12})"', out)
    return m.group(1) if m else None


def container_path(home: str) -> str:
    """카카오톡 컨테이너 데이터 디렉토리(DB 가 있는 곳)."""
    return os.path.join(home, "Library", "Containers", "com.kakao.KakaoTalkMac", "Data", "Library",
                        "Application Support", "com.kakao.KakaoTalkMac")


def _preferences_paths(home: str) -> list[str]:
    """후보 plist: 컨테이너(hex 접미사 우선) → 전역."""
    out = []
    pref_dir = os.path.join(home, "Library", "Containers", "com.kakao.KakaoTalkMac", "Data", "Library",
                            "Preferences")
    try:
        names = sorted(os.listdir(pref_dir))
    except OSError:
        names = []
    for name in names:
        if name.startswith("com.kakao.KakaoTalkMac.") and name.endswith(".plist") \
                and name != "com.kakao.KakaoTalkMac.plist":
            out.append(os.path.join(pref_dir, name))
    out.append(os.path.join(pref_dir, "com.kakao.KakaoTalkMac.plist"))
    out.append(os.path.join(home, "Library", "Preferences", "com.kakao.KakaoTalkMac.plist"))
    return out


def _load_plist(path: str) -> dict | None:
    try:
        with open(path, "rb") as fh:
            d = plistlib.load(fh)
        return d if isinstance(d, dict) else None
    except (OSError, plistlib.InvalidFileException, ValueError):
        return None


def _longest_common_suffix(strings: list[str]) -> str | None:
    if not strings:
        return None
    rev = [s[::-1] for s in strings]
    n = 0
    for i, ch in enumerate(rev[0]):
        if all(i < len(r) and r[i] == ch for r in rev):
            n += 1
        else:
            break
    return strings[0][len(strings[0]) - n:] if n else None


def _suffix_user_id(plist: dict, prefix: str) -> int | None:
    keys = [k for k in plist if isinstance(k, str) and k.startswith(prefix)]
    if len(keys) >= 2:
        suffix = _longest_common_suffix([k[len(prefix):] for k in keys])
        if suffix:
            try:
                return int(suffix)
            except ValueError:
                pass
    return None


def _active_account_hash(plist: dict) -> str | None:
    """DESIGNATEDFRIENDSREVISION:<sha512hex> 중 값이 0이 아닌(활성 계정) 것."""
    prefix = "DESIGNATEDFRIENDSREVISION:"
    for key, val in plist.items():
        if not (isinstance(key, str) and key.startswith(prefix)):
            continue
        h = key[len(prefix):]
        if h == EMPTY_SHA512:
            continue
        if isinstance(val, bool):
            v = 0
        elif isinstance(val, (int, float)):
            v = int(val)
        else:
            v = 0
        if v != 0 and len(h) == 128:
            return h
    return None


def recover_user_id_from_sha512(hex_hash: str, limit: int = BRUTE_DEFAULT) -> int | None:
    """plist 키의 SHA-512(userId) 역상 브루트포스(0..limit). 파이썬은 단일 스레드 — 후보 우선 시도할 것."""
    if len(hex_hash) != 128:
        return None
    for i in range(limit):
        if hashlib.sha512(str(i).encode("ascii")).hexdigest() == hex_hash:
            return i
    return None


def candidate_user_ids(home: str) -> list[int]:
    """plist 의 AlertKakaoIDsList — 브루트포스 전에 먼저 검증해 볼 후보."""
    for path in _preferences_paths(home):
        plist = _load_plist(path)
        if plist is None:
            continue
        ids = plist.get("AlertKakaoIDsList")
        if isinstance(ids, (list, tuple)):
            out = []
            for item in ids:
                if isinstance(item, bool):
                    continue
                if isinstance(item, int) and item > 0:
                    out.append(item)
                elif isinstance(item, str) and item.isdigit() and int(item) > 0:
                    out.append(int(item))
            if out:
                return out
    return []


def user_id(home: str, brute_limit: int = BRUTE_DEFAULT) -> int | None:
    """plist 에서 userId 추출(kakaocli 의 4 전략 + AlertKakaoIDsList 후보 검증). 없으면 None.

    전략(컨테이너 plist 먼저, 각 파일마다 순서대로):
      1. FSChatWindowTransparency* 공통 접미사
      2. 직접 키(userId, user_id, KAKAO_USER_ID, userID)
      3. DESIGNATEDFRIENDSREVISION:<sha512> 역상 — AlertKakaoIDsList 후보 검증 후 브루트포스
      4. "NSWindow Frame FSChatWindowFrame_*" 공통 접미사
    """
    for path in _preferences_paths(home):
        plist = _load_plist(path)
        if plist is None:
            continue
        uid = _suffix_user_id(plist, "FSChatWindowTransparency")          # 1
        if uid is not None:
            return uid
        for key in ("userId", "user_id", "KAKAO_USER_ID", "userID"):     # 2
            v = plist.get(key)
            if isinstance(v, bool):
                continue
            if isinstance(v, int):
                return v
            if isinstance(v, str) and v.isdigit():
                return int(v)
        h = _active_account_hash(plist)                                   # 3
        if h is not None:
            for cand in candidate_user_ids(home):
                if hashlib.sha512(str(cand).encode("ascii")).hexdigest() == h:
                    return cand
            uid = recover_user_id_from_sha512(h, brute_limit)
            if uid is not None:
                return uid
        uid = _suffix_user_id(plist, "NSWindow Frame FSChatWindowFrame_")  # 4
        if uid is not None:
            return uid
    return None


def discover_database_file(container: str, user_id_: int | None = None, uuid: str | None = None) -> str | None:
    """컨테이너에서 암호화 DB 찾기: 78자 hex(또는 .db) 파일; 유도된 이름이 있으면 그 파일도."""
    derived = database_name(user_id_, uuid) if (user_id_ is not None and uuid) else None
    try:
        names = sorted(os.listdir(container))
    except OSError:
        return None
    for name in names:
        if derived is not None and name == derived:
            return os.path.join(container, name)
    for name in names:
        if HEX_DB_RE.match(name):
            return os.path.join(container, name)
    return None


# ----------------------------------------------------------------- SQLCipher 페이지 복호화
def _decrypt_page(key: bytes, page: bytes, first: bool, c: dict) -> bytes:
    psz, r = c["page"], c["reserve"]
    s = 16 if first else 0                      # page 1 은 첫 16바이트가 salt(평문)
    ct = page[s:psz - r]
    iv = page[psz - r:psz - r + 16]
    pt = AES.new(key, AES.MODE_CBC, iv).decrypt(ct)
    return (PAGE_MAGIC + pt) if first else pt


def _page_ok(page1: bytes, c: dict) -> bool:
    """복호화된 page 1(매직 포함 재조립본)이 SQLite 헤더와 일치하는지.
    오프셋은 SQLite 헤더 기준: 16-17 page_size(BE), 18/19 인코딩, 20 reserved, 21-23 64/32/32."""
    return (len(page1) >= 24 and page1[16:18] == c["page"].to_bytes(2, "big") and page1[18] in (1, 2)
            and page1[19] in (1, 2) and page1[20] == c["reserve"] and page1[21:24] == b"\x40\x20\x20")


def wal_cksum(data: bytes, s0: int, s1: int, big_endian: bool):
    fmt = ">II" if big_endian else "<II"
    for x0, x1 in struct.iter_unpack(fmt, data):
        s0 = (s0 + x0 + s1) & 0xFFFFFFFF
        s1 = (s1 + x1 + s0) & 0xFFFFFFFF
    return s0, s1


def _apply_wal(pages: list, wal: bytes, key: bytes, c: dict) -> tuple[int, dict]:
    """WAL 프레임을 pages(1-based) 위에 덮는다 — readers/edb.py 와 같은 규칙(체크섬은 저장 바이트 기준)."""
    facts = {"state": "none", "frames_total": 0, "frames_valid": 0, "frames_applied": 0}
    psz, frame = c["page"], 24 + c["page"]
    facts["frames_total"] = max(0, (len(wal) - 32) // frame)
    if len(wal) < 32:
        return len(pages) - 1, (facts | {"state": "bad_header"}) if wal else facts
    magic, _fmt, psize, _ckpt, salt1, salt2, c1, c2 = struct.unpack(">8I", wal[:32])
    if magic not in (0x377F0682, 0x377F0683) or psize != psz:
        return len(pages) - 1, facts | {"state": "bad_header"}
    big = (magic & 1) == 1
    if wal_cksum(wal[:24], 0, 0, big) != (c1, c2):
        return len(pages) - 1, facts | {"state": "bad_header"}
    facts["state"] = "ok"
    s0, s1 = c1, c2
    off, last_commit, valid = 32, -1, []
    while off + frame <= len(wal):
        fh = wal[off:off + 24]
        pg = wal[off + 24:off + frame]
        pgno, commit, fs1, fs2, fc1, fc2 = struct.unpack(">6I", fh)
        if (fs1, fs2) != (salt1, salt2) or pgno < 1:
            break
        s0, s1 = wal_cksum(fh[:8], s0, s1, big)
        s0, s1 = wal_cksum(pg, s0, s1, big)
        if (s0, s1) != (fc1, fc2):
            break                                   # torn frame — 그 앞까지만 유효
        valid.append((pgno, commit, pg))
        if commit:
            last_commit = len(valid) - 1
        off += frame
    applied = valid[:last_commit + 1]
    facts["frames_valid"] = len(valid)
    facts["frames_applied"] = len(applied)
    for pgno, _commit, pg in applied:
        while len(pages) <= pgno:
            pages.append(b"")
        pages[pgno] = _decrypt_page(key, pg, pgno == 1, c) + bytes(c["reserve"])
    return (applied[-1][1] if applied else len(pages) - 1), facts


def decrypt_db(main: bytes, wal: bytes, passphrase: str, ciphers=CIPHERS):
    """암호화된 SQLCipher DB(main [+ wal])를 평문 SQLite 이미지로.

    반환 (image | None, facts). facts: {cipher, pages, wal: {...}} — cipher 는 판별된
    파라미터 묶음 이름(sqlcipher4/3). page 1 헤더 오라클을 통과하는 cipher 가 없으면
    (None, {..., "cipher": None}) — 암구문 불일치/비-SQLCipher.
    """
    if len(main) == 0:
        return None, {"cipher": None, "pages": 0, "wal": {"state": "none"}}
    for c in ciphers:
        psz = c["page"]
        if len(main) < psz or len(main) % psz:
            continue
        key = hashlib.pbkdf2_hmac(c["hash"], passphrase.encode("utf-8"), main[:16], c["iters"], 32)
        if not _page_ok(_decrypt_page(key, main[:psz], True, c), c):
            continue
        pages = [b""]
        for i in range(len(main) // psz):
            pt = _decrypt_page(key, main[i * psz:(i + 1) * psz], i == 0, c)
            pages.append(pt + bytes(c["reserve"]))
        n, wal_facts = _apply_wal(pages, wal or b"", key, c)
        return b"".join(pages[1:n + 1]), {"cipher": c["name"], "pages": n, "wal": wal_facts}
    return None, {"cipher": None, "pages": len(main) // CIPHERS[0]["page"], "wal": {"state": "none"}}


def _snap(path: str):
    try:
        st = os.stat(path)
        return (st.st_size, int(st.st_mtime))
    except OSError:
        return None


def read_stable(db_path: str, retries: int = 3, delay: float = 0.5):
    """main+WAL 을 일관된 스냅샷으로 읽는다(읽기 전후 지문 비교 — 카톡이 쓰는 중이면 재시도)."""
    wal_path = db_path + "-wal"
    main = wal = None
    for attempt in range(retries):
        try:
            pre = (_snap(db_path), _snap(wal_path))
            with open(db_path, "rb") as fh:
                main = fh.read()
            wal = None
            if os.path.exists(wal_path):
                with open(wal_path, "rb") as fh:
                    wal = fh.read()
            post = (_snap(db_path), _snap(wal_path))
            if pre == post:
                return main, wal, True
        except OSError:
            return b"", None, False
        if attempt + 1 < retries:
            time.sleep(delay)
    return main, wal, False


# ----------------------------------------------------------------- self-test (결정적)
def _selftest() -> bool:
    """카톡·macOS 없이 도는 결정적 벡터: 유도 형태/안정성 + SQLCipher 페이지 왕복 + 판별."""
    uuid = "01234567-89ab-cdef-fedc-ba9876543210"
    # 유도: 형태와 안정성(값 자체는 test_macdb 에서 핀으로 검증)
    assert len(database_name(1, uuid)) == 78 and all(ch in "0123456789abcdef" for ch in database_name(1, uuid))
    assert database_name(1, uuid) == database_name(1, uuid)
    assert database_name(1, uuid) != database_name(2, uuid) and database_name(1, uuid) != database_name(1, uuid.upper())
    assert len(secure_key(1, uuid)) == 256
    # hashed_device_uuid = base64(sha1 ‖ sha256)
    d = uuid.encode()
    assert base64.b64decode(hashed_device_uuid(uuid)) == hashlib.sha1(d).digest() + hashlib.sha256(d).digest()
    # suffix / hash 헬퍼
    assert _longest_common_suffix(["aX12", "bX12", "cX12"]) == "X12"
    assert _suffix_user_id({"FSChatWindowTransparency12": 1, "FSChatWindowTransparencyX12": 1},
                           "FSChatWindowTransparency") == 12
    assert recover_user_id_from_sha512(hashlib.sha512(b"42").hexdigest(), 100) == 42
    assert recover_user_id_from_sha512(hashlib.sha512(b"999").hexdigest(), 100) is None
    # SQLCipher 페이지 왕복 + 판별: 평문 page 1 본문을 암호화해 _decrypt_page/_page_ok 로 되돌린다
    for c in CIPHERS:
        psz, r = c["page"], c["reserve"]
        key = hashlib.pbkdf2_hmac(c["hash"], b"pass", b"S" * 16, c["iters"], 32)
        body = bytearray(bytes(range(0x20, 0x20 + 8)) + bytes(psz - 16 - r - 8))   # page1 본문(예약영역 제외)
        body[0:2] = psz.to_bytes(2, "big")        # 헤더 page_size 필드(본문 기준 오프셋 0)
        body[2], body[3] = 1, 1                   # 인코딩
        body[4] = r                               # reserved
        body[5:8] = b"\x40\x20\x20"
        iv = bytes(range(16))
        page = b"S" * 16 + AES.new(key, AES.MODE_CBC, iv).encrypt(bytes(body)) + iv + bytes(r - 16)
        pt = _decrypt_page(key, page, True, c)    # 매직 재조립본: pt[16:] == body
        assert _page_ok(pt, c) and pt[16:24] == bytes(body[0:8])
        # 다른 cipher 파라미터로는 오라클을 통과하지 않아야 한다
        for other in CIPHERS:
            if other is not c:
                assert not _page_ok(pt, other)
    return True
