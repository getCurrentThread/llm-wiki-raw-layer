# -*- coding: utf-8 -*-
"""
katok_decrypt.py — Windows 카카오톡 로컬 DB(.edb) 연구용 복호화 툴킷
================================================================
kakatok(sample/kakatok/katok_decrypt.py)에서 kakaotalk app으로 옮겼다(독립화, 2026-10).
CLI(info/userid/backup/live)는 setup 프로비저닝(`manage.py setup kakaotalk --provision`, kakaotalk/backup.py)이 대신한다.

대상: 본인 계정·본인 기기의 카카오톡 데이터를 오프라인에서 복호화/분석 (연구용).

두 스킴을 모두 다룬다.
  (1) 구버전/백업(≈25.7.2 미만, EvaSQLite)  : 레지스트리 pragma + userId 로 정적 복호화
  (2) 라이브(25.7.2+, SQLCipher4)            : 방별 raw DEK — appstate.dat 언랩(keychain) 또는
                                                실행 중 프로세스 메모리에서 키 회수(scan_memory_keys)

의존성: pip install pycryptodome   (표준 라이브러리 sqlite3/winreg/ctypes 사용)

주의: 메모리 스캔(scan_memory_keys)은 KakaoTalk 이 실행 중이어야 하며(=키가 메모리에 상주),
      보고 싶은 방을 카톡에서 한 번 열어두면 그 방의 키가 메모리에 올라와 회수 성공률이 높아진다.
"""
import os, base64, hashlib, sqlite3
from Crypto.Cipher import AES

# ---------------------------------------------------------------- 공통 상수/유틸
PAGE = 4096
SEED_NE = "88ac0ad1fce39846dac8a313513d85a2"   # dev_id_type != 2 (표준)
SEED_EQ = "01ac0ad1fce39846dad8a313513d85a2"   # dev_id_type == 2 (메인보드 교체/포터블)
SQLITE_MAGIC = b"SQLite format 3\x00"
SC_RESERVE, SC_ITERS = 80, 256000               # SQLCipher4 (라이브 ActionLogDB 로 실측 확정)

def md5(b): return hashlib.md5(b).digest()
def sha1(b): return hashlib.sha1(b).digest()
def sha512(b): return hashlib.sha512(b).digest()

def read_shared(path, nbytes=-1):
    with open(path, "rb") as f:      # 백업/원본 모두 공유 읽기로 접근
        return f.read() if nbytes < 0 else f.read(nbytes)

# ---------------------------------------------------------------- EvaSQLite (구버전/백업)
def read_deviceinfo():
    import winreg
    base = r"Software\Kakao\KakaoTalk\DeviceInfo"
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, base) as k:
        last, _ = winreg.QueryValueEx(k, "Last")      # 활성 서브키(날짜형) 이름을 가리키는 포인터
    vals = {}
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, base + "\\" + last) as k:
        for n in ["dev_id","sys_uuid","hdd_model","hdd_serial","hdd_serial_v2","os_ver","dev_id_type"]:
            try: vals[n] = winreg.QueryValueEx(k, n)[0]
            except FileNotFoundError: pass
    return vals

def _builtin_key(seed_hex):
    h = sha1(seed_hex.encode("ascii"))           # ★ Seed 는 ASCII 32바이트로 해싱(실측 확정)
    h = h + b"\x00"*(64-len(h))
    h = bytes(x ^ 0x36 for x in h)               # HMAC ipad
    return sha1(h)[:16]

def _pkcs7(b, bs=16):
    p = bs - (len(b) % bs); return b + bytes([p])*p

def make_pragma(dev):
    dt = str(dev.get("dev_id_type", ""))
    if dt == "2":
        msg = dev.get("hdd_serial",""); seed = SEED_EQ
    else:
        msg = f'{dev.get("sys_uuid","")}|{dev.get("hdd_model","")}|{dev.get("hdd_serial","")}'; seed = SEED_NE
    ct = AES.new(_builtin_key(seed), AES.MODE_CBC, b"\x00"*16).encrypt(_pkcs7(msg.encode("utf-8")))
    return base64.b64encode(sha512(ct)).decode("ascii")     # 88자

def eva_passphrase(dbtype, pragma, userid=None):
    if dbtype == 1: return base64.b64encode(md5((pragma+userid).encode())).decode()
    if dbtype == 2: return base64.b64encode(md5(("emoticon"+pragma+"emoticon").encode())).decode()
    if dbtype == 3: return pragma + userid                  # chatLogs / TalkUserDB / talkmedia ...
    if dbtype == 4: return "multiprofile" + pragma + userid
    if dbtype == 7: return "SIMON_IS_FREE"                  # ActionLogDB (고정)
    raise ValueError(dbtype)

def eva_key_iv(passphrase):
    pb = passphrase.encode()
    seed = (pb * (512//len(pb) + 1))[:512]
    key = md5(seed)
    return key, md5(base64.b64encode(key))

def eva_decrypt(data, key, iv):
    out = bytearray()
    for pos in range(0, len(data), PAGE):
        blk = data[pos:pos+PAGE]; n = len(blk)-(len(blk)%16)
        if n: out += AES.new(key, AES.MODE_CBC, iv).decrypt(blk[:n])
    return bytes(out)

def eva_decrypt_file(path, dbtype, pragma, userid=None):
    key, iv = eva_key_iv(eva_passphrase(dbtype, pragma, userid))
    dec = eva_decrypt(read_shared(path), key, iv)
    return dec, dec[:16] == SQLITE_MAGIC

# ---------------------------------------------------------------- SQLCipher4 (라이브)
def sqlcipher_decrypt(data, key, reserve=SC_RESERVE):
    end = PAGE - reserve; out = bytearray(); n = len(data)//PAGE
    for i in range(n):
        pg = data[i*PAGE:(i+1)*PAGE]; s = 16 if i == 0 else 0
        ct = pg[s:end]; iv = pg[end:end+16]
        if len(ct) % 16: ct = ct[:len(ct)//16*16]
        pt = AES.new(key, AES.MODE_CBC, iv).decrypt(ct)
        out += (SQLITE_MAGIC + pt + b"\x00"*reserve) if i == 0 else (pt + b"\x00"*reserve)
    return bytes(out)

def sqlcipher_key_from_passphrase(passphrase, salt):
    return hashlib.pbkdf2_hmac("sha512", passphrase.encode() if isinstance(passphrase,str) else passphrase,
                               salt, SC_ITERS, 32)

def get_userid_live(p=None):
    """라이브 ActionLogDB(SQLCipher4 + SIMON_IS_FREE) → Common.userid.
    p 를 주면 그 파일(카톡 users 루트의 ActionLogDB.edb)을, 없으면 LOCALAPPDATA 기준 경로를 쓴다."""
    from . import keystore_v2 as _kv  # noqa: PLC0415
    p = p or os.path.join(_kv.users_dir(), "ActionLogDB.edb")
    data = read_shared(p); key = sqlcipher_key_from_passphrase("SIMON_IS_FREE", data[:16])
    dec = sqlcipher_decrypt(data, key)
    tmp = p + ".dec.sqlite"; open(tmp,"wb").write(dec)
    try:
        con = sqlite3.connect(tmp); r = con.execute("select userid from Common where userid is not null limit 1").fetchone(); con.close()
        return str(r[0]) if r else None
    finally:
        try: os.remove(tmp)
        except OSError: pass

def get_userid_backup(backup_actionlog, pragma):
    data = read_shared(backup_actionlog); key, iv = eva_key_iv("SIMON_IS_FREE")
    dec = eva_decrypt(data, key, iv)
    tmp = backup_actionlog + ".dec.sqlite"; open(tmp,"wb").write(dec)
    try:
        con = sqlite3.connect(tmp); r = con.execute("select userid from Common where userid is not null limit 1").fetchone(); con.close()
        return str(r[0]) if r else None
    finally:
        try: os.remove(tmp)
        except OSError: pass

# ---------------------------------------------------------------- 메모리 키 스캔 (라이브)
def _find_pid(name="KakaoTalk.exe"):
    for line in os.popen(f'tasklist /FI "IMAGENAME eq {name}" /FO CSV /NH').read().splitlines():
        parts = line.split('","')
        if len(parts) >= 2:
            try: return int(parts[1].strip('"'))
            except ValueError: pass
    return None

def memory_candidates(pid=None):
    """KakaoTalk.exe 의 private-writable 메모리에서 32B 후보 집합 (8정렬, 엔트로피 필터, 중복 제거).
    (cands, pid) 반환. 미실행/OpenProcess 실패는 RuntimeError — 방 DEK 스캔과 루트 스캔이 공유한다.
    8바이트 스텝: 열린 방의 DEK는 16정렬에 놓이지만(2026-10-03 실측), 상시 열린 DB(chatListInfo 등)의
    DEK는 8정렬에만 상주하는 것이 2026-10-10 실측되었다 — 16 스텝은 그 절반을 놓친다."""
    import ctypes, ctypes.wintypes as wt
    pid = pid or _find_pid()
    if not pid: raise RuntimeError("KakaoTalk.exe 미실행")
    MEM_COMMIT=0x1000; MEM_PRIVATE=0x20000; GUARD=0x100; WRITABLE={0x04,0x08,0x40,0x80}
    class MBI(ctypes.Structure):
        _fields_=[("BaseAddress",ctypes.c_void_p),("AllocationBase",ctypes.c_void_p),("AllocationProtect",wt.DWORD),
                  ("__a1",wt.DWORD),("RegionSize",ctypes.c_size_t),("State",wt.DWORD),("Protect",wt.DWORD),("Type",wt.DWORD),("__a2",wt.DWORD)]
    k=ctypes.windll.kernel32
    k.OpenProcess.restype=wt.HANDLE; k.OpenProcess.argtypes=[wt.DWORD,wt.BOOL,wt.DWORD]
    k.VirtualQueryEx.restype=ctypes.c_size_t; k.VirtualQueryEx.argtypes=[wt.HANDLE,ctypes.c_void_p,ctypes.POINTER(MBI),ctypes.c_size_t]
    k.ReadProcessMemory.restype=wt.BOOL; k.ReadProcessMemory.argtypes=[wt.HANDLE,ctypes.c_void_p,ctypes.c_void_p,ctypes.c_size_t,ctypes.POINTER(ctypes.c_size_t)]
    h=k.OpenProcess(0x10|0x400, False, pid)
    if not h: raise RuntimeError("OpenProcess 실패(관리자 권한 필요할 수 있음)")
    cands=set(); addr=0; mbi=MBI()
    try:
        while addr < 0x7FFFFFFFFFFF:
            if k.VirtualQueryEx(h, ctypes.c_void_p(addr), ctypes.byref(mbi), ctypes.sizeof(mbi))==0: break
            base=mbi.BaseAddress or 0; size=mbi.RegionSize or PAGE; prot=mbi.Protect
            if mbi.State==MEM_COMMIT and mbi.Type==MEM_PRIVATE and prot in WRITABLE and not (prot&GUARD) and size>=32:
                buf=ctypes.create_string_buffer(size); nr=ctypes.c_size_t(0)
                if k.ReadProcessMemory(h, ctypes.c_void_p(base), buf, size, ctypes.byref(nr)) and nr.value>=32:
                    d=buf.raw[:nr.value]
                    for o in range(0, len(d)-31, 8):
                        w=d[o:o+32]
                        if w.count(0)<8 and len(set(w))>=18: cands.add(w)
            na=base+size
            if na<=addr: break
            addr=na
    finally:
        k.CloseHandle(h)
    return cands, pid

def scan_memory_keys(targets, pid=None):
    """targets: [(name, ct16, iv), ...]  → {name: key(32B)}  (private-writable, 8정렬, 엔트로피 필터)."""
    cands, _pid = memory_candidates(pid)
    ECB=AES.MODE_ECB; hits={}; ntargets=len(targets)
    for key in cands:
        cipher=AES.new(key,ECB)         # ECB 는 상태 없음 → 후보당 1회 생성해 전 타깃에 재사용
        for name,ct16,iv in targets:    # (타깃마다 AES 객체 생성하던 것을 제거 → 타깃 수에 거의 무관)
            if name in hits: continue
            ecb=cipher.decrypt(ct16)
            if (ecb[0]^iv[0])==0x10 and (ecb[1]^iv[1])==0x00:
                p=bytes(ecb[i]^iv[i] for i in range(8))
                if p[4]==SC_RESERVE and p[5]==64 and p[6]==32 and p[7]==32 and p[2] in (1,2) and p[3] in (1,2):
                    hits[name]=key; break
        if len(hits)>=ntargets: break   # 모든 타깃 회수 완료면 나머지 후보 스킵
    return hits, len(cands)
