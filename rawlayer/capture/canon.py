# -*- coding: utf-8 -*-
"""Canonical JSON, record hashes and the small value rules of SPEC sections 5-6."""
import base64
import datetime as _dt
import hashlib
import json
import re

_DEC = re.compile(r"^-?[0-9]+$")
_PK_BAD = re.compile("[\x00-\x1f:]")
KST_OFFSET_S = 32400
_EPOCH = _dt.datetime(1970, 1, 1)


def enc(x):
    if isinstance(x, (bytes, bytearray, memoryview)):
        return {"$b64": base64.b64encode(bytes(x)).decode("ascii")}
    if isinstance(x, dict):
        return {str(k): enc(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [enc(v) for v in x]
    return x


def _b64(o):
    if isinstance(o, (bytes, bytearray, memoryview)):
        return {"$b64": base64.b64encode(bytes(o)).decode("ascii")}
    raise TypeError("not canonical")


_ENCODER = json.JSONEncoder(sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False, default=_b64)


def canon(x) -> str:
    """Same bytes as json.dumps(enc(x), ...) of SPEC 6 (the C encoder does the walk; bytes go through
    `default`). Every dict key in this package is already a str. Raises ValueError for NaN/Inf."""
    return _ENCODER.encode(x)


def sha_ascii(s: str) -> str:
    return hashlib.sha256(s.encode("ascii")).hexdigest()


def record_h(native, derived) -> str:
    return sha_ascii(canon({"n": native, "d": derived}))


def as_int(v):
    """I() of SPEC section 6."""
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, str) and _DEC.fullmatch(v):
        return int(v)
    return None


def as_str(v):
    """S() of SPEC section 6."""
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return str(v)
    if isinstance(v, str):
        return v
    return None


_DAY_CACHE = {}


def kst_day(ts_ms) -> str:
    if ts_ms is None:
        return "none"
    d = (ts_ms // 1000 + KST_OFFSET_S) // 86400
    s = _DAY_CACHE.get(d)
    if s is None:
        s = _DAY_CACHE[d] = (_EPOCH + _dt.timedelta(days=d)).date().isoformat()
    return s


def facts_digest(lines) -> str:
    """sha256 over sorted key + TAB + h lines (SPEC 9.3)."""
    return hashlib.sha256("\n".join(sorted(lines)).encode("utf-8")).hexdigest()


def source_id(relpath: str) -> str:
    """sha256 of the relpath's text, cut to 12 hex. The text is the id: a Path (whose str() spells separators
    the OS's way) is refused, not converted."""
    return hashlib.sha256(relpath.encode("utf-8")).hexdigest()[:12]


def pk_text(v):
    """PK value -> key component, or None when the value cannot be a key part (pk_bad)."""
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return str(v)
    if isinstance(v, str) and v != "" and not _PK_BAD.search(v):
        return v
    return None


_QUOTED = re.compile(r"^'(.*)'$", re.S)
_FLOAT = re.compile(r"^-?[0-9]+\.[0-9]+$")
NO_DEFAULT = object()


def parse_default(d):
    """SPEC 8.4. Returns NO_DEFAULT when no value can equal the default."""
    if d is None:
        return None
    s = str(d).strip()
    if s.upper() == "NULL":
        return None
    if _DEC.fullmatch(s):
        return int(s)
    if _FLOAT.fullmatch(s):
        return float(s)
    m = _QUOTED.fullmatch(s)
    if m:
        return m.group(1).replace("''", "'")
    return NO_DEFAULT


def default_equal(value, d) -> bool:
    pd = parse_default(d)
    if pd is NO_DEFAULT:
        return False
    if pd is None:
        return value is None
    if isinstance(value, bool) or isinstance(value, (bytes, bytearray, memoryview)):
        return False
    return type(value) is type(pd) and value == pd
