# -*- coding: utf-8 -*-
"""File and SQLite access rules of SPEC 10.1 / 11 (share-delete reads, read-only SQLite, temp dirs).

Every function takes a path as str or os.PathLike. The Windows APIs (kernel32, advapi32, msvcrt) are loaded on
first use, so the module imports on any OS."""
import hashlib
import os
import shutil
import sqlite3
import stat
import tempfile
import time
from pathlib import Path
from urllib.request import pathname2url

_GENERIC_READ = 0x80000000
_SHARE_ALL = 0x1 | 0x2 | 0x4
_OPEN_EXISTING = 3
_ATTR_NORMAL = 0x80
_RETRY = (5, 32, 33)
_k32 = None


def _kernel32():
    """kernel32 with CreateFileW's prototype (Windows only), loaded once."""
    global _k32
    if _k32 is None:
        import ctypes  # noqa: PLC0415
        import ctypes.wintypes as wt  # noqa: PLC0415
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateFileW.restype = wt.HANDLE
        k32.CreateFileW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD, ctypes.c_void_p, wt.DWORD, wt.DWORD, wt.HANDLE]
        _k32 = k32
    return _k32


def read_shared(path, limit=None, retries=6):
    """Whole-file (or first `limit` bytes) read with FILE_SHARE_READ|WRITE|DELETE, handle closed at once."""
    if os.name != "nt":
        # a POSIX open never denies other openers, renames or deletes: a plain read is the same contract.
        # The errors name no path, as on Windows.
        try:
            with Path(path).open("rb") as fh:
                return fh.read() if limit is None else fh.read(limit)
        except (FileNotFoundError, NotADirectoryError):
            raise FileNotFoundError(2, "not found") from None
        except OSError as e:
            raise OSError(e.errno, "read_shared failed") from None
    import ctypes  # noqa: PLC0415
    import ctypes.wintypes as wt  # noqa: PLC0415
    import msvcrt  # noqa: PLC0415
    k32 = _kernel32()
    invalid = wt.HANDLE(-1).value
    name = os.fspath(path)          # CreateFileW takes the path's text
    err = 0
    for i in range(retries):
        h = k32.CreateFileW(name, _GENERIC_READ, _SHARE_ALL, None, _OPEN_EXISTING, _ATTR_NORMAL, None)
        if h == invalid or h is None:
            err = ctypes.get_last_error()
            if err in (2, 3):
                raise FileNotFoundError(2, "not found")
            if err in _RETRY and i + 1 < retries:
                time.sleep(0.2 * (i + 1))
                continue
            raise OSError(err, "read_shared failed")
        fd = msvcrt.open_osfhandle(h, os.O_RDONLY | os.O_BINARY)
        with os.fdopen(fd, "rb") as fh:
            return fh.read() if limit is None else fh.read(limit)
    raise OSError(err, "read_shared failed")


def snap(path):
    """(size, mtime_ns) or None. stat only asks for attributes, so it never blocks writers."""
    try:
        st = Path(path).stat()
        return (st.st_size, st.st_mtime_ns)
    except FileNotFoundError:
        return None


def mtime_s(path):
    try:
        return int(Path(path).stat().st_mtime)
    except FileNotFoundError:
        return None


def size(path):
    """File size in bytes, 0 when it cannot be read."""
    try:
        return Path(path).stat().st_size
    except OSError:
        return 0


def ro_uri(path, immutable):
    # abspath, not Path.resolve(): '..' is folded without following links or junctions (the same URI text as before)
    return "file:" + pathname2url(os.path.abspath(path)) + ("?mode=ro&immutable=1" if immutable else "?mode=ro")


def connect_ro(path, immutable):
    con = sqlite3.connect(ro_uri(path, immutable), uri=True)
    con.execute("PRAGMA query_only=1")
    con.execute("PRAGMA temp_store=MEMORY")
    return con


def sha256_file(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def suffixed(path, suffix) -> Path:
    """`path` with `suffix` appended to its last component (state.sqlite -> state.sqlite-wal, x.json -> x.json.tmp)."""
    path = Path(path)
    return path.with_name(path.name + suffix)


def fsync_write(path, data: bytes):
    with Path(path).open("wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())


def atomic_write(path, data: bytes):
    """fsync_write to path.tmp, then replace: a crash leaves the old file or the new one, never a torn one."""
    tmp = suffixed(path, ".tmp")
    fsync_write(tmp, data)
    replace_retry(tmp, path)


def replace_retry(src, dst, attempts=10):
    for i in range(attempts):
        try:
            Path(src).replace(dst)
            return
        except PermissionError:
            if i + 1 == attempts:
                raise
            time.sleep(0.2 * (i + 1))


def lock_fd(fd):
    """Lock byte 0 of an open file without blocking (OSError when held): per open file, released by the OS on exit."""
    if os.name == "nt":
        import msvcrt  # noqa: PLC0415
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    else:
        import fcntl  # noqa: PLC0415
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def unlock_fd(fd):
    if os.name == "nt":
        import msvcrt  # noqa: PLC0415
        os.lseek(fd, 0, 0)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl  # noqa: PLC0415
        fcntl.flock(fd, fcntl.LOCK_UN)


def make_readonly(path):
    Path(path).chmod(stat.S_IREAD)


def remove_file(path):
    path = Path(path)
    try:
        path.chmod(stat.S_IWRITE | stat.S_IREAD)
    except OSError:
        pass
    path.unlink(missing_ok=True)


def clear_dir(path):
    path = Path(path)
    if not path.is_dir():
        return
    def onerr(func, p, _exc):
        try:
            Path(p).chmod(stat.S_IWRITE | stat.S_IREAD)
            func(p)
        except OSError:
            pass
    for p in path.iterdir():
        if p.is_dir():
            shutil.rmtree(p, onexc=onerr)
        else:
            remove_file(p)


def token_user_sid():
    """String SID of this process's token user (S-1-5-18 = LocalSystem), or None if it cannot be read (always off
    Windows: there is no LocalSystem token there)."""
    if os.name != "nt":
        return None
    import ctypes  # noqa: PLC0415
    import ctypes.wintypes as wt  # noqa: PLC0415
    k32 = _kernel32()
    adv = ctypes.WinDLL("advapi32", use_last_error=True)
    adv.OpenProcessToken.argtypes = [wt.HANDLE, wt.DWORD, ctypes.POINTER(wt.HANDLE)]
    adv.OpenProcessToken.restype = wt.BOOL
    adv.GetTokenInformation.argtypes = [wt.HANDLE, ctypes.c_int, ctypes.c_void_p, wt.DWORD, ctypes.POINTER(wt.DWORD)]
    adv.GetTokenInformation.restype = wt.BOOL
    adv.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wt.LPWSTR)]
    adv.ConvertSidToStringSidW.restype = wt.BOOL
    k32.GetCurrentProcess.restype = wt.HANDLE
    k32.CloseHandle.argtypes = [wt.HANDLE]
    k32.LocalFree.argtypes = [ctypes.c_void_p]
    tok = wt.HANDLE()
    if not adv.OpenProcessToken(k32.GetCurrentProcess(), 0x0008, ctypes.byref(tok)):   # TOKEN_QUERY
        return None
    try:
        n = wt.DWORD()
        adv.GetTokenInformation(tok, 1, None, 0, ctypes.byref(n))                         # TokenUser
        buf = ctypes.create_string_buffer(n.value)
        if not n.value or not adv.GetTokenInformation(tok, 1, buf, n, ctypes.byref(n)):
            return None
        psid = ctypes.c_void_p.from_buffer(buf).value                                      # TOKEN_USER.User.Sid
        out = wt.LPWSTR()
        if not adv.ConvertSidToStringSidW(psid, ctypes.byref(out)):
            return None
        try:
            return out.value
        finally:
            k32.LocalFree(ctypes.cast(out, ctypes.c_void_p))
    finally:
        k32.CloseHandle(tok)


def setup_process(work_dir):
    """TMP/TEMP -> raw/.work/<run>, no heap in crash dumps (SPEC 11)."""
    Path(work_dir).mkdir(parents=True, exist_ok=True)
    text = os.fspath(work_dir)      # environment and tempfile.tempdir hold text
    os.environ["TMP"] = text
    os.environ["TEMP"] = text
    tempfile.tempdir = text
    if os.name == "nt":
        try:
            import ctypes  # noqa: PLC0415
            ctypes.windll.kernel32.WerSetFlags(1)  # WER_FAULT_REPORTING_FLAG_NOHEAP
        except Exception:  # noqa: BLE001
            pass
