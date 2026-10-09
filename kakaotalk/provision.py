# -*- coding: utf-8 -*-
"""kakaotalk's side of `manage.py setup` (the AppConfig.setup_defaults / setup_provision hooks): the config's
default source paths, and the provisioning of every capture input the app derives itself — the root key, the
keystore, the decrypted backup, the macOS database. setup runs all of it on a first run; `setup --provision`
reruns it against the config.json that is already there (the recovery path when a credential rotated), with
`--scan` (the client's memory fallback), `--root` (a root file to install, verified first) and
`--restart-client` (restart KakaoTalk.exe to rescan a freshly started client).

Defaults (setup_defaults -> fill_defaults): each placeholder source resolves to this machine's kakatok research
tree when the file is there (the tree this app grew out of: Downloads\\kakatok, Documents\\kakatok), and the
app's own outputs — keystore, backup_decrypted — to their place under the data root when it is not. The client's
user directory is detected under %LOCALAPPDATA%\\Kakao\\KakaoTalk\\users (the non-backup folder holding
appstate.dat). Nothing is printed but the key names.

The root (setup_provision -> provision): the one input capture cannot read anywhere else. Every room's DEK is
wrapped under it (KEK = HKDF-SHA256(root, appstate.salt, info_prefix + relpath), keychain.py), and on-disk
artifacts do not hold it (profile.dat's plaintext is a stray value; the session material behind the live root
never leaves the client's VM transform — the kakatok research, PAPER.md §6). Setup therefore installs it in
three steps, each verified against the client's own store, unwrap success being the root's only honest oracle
(keychain.root_status):

  1. a root already under <DATA_ROOT>/keys/root_v2.hex stays while it unwraps the store;
  2. a copy on this machine is adopted — the research tree's root_v2.hex files, verified before installing;
  3. the running client's memory is scanned for a 32-byte value that unwraps the store (the same candidate walk
     the DEK scan uses; the rooms recover best right after they were opened, and --restart-client restarts
     KakaoTalk.exe so its auto-opened rooms re-derive — clients that leave the root in memory a while can be
     caught that way; residency is version-dependent, so this is best-effort).

With a root the keystore follows (write_keystore_for, the derivation the old keys command ran: every entry the
root unwraps, each key checked against its room file's page-1 header oracle; --scan adds the room-DEK memory
fallback for rooms the root does not key). The key-source files' (sha256, size, mtime) fingerprint is diffed
against the previous run in <DATA_ROOT>/keys/rotation_state.json, so a logout/re-login rotation is reported the
moment it is observed. Then the backup user directory decrypts (kakaotalk/backup.py, EvaSQLite: registry
pragma + userId — needs the KakaoTalk user's own HKCU, which is why provisioning lives in setup, run
interactively, not in the SYSTEM task), and on a Mac the client database decrypts (kakaocli port) when the
config sets mac_db. Keys are credentials: every line this module prints is counts, paths and statuses — never
key material.
"""
from __future__ import annotations

import hmac
import json
import os
import subprocess
import time
from pathlib import Path

from rawlayer.capture import paths

from . import katok_decrypt as kd
from . import keychain
from . import keystore_v2 as kv

# the kakatok research tree under the home: the collector outputs this app reads and the root copies it kept.
# A file is a default only while it exists; the app's own outputs fall back to the data root.
RESEARCH = {
    "merged_out": ("Downloads", "kakatok", "merged", "merged.sqlite"),
    "live_out": ("Documents", "kakatok", "out", "live.sqlite"),
    "backup_decrypted": ("Downloads", "kakatok", "backup_decrypted"),
    "decrypted_live": ("Downloads", "kakatok", "decrypted_live"),
    "keystore": ("Documents", "kakatok", "out", "keystore_v2_full.json"),
}
# where a root copy may sit, in the order they are tried (adoption verifies before installing)
ROOT_FILES = (
    ("Documents", "kakatok", "root_v2.hex"),
    ("Documents", "kakatok", "out", "root_v2.hex"),
    ("Downloads", "kakatok", "root_v2.hex"),
)


def _home() -> Path:
    return Path.home()


def _users_dir() -> str | None:
    """The client's users folder, or None where no Windows client lives (no LOCALAPPDATA)."""
    try:
        return kv.users_dir()
    except KeyError:
        return None


def store_file(udir) -> str | None:
    """The keystore store in a client user directory: appstate.dat (v2), else keystore.bin (v1)."""
    for name in ("appstate.dat", "keystore.bin"):
        p = os.path.join(udir, name)
        if os.path.exists(p):
            return p
    return None


def live_user_dir(users=None) -> str | None:
    """The client's live user directory: the non-backup folder under the users folder that holds a store."""
    for sub in kv.user_dirs(users):
        if store_file(sub):
            return sub
    return None


# ----------------------------------------------------------------- config defaults
def default_sources(cfg, home=None, users=None) -> dict:
    """The machine's defaults for config.example.json's placeholder sources: the research tree's files that are
    there, the detected client user directory, and the app's own outputs under the data root."""
    home = Path(home) if home is not None else _home()
    out: dict[str, str] = {}
    for key, rel in RESEARCH.items():
        p = home.joinpath(*rel)
        if p.exists():
            out[key] = str(p)
    udir = live_user_dir(users)
    if udir:
        out["kakao_user_dir"] = udir
    data_root = Path(cfg["data_root"])
    out.setdefault("backup_decrypted", str(data_root / "backup_decrypted"))   # backup's output
    out.setdefault("keystore", str(data_root / "keys" / "keystore.json"))     # keys'/setup's output
    return out


def fill_defaults(cfg, home=None, users=None, note=None) -> list:
    """Set the detected defaults for the config's placeholder (or missing) sources, in place; the keys filled."""
    defaults = default_sources(cfg, home, users)
    src = cfg.setdefault("sources", {})
    filled = []
    for key, value in defaults.items():
        v = src.get(key)
        if v is None or (isinstance(v, str) and "<" in v and ">" in v):
            src[key] = value
            filled.append(key)
    if filled and note is not None:
        note(f"default sources set: {', '.join(sorted(filled))}")
    return filled


# ----------------------------------------------------------------- the root
def candidate_roots(home=None) -> list:
    """The files a root copy may live in on this machine, in the order they are tried."""
    home = Path(home) if home is not None else _home()
    return [home.joinpath(*rel) for rel in ROOT_FILES]


def root_in(cands, store) -> bytes | None:
    """The candidate that unwraps the store's first entry — the oracle the memory scan tests each 32-byte
    window with (a wrong candidate passes with probability 2**-64; root_status verifies the whole store).
    HKDF is inlined as one-shot hmac.digest calls: this loop runs a million times."""
    if not store["map"]:
        return None
    relpath, wrapped = next(iter(store["map"].items()))
    salt = store["salt"]
    info = store["version"].encode("latin1") + relpath.encode("latin1")
    for cand in cands:
        try:
            keychain.aes_kw_unwrap(hmac.digest(hmac.digest(salt, cand, "sha256"), info + b"\x01", "sha256"),
                                   wrapped)
        except ValueError:
            continue
        return bytes(cand)
    return None


def scan_memory_root(store, pid=None, note=None):
    """A root lingering in the running client's memory, or None. The same candidate walk the DEK scan uses,
    tested with the unwrap oracle instead of the page-1 header."""
    cands, pid = kd.memory_candidates(pid)
    if note is not None:
        note(f"scanning KakaoTalk.exe (pid {pid}) for the root: {len(cands):,} candidates, a minute or two")
    return root_in(sorted(cands), store)


def install_root(keys_dir: Path, root: bytes) -> Path:
    """root_v2.hex under the protected data root, written atomically; the hex never travels elsewhere."""
    keys_dir.mkdir(parents=True, exist_ok=True)
    path = keys_dir / "root_v2.hex"
    tmp = path.with_name("root_v2.hex.part")
    tmp.write_text(root.hex(), encoding="ascii")
    tmp.replace(path)
    return path


def client_exe() -> str | None:
    """Where KakaoTalk.exe lives: the running client's own path, else the standard install locations."""
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "(Get-Process -Name KakaoTalk -ErrorAction SilentlyContinue).Path"],
            capture_output=True, text=True, timeout=30).stdout.strip()
        if out:
            return out.splitlines()[0].strip()
    except (OSError, subprocess.SubprocessError):
        pass
    cands = []
    env = os.environ.get("LOCALAPPDATA")
    if env:
        cands.append(os.path.join(env, "Kakao", "KakaoTalk", "KakaoTalk.exe"))
    for root in (r"C:\Program Files (x86)", r"C:\Program Files"):
        cands.append(os.path.join(root, "Kakao", "KakaoTalk", "KakaoTalk.exe"))
    return next((p for p in cands if os.path.exists(p)), None)


def relaunch_client(note):
    """(Re)start KakaoTalk.exe and wait out its auto-login: the only window in which a client that keeps the
    root in memory after deriving can be scanned. Returns the new pid, or None."""
    exe = client_exe()
    if exe is None:
        note("KakaoTalk.exe was not found on this machine")
        return None
    if kd._find_pid():
        subprocess.run(["taskkill", "/f", "/im", "KakaoTalk.exe"], capture_output=True, timeout=60)
        for _ in range(30):
            if not kd._find_pid():
                break
            time.sleep(0.5)
    note("starting KakaoTalk.exe and waiting for its auto-login (about half a minute)")
    try:
        subprocess.Popen([exe], cwd=os.path.dirname(exe), close_fds=True)
    except OSError as e:
        note(f"could not start the client (error {type(e).__name__})")
        return None
    for _ in range(60):                     # the client takes ~20s to show its window and log in
        pid = kd._find_pid()
        if pid:
            time.sleep(20)                  # the auto-login settles (the 2026-10-03 observation)
            return kd._find_pid() or pid
        time.sleep(1)
    note("KakaoTalk.exe did not come up")
    return None


# ----------------------------------------------------------------- the keystore (the old keys command)
def derive_entries(root: bytes, store: dict, udir: str) -> dict:
    """The keystore entries for every store entry the root unwraps: each key checked against its room file's
    page-1 header oracle when the file is there. Entries that do not unwrap are left out, so capture reads
    those rooms as no_key rather than with a bogus key."""
    deks = keychain.unwrap_store(root, store)
    entries = {}
    for relpath, wrapped in store["map"].items():
        dek = deks.get(relpath)
        if dek is None:
            continue
        p = os.path.join(udir, relpath)
        entries[relpath] = {
            "chatId": kv.chatid_of_relpath(relpath),
            "key_hex": dek.hex(),
            "wrapped_hex": wrapped.hex(),
            "integrity": True,
            "header_ok": keychain.verify_dek(dek, p) if os.path.exists(p) else None,
        }
    return entries


def scan_fallback(udir, keyed_chats, note) -> dict:
    """Room files on disk a live root did not key: recover their DEKs from the running client's memory."""
    targets = []
    chat_data = os.path.join(udir, "chat_data")
    for name in sorted(os.listdir(chat_data)) if os.path.isdir(chat_data) else []:
        p = os.path.join(chat_data, name)
        cid = kv.chatid_of_relpath(name)
        if cid is None or cid in keyed_chats or not os.path.exists(p):
            continue
        try:
            head = kd.read_shared(p, kd.PAGE)
        except OSError:
            continue
        if len(head) >= kd.PAGE:
            targets.append((p, head[16:32], head[kd.PAGE - kd.SC_RESERVE:kd.PAGE - kd.SC_RESERVE + 16]))
    if not targets:
        return {}
    note(f"memory scan of {len(targets)} unkeyed room(s) (KakaoTalk.exe must be running; tens of seconds)")
    try:
        hits, ncand = kd.scan_memory_keys(targets)
    except (RuntimeError, OSError) as e:
        note(f"  scan not available: {e}")
        return {}
    note(f"  {len(hits)} key(s) recovered from {ncand:,} candidates")
    return {os.path.basename(p): key for p, key in hits.items()}


def write_keystore(path: str, entries: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(entries, fh, ensure_ascii=True, indent=1)
    os.replace(tmp, path)


def write_keystore_for(cfg, note, root, store, udir, scan=False) -> list:
    """The old keys command's body: write the configured keystore source from `root` (or, with scan=True and no
    root, from whatever the memory fallback recovers) and keep the key-source rotation fingerprint. Returns
    attention lines. No root and no scan writes nothing — the refusal the old command made."""
    if root is None and not scan:
        return []
    entries = derive_entries(root, store, udir) if root is not None else {}
    if scan:
        keyed = {kv.chatid_of_relpath(r) for r in entries}
        for name, key in scan_fallback(udir, keyed, note).items():
            relpath = "chat_data\\" + name
            if relpath in entries:
                continue
            p = os.path.join(udir, relpath)
            entries[relpath] = {"chatId": kv.chatid_of_relpath(relpath), "key_hex": key.hex(),
                                "wrapped_hex": "", "integrity": True,
                                "header_ok": keychain.verify_dek(key, p)}
    ks = (cfg.get("sources") or {}).get("keystore")
    if isinstance(ks, str) and "<" not in ks and ">" not in ks:
        write_keystore(ks, entries)
        rooms = sum(1 for e in entries.values() if e["chatId"] is not None)
        ok = sum(1 for e in entries.values() if e["header_ok"] is True)
        note(f"keystore written: {ks} ({len(entries)} entries, {rooms} rooms, {ok} header-verified)")

    keys_dir = Path(cfg["data_root"], "keys")
    os.makedirs(keys_dir, exist_ok=True)
    st_path = str(keys_dir / "rotation_state.json")
    fp = keychain.fingerprint(os.path.dirname(udir))
    prev = keychain.load_rotation_state(st_path).get("fingerprint")
    state = {"fingerprint": fp, "at": int(time.time())}
    if fp and prev:
        d = keychain.diff_fingerprint(prev, fp)
        if d["changed"] or d["added"] or d["removed"]:
            names = [c["name"] for c in d["changed"]] + d["added"] + d["removed"]
            note(f"key-source files changed since last run: {', '.join(names)}")
            state["last_change"] = {"at": int(time.time()), "files": names}
    keychain.save_rotation_state(st_path, state)

    if root is not None and store["map"] and not entries and not scan:
        return ["the root unwrapped nothing while the store has entries: the root has likely rotated "
                "(logout/re-login/device added). Recover one (setup --provision --scan) or install a new one "
                "with --root."]
    return []


# ----------------------------------------------------------------- the macOS database (the old macdb command)
def run_macdb(cfg, note, uuid=None, user_id=None, db=None, home=None, brute_limit=None) -> int:
    """The old macdb command's body: derive the client database's name and key from the platform UUID and the
    userId (the kakaocli port), decrypt it in one consistent snapshot into the configured mac_db path. Counts
    only, never the key. 0 ok · 2 refusal (mac_db unset, no uuid/userId/database) · 3 the key opens nothing."""
    from . import kakaocli
    out = cfg.get("mac_db")
    if not out:
        note('the config sets no "mac_db": the macOS database is not captured on this machine')
        return 2                                    # provision only calls this when mac_db is set
    home = home or os.path.expanduser("~")
    uuid = uuid or kakaocli.platform_uuid()
    if uuid is None:
        note("no IOPlatformUUID (ioreg): not macOS, or pass the uuid")
        return 2
    uid = user_id
    if uid is None:
        uid = kakaocli.user_id(home, brute_limit=brute_limit or kakaocli.BRUTE_DEFAULT)
        if uid is None:
            note(f"no userId in the KakaoTalk plists under {home} (strategies 1/2/3/4)")
            return 2
    db = db or kakaocli.discover_database_file(kakaocli.container_path(home), uid, uuid)
    if db is None or not os.path.exists(db):
        note(f"no encrypted database found for user {uid} in {kakaocli.container_path(home)}")
        return 2
    note(f"database: {os.path.basename(db)} (user {uid})")
    main_bytes, wal, stable = kakaocli.read_stable(db)
    if not stable:
        note("no consistent snapshot of the database (KakaoTalk is writing): retry")
        return 3
    image, facts = kakaocli.decrypt_db(main_bytes, wal, kakaocli.secure_key(uid, uuid))
    del main_bytes, wal
    if image is None:
        note("the derived key opens no SQLCipher page (wrong uuid/userId, or a new scheme)")
        return 3
    tmp = out + ".tmp"
    with open(tmp, "wb") as fh:
        fh.write(image)
    os.replace(tmp, out)
    note(f"decrypted: {facts['cipher']}, {facts['pages']} pages, wal {facts['wal']['state']} "
         f"({facts['wal']['frames_applied']} frames) -> {out}")
    return 0


# ----------------------------------------------------------------- the setup_provision hook
def provision(cfg, note, restart_client=False, home=None, scan=False, root_file=None) -> list:
    """Install a live root under <data_root>/keys, write the keystore, decrypt the backup and (on a Mac) the
    client database (setup_provision's body). Returns the attention lines; an empty list means the app's
    capture inputs are as ready as this machine allows."""
    from .backup import decrypt_backup
    src = cfg.get("sources") or {}
    udir = src.get("kakao_user_dir")
    if not isinstance(udir, str) or ("<" in udir and ">" in udir):
        return ["kakao_user_dir is not set: the keys come from the client's user directory"]
    sp = store_file(udir)
    if sp is None:
        return ["no keystore store in the client user directory (appstate.dat / keystore.bin)"]

    # the app's own output dir inside the data root exists from the start (a source path must)
    bak = src.get("backup_decrypted")
    if isinstance(bak, str) and "<" not in bak and ">" not in bak and paths.inside(Path(bak), Path(cfg["data_root"])):
        os.makedirs(bak, exist_ok=True)

    store = kv.load_store(sp)
    keys_dir = Path(cfg["data_root"], "keys")
    root_path = keys_dir / "root_v2.hex"
    root, st = None, None

    if root_file is not None:                       # --root: the operator's file, verified before installing
        cand = keychain.read_root_hex(root_file)
        if cand is None:
            return [f"the root file {root_file} is not a 64-hex-character 32-byte root"]
        st = keychain.root_status(cand, store)
        if st["alive"] or st["partial"]:
            install_root(keys_dir, cand)
            root = cand
            kind = "alive" if st["alive"] else "partial"
            note(f"root installed from {root_file} ({st['unwrapped']}/{st['entries']} entries unwrapped, {kind})")
        else:
            return [f"the root file {root_file} unwraps nothing (a rotated root is suspected)"]
    else:
        root = keychain.read_root_hex(str(root_path)) if root_path.exists() else None
        st = keychain.root_status(root, store) if root is not None else None
        if st is not None:
            kind = "alive" if st["alive"] else ("partial" if st["partial"] else "dead (rotated?)")
            note(f"root at keys/root_v2.hex: {st['unwrapped']}/{st['entries']} entries unwrapped ({kind})")
        if st is None or not st["alive"]:
            for cand_path in candidate_roots(home):
                if not cand_path.exists():
                    continue
                cand = keychain.read_root_hex(str(cand_path))
                if cand is None:
                    continue
                cst = keychain.root_status(cand, store)
                if cst["alive"] or cst["partial"]:
                    install_root(keys_dir, cand)
                    root, st = cand, cst
                    note(f"root adopted from {cand_path} ({cst['unwrapped']}/{cst['entries']} entries unwrapped)")
                    break
        if st is None or not st["alive"]:
            pid = kd._find_pid()
            if pid is None and restart_client:
                pid = relaunch_client(note)
            if pid is not None:
                try:
                    found = scan_memory_root(store, pid=pid, note=note)
                except (RuntimeError, OSError) as e:
                    note(f"memory scan not available: {e}")
                    found = None
                if found is not None:
                    fst = keychain.root_status(found, store)
                    install_root(keys_dir, found)
                    root, st = found, fst
                    note(f"root recovered from the client's memory "
                         f"({fst['unwrapped']}/{fst['entries']} entries unwrapped)")
                else:
                    note("the client's memory holds no root (current clients wipe it right after deriving it)")

    problems = write_keystore_for(cfg, note, root, store, udir, scan=scan)

    brc = decrypt_backup(cfg, note)
    if brc == 3:
        problems.append("the backup user directory has rooms but none decrypt: the pragma (registry "
                        "DeviceInfo) or the userId does not match this backup")
    elif brc not in (0, 2):
        problems.append(f"the backup did not decrypt (rc={brc})")

    if cfg.get("mac_db"):
        mrc = run_macdb(cfg, note)
        if mrc != 0:
            problems.append(f"the macOS database did not decrypt (rc={mrc}; check the uuid/userId on the Mac)")

    if problems:
        return problems
    if st is not None and (st["alive"] or st["partial"]):
        return []
    return ["no live root on this machine: capture reads the .edb rooms as no_key until one is derived "
            "(setup --provision [--scan] recovers one)"]
