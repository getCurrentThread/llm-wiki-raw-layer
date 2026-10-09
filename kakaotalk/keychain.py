# -*- coding: utf-8 -*-
"""keychain.py — v2/v1 키저장소 언랩 체인 + 이중 루트 검증 + 회전 감지 지문.

kakatok(sample/kakatok/keychain.py)에서 kakaotalk app으로 옮겼다(독립화, 2026-10).
CLI(verify/fingerprint)는 setup 프로비저닝(`manage.py setup kakaotalk --provision`, kakaotalk/provision.py)이 대신한다.

루트 → DEK 언랩 (keystore_v2.store 기반, 전 방 동시 파생)
--------------------------------------------------------
카톡은 방마다 SQLCipher4 raw DEK(32B)를 쓰고, 그 DEK를 per-entry로 래핑해
appstate.dat(v2) / keystore.bin(v1)의 wrapped_dek_map 에 넣는다.

  KEK_i = HKDF-SHA256(root, salt=store.salt, info=store.info_prefix + relpath)
  DEK_i = AES-KW-unwrap(KEK_i, wrapped_dek[40B])      # RFC 3394, ICV 0xA6×8

info의 relpath는 map 키 문자열을 **원문 그대로**(역슬래시 포함, 정규화 금지) 쓴다 —
`chat_data\\chatLogs_<id>.edb`를 `/`로 바꾸거나 소문자화하면 ICV가 깨진다(실측).
루트만 있으면 메모리 스캔 없이 **전 방의 DEK를 한 번에** 파생할 수 있다. 이게
"커버리지를 올리려면 방을 열어야 한다"는 기존 제약을 없앤다.

이중 루트 (W → profile.dat, 회전·스트레일 판정)
----------------------------------------------
온디스크 래핑층도 재구성했다(research: P02 완결).

  W         = HMAC-SHA256(key=엔트로피32B, msg=사용자문자열88B ‖ b"ikm-wrap")
  profile.dat(40B) = AES-KW-wrap(W, root_file 32B)

핵심 함정: profile.dat 의 평문 root_file 은 **실가동 루트가 아닌 스트레일 값**일 수
있다(2026-10-02 실측: 56d8… 스트레일 vs 실가동 8e7e…). 실가동 루트는 서버발급 세션
자료(엔트로피)의 Themida VM 변환으로만 생긴다. 그래서 root_file ≠ live root 라고
해서 곧바로 "키 무효"가 아니다 — **appstate 언랩 성공률**이 권위 있는 판정이다
(unwrap_store가 623/623이면 루트 유효, 0이면 회전됨).

회전 감지 지문
--------------
키 원천 파일(appstate.dat, keystore.bin, profile.dat, credential.bin,
last_pc_login.dat)의 (sha256, size, mtime)을 찍어 둔다. 2026-10-03 실험: 강제종료·
재시작·자동로그인으로는 **회전 없음**(3중 증거) — 회전 트리거 후보는 로그아웃/재로그인/
기기추가로 좁혀져 있으므로, 운영 중 지문이 바뀌면 그 순간이 최초 관측 사례가 된다.
fingerprint() → diff_fingerprint() 로 이전 상태와 비교해 어떤 파일이 언제 바뀌었는지
돌려준다.

의존성: 표준 라이브러리 + pycryptodome(AES). keystore_v2(CBOR 파서) 재사용.
프라이버시: DEK·루트는 키 자격증명 — 로그에는 절대 찍지 않고(개수/해시카운트만),
보호된 data root 안에서만 다룬다.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import struct

from . import keystore_v2 as kv

from Crypto.Cipher import AES

KW_IV = b"\xa6" * 8                      # RFC 3394 기본 IV(ICV)
DEK_BYTES = 32

# 회전 감지 대상 키 원천 파일(사용자 디렉토리 루트 상대). 없는 것은 스킵.
ROTATION_FILES = (
    "appstate.dat",          # v2 키저장소(라이브)
    "keystore.bin",          # v1 키저장소(구버전)
    "profile.dat",           # W로 래핑된 root_file(스트레일 후보)
    "credential.bin",        # profile.dat 와 동일 바이트(실측)
    "last_pc_login.dat",     # 자동로그인 토큰
)


# ----------------------------------------------------------------- HKDF (RFC 5869)
def hkdf_sha256(ikm: bytes, salt: bytes, info: bytes, length: int = 32) -> bytes:
    """HKDF-SHA256 extract+expand. 카톡 v2 언랩의 KEK 유도(salt=store.salt, info=prefix+relpath)."""
    prk = hmac.new(salt, ikm, hashlib.sha256).digest()
    okm = b""
    t = b""
    counter = 1
    while len(okm) < length:
        t = hmac.new(prk, t + info + bytes([counter]), hashlib.sha256).digest()
        okm += t
        counter += 1
    return okm[:length]


# ----------------------------------------------------------------- AES-KW (RFC 3394)
def aes_kw_wrap(kek: bytes, key: bytes) -> bytes:
    """32B 키를 40B wrapped로. selftest 왕복·profile.dat 재래핑 검증에 쓴다."""
    n = len(key) // 8
    a = KW_IV
    r = [key[8 * i:8 * (i + 1)] for i in range(n)]
    for j in range(6):
        for i in range(1, n + 1):
            b = AES.new(kek, AES.MODE_ECB).encrypt(a + r[i - 1])
            t = n * j + i
            a = bytes(x ^ y for x, y in zip(b[:8], struct.pack(">Q", t)))
            r[i - 1] = b[8:]
    return a + b"".join(r)


def aes_kw_unwrap(kek: bytes, wrapped: bytes) -> bytes:
    """40B wrapped → 32B. ICV(A6×8) 불일치면 ValueError — 곧 '잘못된 KEK/relpath'."""
    if len(wrapped) % 8 or len(wrapped) < 16:
        raise ValueError("AES-KW: wrapped 길이 오류")
    n = len(wrapped) // 8 - 1
    cipher = AES.new(kek, AES.MODE_ECB)
    a = wrapped[:8]
    r = [wrapped[8 * (i + 1):8 * (i + 2)] for i in range(n)]
    for j in range(5, -1, -1):
        for i in range(n, 0, -1):
            t = n * j + i
            at = bytes(x ^ y for x, y in zip(a, struct.pack(">Q", t)))
            b = cipher.decrypt(at + r[i - 1])
            a = b[:8]
            r[i - 1] = b[8:]
    if a != KW_IV:
        raise ValueError("AES-KW: ICV 불일치 (루트 불일치 또는 relpath 변형)")
    return b"".join(r)


# ----------------------------------------------------------------- 루트 → DEK
def derive_kek(root: bytes, store: dict, relpath: str) -> bytes:
    """relpath 하나의 KEK. info = info_prefix + relpath **원문 그대로**(정규화 금지)."""
    info = store["version"].encode("latin1") + relpath.encode("latin1")
    return hkdf_sha256(root, store["salt"], info, 32)


def derive_dek(root: bytes, store: dict, relpath: str) -> bytes:
    """relpath 하나의 DEK(32B). ICV 실패 = ValueError."""
    return aes_kw_unwrap(derive_kek(root, store, relpath), store["map"][relpath])


def unwrap_store(root: bytes, store: dict) -> dict[str, bytes]:
    """전 relpath 언랩. 성공만 담는다({relpath: DEK}). 실패 개수는 len(map)과 비교."""
    out: dict[str, bytes] = {}
    for relpath in store["map"]:
        try:
            out[relpath] = derive_dek(root, store, relpath)
        except ValueError:
            continue
    return out


def root_status(root: bytes, store: dict) -> dict:
    """루트 유효성 요약 — 개수만(키·relpath 원문은 로그 금지)."""
    total = len(store["map"])
    ok = len(unwrap_store(root, store))
    return {"version": store["version"], "entries": total, "unwrapped": ok,
            "alive": ok == total and total > 0,
            "partial": 0 < ok < total}


def verify_dek(dek: bytes, edb_path: str, page: int = 4096, reserve: int = 80) -> bool:
    """DEK가 이 .edb 를 여는지 — page1 SQLCipher 헤더 오라클.
    복호화한 page1 본문 첫 바이트들이 SQLite 헤더(page_size 4096 BE, reserve 80,
    인코딩 64/32/32)와 일치하는지 본다. 우연 일치 확률은 무시 가능(~2^-40)."""
    try:
        with open(edb_path, "rb") as f:
            head = f.read(page)
    except OSError:
        return False
    if len(head) < page - reserve + 16:
        return False
    ct = head[16:page - reserve]
    iv = head[page - reserve:page - reserve + 16]
    pt = AES.new(dek, AES.MODE_CBC, iv).decrypt(ct)
    return (pt[0:2] == b"\x10\x00"          # page_size = 4096 (BE)
            and pt[2] in (1, 2) and pt[3] in (1, 2)
            and pt[4] == reserve             # reserved = 80
            and pt[5:8] == b"\x40\x20\x20")  # max/min payload·leaf 64/32/32


def read_root_hex(path: str) -> bytes | None:
    """root_v2.hex(64자 hex, 개행 무시) 읽기. 없으면 None."""
    try:
        with open(path, "r", encoding="ascii") as f:
            hx = f.read().strip()
        root = bytes.fromhex(hx)
        return root if len(root) == DEK_BYTES else None
    except (OSError, ValueError):
        return None


# ----------------------------------------------------------------- 회전 감지 지문
def fingerprint(users=None) -> dict:
    """키 원천 파일들의 (sha256, size, mtime) 지문. rotation_state.json 저장용."""
    udir = kv.user_dir(users)
    fp: dict[str, dict] = {}
    if not udir:
        return fp
    for name in ROTATION_FILES:
        p = os.path.join(udir, name)
        if not os.path.exists(p):
            continue
        try:
            st = os.stat(p)
            with open(p, "rb") as f:
                fp[name] = {"sha256": hashlib.sha256(f.read()).hexdigest(),
                            "size": st.st_size, "mtime": int(st.st_mtime)}
        except OSError:
            continue
    return fp


def diff_fingerprint(prev: dict, cur: dict) -> dict:
    """두 지문 비교 → {added:[], removed:[], changed:[{name, what...}], same:n}."""
    added = sorted(set(cur) - set(prev))
    removed = sorted(set(prev) - set(cur))
    changed = []
    same = 0
    for name in sorted(set(prev) & set(cur)):
        if prev[name] == cur[name]:
            same += 1
            continue
        what = [k for k in ("sha256", "size", "mtime") if prev[name].get(k) != cur[name].get(k)]
        changed.append({"name": name, "fields": what})
    return {"added": added, "removed": removed, "changed": changed, "same": same}


def load_rotation_state(path: str) -> dict:
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            pass
    return {}


def save_rotation_state(path: str, state: dict):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=True, indent=1)
    os.replace(tmp, path)


# ----------------------------------------------------------------- self-test (결정적)
def _selftest() -> bool:
    """카톡·키 없이 도는 결정적 벡터: RFC 5869 TC1, RFC 3394 벡터, 언랩 왕복."""
    # RFC 5869 Test Case 1 (SHA-256)
    okm = hkdf_sha256(b"\x0b" * 22, bytes.fromhex("000102030405060708090a0b0c"),
                      bytes.fromhex("f0f1f2f3f4f5f6f7f8f9"), 42)
    assert okm == bytes.fromhex(
        "3cb25f25faacd57a90434f64d0362f2a"
        "2d2d0a90cf1a5a4c5db02d56ecc4c5bf"
        "34007208d5b887185865"), "HKDF RFC5869 TC1"

    # RFC 3394 4.3 — 256-bit KEK, 128-bit key data
    kek = bytes.fromhex("000102030405060708090A0B0C0D0E0F101112131415161718191A1B1C1D1E1F")
    kd16 = bytes.fromhex("00112233445566778899AABBCCDDEEFF")
    assert aes_kw_wrap(kek, kd16) == bytes.fromhex(
        "64E8C3F9CE0F5BA2"
        "63E9777905818A2A"
        "93C8191E7D6E8AE7"), "KW wrap RFC3394 4.3"
    assert aes_kw_unwrap(kek, aes_kw_wrap(kek, kd16)) == kd16

    # RFC 3394 4.6 — 256-bit KEK, 256-bit key data (카톡 DEK 와 같은 32B→40B)
    kd32 = bytes.fromhex("00112233445566778899AABBCCDDEEFF000102030405060708090A0B0C0D0E0F")
    assert aes_kw_wrap(kek, kd32) == bytes.fromhex(
        "28C9F404C4B810F4"
        "CBCCB35CFB87F826"
        "3F5786E2D80ED326"
        "CBC7F0E71A99F43B"
        "FB988B9B7A02DD21"), "KW wrap RFC3394 4.6"
    assert aes_kw_unwrap(kek, aes_kw_wrap(kek, kd32)) == kd32

    # 카톡 체인 모형: 합성 store 에 대해 root→KEK→wrap→unwrap 왕복
    store = {"version": "v2:", "salt": bytes(range(32)),
             "map": {r"chat_data\chatLogs_9.edb": aes_kw_wrap(
                 hkdf_sha256(b"\x11" * 32, bytes(range(32)), b"v2:chat_data\\chatLogs_9.edb", 32),
                 b"\xab" * 32)}}
    assert derive_dek(b"\x11" * 32, store, r"chat_data\chatLogs_9.edb") == b"\xab" * 32
    # relpath 를 정규화(/ 치환)한 KEK 로는 원본 wrapped 가 안 풀려야 한다 — 원문 그대로가 계약
    wrong_kek = hkdf_sha256(b"\x11" * 32, bytes(range(32)),
                            b"v2:chat_data/chatLogs_9.edb", 32)
    try:
        aes_kw_unwrap(wrong_kek, store["map"][r"chat_data\chatLogs_9.edb"])
        return False
    except ValueError:
        pass
    # 잘못된 루트 → 전멸
    assert unwrap_store(b"\x22" * 32, store) == {}
    return True
