# -*- coding: utf-8 -*-
"""The KakaoTalk v2/v1 keystore (CBOR) parser and the inventory of every room the client knows (the denominator of
key coverage).

  users/<hash>/appstate.dat (v2, live) and keystore.bin (v1, older): CBOR {info_prefix, salt, wrapped_dek_map}
  wrapped_dek_map = [[relpath, wrapped_dek (40 bytes)], ...], one per room

The client writes byte strings as CBOR arrays of uint8, so the small decoder below turns both into bytes. The
owner's own keystore on this machine: wrapped keys are logged as hashes only."""
from __future__ import annotations

import os
import re

CHATLOG_RE = re.compile(r"chatLogs_(\d+)\.edb", re.IGNORECASE)


# ----------------------------------------------------------------- 미니 CBOR 디코더
class _CborError(ValueError):
    pass


def _cbor_decode(buf: bytes):
    """CBOR 바이트열 하나를 파이썬 객체로. 주요 타입 0~5,7 + 무한길이 지원.
    반환 (value, next_offset). 카톡 keystore 에 필요한 부분집합을 견고하게 처리한다."""
    val, off = _dec(buf, 0)
    return val


def _read_len(buf, off, ai):
    """추가정보(ai)로 길이/값을 읽어 (n, new_off). ai<24 즉값, 24/25/26/27 = 1/2/4/8B."""
    if ai < 24:
        return ai, off
    if ai == 24:
        return buf[off], off + 1
    if ai == 25:
        return int.from_bytes(buf[off:off + 2], "big"), off + 2
    if ai == 26:
        return int.from_bytes(buf[off:off + 4], "big"), off + 4
    if ai == 27:
        return int.from_bytes(buf[off:off + 8], "big"), off + 8
    if ai == 31:
        return None, off  # 무한길이 표식
    raise _CborError(f"bad additional info {ai}")


def _dec(buf, off):
    if off >= len(buf):
        raise _CborError("unexpected end of CBOR")
    ib = buf[off]; off += 1
    major = ib >> 5
    ai = ib & 0x1F

    if major == 0:  # unsigned int
        n, off = _read_len(buf, off, ai); return n, off
    if major == 1:  # negative int
        n, off = _read_len(buf, off, ai); return -1 - n, off
    if major == 2:  # byte string
        if ai == 31:
            out = bytearray()
            while buf[off] != 0xFF:
                chunk, off = _dec(buf, off); out += bytes(chunk)
            return bytes(out), off + 1
        n, off = _read_len(buf, off, ai); return buf[off:off + n], off + n
    if major == 3:  # text string
        if ai == 31:
            parts = []
            while buf[off] != 0xFF:
                chunk, off = _dec(buf, off); parts.append(chunk)
            return "".join(parts), off + 1
        n, off = _read_len(buf, off, ai); return buf[off:off + n].decode("utf-8", "replace"), off + n
    if major == 4:  # array
        if ai == 31:
            out = []
            while buf[off] != 0xFF:
                v, off = _dec(buf, off); out.append(v)
            return out, off + 1
        n, off = _read_len(buf, off, ai)
        out = []
        for _ in range(n):
            v, off = _dec(buf, off); out.append(v)
        return out, off
    if major == 5:  # map
        out = {}
        if ai == 31:
            while buf[off] != 0xFF:
                k, off = _dec(buf, off); v, off = _dec(buf, off); out[k] = v
            return out, off + 1
        n, off = _read_len(buf, off, ai)
        for _ in range(n):
            k, off = _dec(buf, off); v, off = _dec(buf, off); out[k] = v
        return out, off
    if major == 6:  # tag — 값만 통과
        _tag, off = _read_len(buf, off, ai); return _dec(buf, off)
    if major == 7:  # simple / float
        if ai == 20: return False, off
        if ai == 21: return True, off
        if ai == 22: return None, off
        if ai == 23: return None, off
        if ai == 24: return buf[off], off + 1
        if ai == 25: off += 2; return None, off
        if ai == 26: off += 4; return None, off
        if ai == 27: off += 8; return None, off
        return None, off
    raise _CborError(f"bad major type {major}")


def _to_bytes(x) -> bytes:
    """CBOR 값(바이트열 또는 uint8 배열)을 bytes 로 환원. 카톡은 배열로 쓴다."""
    if isinstance(x, (bytes, bytearray)):
        return bytes(x)
    if isinstance(x, list):
        return bytes(int(v) & 0xFF for v in x)
    raise _CborError(f"cannot coerce {type(x)} to bytes")


# ----------------------------------------------------------------- 저장소/경로
def users_dir() -> str:
    """카톡 users 폴더. 서비스(LocalSystem)면 KATOK_USERS_DIR 로 넘긴 실제 사용자 경로."""
    ov = os.environ.get("KATOK_USERS_DIR")
    if ov:
        return ov
    return os.path.join(os.environ["LOCALAPPDATA"], "Kakao", "KakaoTalk", "users")


def user_dirs(users=None, live=True) -> list[str]:
    """users 폴더의 서브폴더들, 이름순. live=True 면 비-backup(라이브), False 면 backup.
    users 폴더가 없거나(비-Windows) 주어지지 않으면 []."""
    if users is None:
        try:
            users = users_dir()
        except KeyError:
            return []
    try:
        subs = sorted(os.listdir(users))
    except OSError:
        return []
    return [os.path.join(users, d) for d in subs
            if ("_backup_" not in d) == live and os.path.isdir(os.path.join(users, d))]


def user_dir(users=None, live=True) -> str | None:
    """user_dirs 의 첫 번째 결과(클라이언트는 보통 하나)."""
    d = user_dirs(users, live)
    return d[0] if d else None


def load_store(path: str) -> dict:
    """appstate.dat / keystore.bin 하나를 파싱. {'version','salt','map'} 반환.
    map = {relpath(str): wrapped_dek(bytes)}."""
    with open(path, "rb") as f:
        obj = _cbor_decode(f.read())
    if not isinstance(obj, dict):
        raise _CborError("keystore root is not a CBOR map")
    version = _to_bytes(obj["info_prefix"]).decode("latin1")
    salt = _to_bytes(obj["salt"])
    m: dict[str, bytes] = {}
    for entry in obj["wrapped_dek_map"]:
        relpath = entry[0]
        if isinstance(relpath, (bytes, bytearray, list)):
            # 카톡은 relpath 도 uint8 배열로 쓴다(byte string/text 아님)
            relpath = _to_bytes(relpath).decode("latin1")
        m[str(relpath)] = _to_bytes(entry[1])
    return {"version": version, "salt": salt, "map": m}


# ----------------------------------------------------------------- 인벤토리
def chatid_of_relpath(relpath: str) -> str | None:
    m = CHATLOG_RE.search(relpath.replace("\\", "/"))
    return m.group(1) if m else None
