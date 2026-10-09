# -*- coding: utf-8 -*-
"""The decrypted backup of the PC client's backup user directory (the old `manage.py backup kakaotalk` command,
now setup's provisioning step).

The EvaSQLite scheme of pre-25.7.2 KakaoTalk backups, ported from kakatok (kakaotalk/katok_decrypt.py): the
HKCU Software\\Kakao\\KakaoTalk\\DeviceInfo registry values make the machine's pragma, the userId comes from the
backup's ActionLogDB (users/ActionLogDB.edb_*.backup, EvaSQLite with the fixed SIMON_IS_FREE passphrase) or the
live ActionLogDB next to the configured users directory, and every chat_data/chatLogs_<chatId>.edb decrypts with
the dbtype-3 passphrase into <chatId>.sqlite under the configured "backup_decrypted" source — the layout and
_manifest.json ({"decrypted": [{"chatId", "rows", "from", "to"}], "updated"}) readers/out.py reads.

This owns that directory: a room whose source no longer decrypts loses its stale decrypted file, so capture
never mistakes an old copy for the backup's current content (the origin's room_gone note reports it).
TalkUserDB.edb, when present, decrypts too (best-effort; capture only counts it as a non-room file).

The registry read needs the KakaoTalk user's own HKCU: it runs from setup, interactively as that user — never
as the SYSTEM scheduled task (live capture; the task only needs live_out and the .edb rooms).

decrypt_backup returns the old command's exit codes: 0 ok (an empty backup directory is ok) · 2 a refusal to
run (no backup user directory, registry unreadable, no userId — a skip as far as setup is concerned) · 3 rooms
exist but none decrypt (wrong pragma or userId suspected) · 1 error."""
import datetime as _dt
import glob
import json
import os
import re
import sqlite3

from . import katok_decrypt as kd
from . import keystore_v2 as kv

_ROOM = re.compile(r"^chatLogs_(\d+)\.edb$", re.IGNORECASE)
_SQLITE = re.compile(r"^(\d+)\.sqlite$")


def _rows_of(path):
    con = sqlite3.connect(path)
    try:
        cols = {c[1] for c in con.execute("PRAGMA table_info(chatLogs)")}
        if "logId" not in cols:
            return None
        return con.execute("SELECT COUNT(*) FROM chatLogs").fetchone()[0]
    finally:
        con.close()


def _write(path, data):
    tmp = path + ".tmp"
    with open(tmp, "wb") as fh:
        fh.write(data)
    os.replace(tmp, path)


def decrypt_backup(cfg, note) -> int:
    """cfg: the app's config dict (kakao_user_dir and backup_decrypted sources). note: the setup line."""
    src = cfg.get("sources") or {}
    udir = src.get("kakao_user_dir")
    outdir = src.get("backup_decrypted")
    if not isinstance(udir, str) or not isinstance(outdir, str):
        note("backup skipped: kakao_user_dir or backup_decrypted is not set")
        return 2
    users = os.path.dirname(udir)
    bak = kv.user_dir(users, live=False)
    if bak is None:
        note("backup skipped: no backup user directory (*_backup_*) under the users folder")
        return 2
    try:
        dev = kd.read_deviceinfo()
    except OSError as e:
        note(f"backup skipped: registry DeviceInfo unreadable ({e}); run as the KakaoTalk user, interactively")
        return 2
    pragma = kd.make_pragma(dev)
    alogs = sorted(glob.glob(os.path.join(users, "ActionLogDB.edb_*.backup")))
    try:
        uid = kd.get_userid_backup(alogs[0], pragma) if alogs else \
            kd.get_userid_live(os.path.join(users, "ActionLogDB.edb"))
    except (OSError, sqlite3.Error) as e:
        note(f"backup skipped: ActionLogDB unreadable ({type(e).__name__}: {e})")
        return 2
    if not uid:
        note("backup skipped: no userId found in ActionLogDB")
        return 2

    rooms = sorted(glob.glob(os.path.join(bak, "chat_data", "chatLogs_*.edb")))
    manifest, ok, failed = [], 0, []
    succeeded = set()
    for p in rooms:
        m = _ROOM.match(os.path.basename(p))
        if not m:
            continue
        cid = m.group(1)
        dst = os.path.join(outdir, cid + ".sqlite")
        dec, good = kd.eva_decrypt_file(p, 3, pragma, uid)
        if not good:
            failed.append(cid)
            if os.path.exists(dst):
                os.remove(dst)
                note(f"backup room {cid}: source no longer decrypts; stale {cid}.sqlite removed")
            continue
        _write(dst, dec)
        succeeded.add(cid)
        ok += 1
        try:
            rows = _rows_of(dst)
        except sqlite3.Error:
            rows = None
        manifest.append({"chatId": cid, "rows": rows, "from": os.path.basename(p), "to": cid + ".sqlite"})

    for name in sorted(os.listdir(outdir)):
        m = _SQLITE.match(name)
        if m and m.group(1) not in succeeded:
            os.remove(os.path.join(outdir, name))
            note(f"backup room {m.group(1)}: room gone from the backup; {name} removed")

    tu = os.path.join(bak, "TalkUserDB.edb")
    if os.path.exists(tu):
        dec, good = kd.eva_decrypt_file(tu, 3, pragma, uid)
        if good:
            _write(os.path.join(outdir, "TalkUserDB.sqlite"), dec)
        else:
            note("backup TalkUserDB.edb did not decrypt (kept going)")

    manifest.sort(key=lambda e: e["chatId"])
    _write(os.path.join(outdir, "_manifest.json"), json.dumps(
        {"decrypted": manifest, "updated": _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")},
        ensure_ascii=True, indent=1).encode("utf-8"))
    note(f"backup {os.path.basename(bak)}: {ok}/{len(rooms)} rooms decrypted -> {outdir}"
         + (f" (failed: {', '.join(failed)})" if failed else ""))
    if rooms and not ok:
        return 3
    return 0
