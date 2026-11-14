# -*- coding: utf-8 -*-
"""
wincrypt.py — Windows 카카오톡 로컬 DB(.edb) 연구용 복호화 툴킷
================================================================
kakatok(sample/kakatok/katok_decrypt.py)에서 kakaotalk app으로 옮겼다(독립화, 2026-10).
CLI(info/userid/live)는 setup 프로비저닝(`manage.py setup kakaotalk --provision`)이 대신한다.

대상: 본인 계정·본인 기기의 카카오톡 데이터를 오프라인에서 복호화/분석 (연구용).
Scope: the owner's own, signed-in account on this machine only — offline personal archival and research.
Nothing is sent anywhere; recovered keys stay local and are never printed.

라이브(25.7.2+, SQLCipher4) 방별 raw DEK — appstate.dat 언랩(keychain) 또는 실행 중 프로세스 메모리에서
키 회수(scan_memory_keys). 구버전 백업(EvaSQLite) 복호화는 2026-11 에 제거했다: 이미 복호화된 폴더만 읽는다.

의존성: pip install pycryptodome   (표준 라이브러리 ctypes 사용)

주의: 메모리 스캔(scan_memory_keys)은 KakaoTalk 이 실행 중이어야 하며(=키가 메모리에 상주),
      보고 싶은 방을 카톡에서 한 번 열어두면 그 방의 키가 메모리에 올라와 회수 성공률이 높아진다.
"""
import os
from Crypto.Cipher import AES

# ---------------------------------------------------------------- 공통 상수/유틸
PAGE = 4096
SC_RESERVE, SC_ITERS = 80, 256000               # SQLCipher4 (라이브 ActionLogDB 로 실측 확정)

def read_shared(path, nbytes=-1):
    with open(path, "rb") as f:      # 백업/원본 모두 공유 읽기로 접근
        return f.read() if nbytes < 0 else f.read(nbytes)

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
