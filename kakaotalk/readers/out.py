# -*- coding: utf-8 -*-
"""backup_decrypted: the decrypted room DBs setup's backup step writes (<chatId>.sqlite beside the
_manifest.json), the room-DB input the app reads as static files (SPEC 7.3, 10.2).

The manifest entries arrive as each room record's derived fields (rows, from, to and, when the backup's own
chat-list database named the room, title). The sibling TalkUserDB.sqlite (the backup step writes it too)
labels the rooms' senders (the accounts' display names); a message whose author it does not name carries
only the authorId, never an error."""
import json
import re
import sqlite3
from pathlib import Path

from rawlayer.capture import canon, fsio
from rawlayer.capture.model import Container, Obs

from . import dumper
from .. import roomlist
from ..state import base_for

_DIGITS = re.compile(r"^[0-9]+$")


def _read_manifest(root):
    p = root / "_manifest.json"
    try:
        d = json.loads(fsio.read_shared(p))
    except (OSError, ValueError):
        return {}
    out = {}
    for e in d.get("decrypted", []) or []:
        if isinstance(e, dict) and "chatId" in e:
            out[str(e["chatId"])] = {k: v for k, v in e.items() if k != "chatId"}
    return out


def _names_of(root):
    """{str(authorId): name} from the backup's decrypted user database, or None when there is none."""
    p = Path(root) / "TalkUserDB.sqlite"
    if not p.exists():
        return None
    try:
        con = fsio.connect_ro(p, immutable=True)
        try:
            return roomlist.user_names(con)
        finally:
            con.close()
    except (sqlite3.Error, OSError):
        return None


def backup_containers(ctx):
    root = Path(ctx.inputs.backup_decrypted)
    man = _read_manifest(root)
    names = _names_of(root)
    out = []
    not_room = 0
    present = set()
    for name in sorted(p.name for p in root.iterdir()):
        if not name.endswith(".sqlite"):
            continue
        chat_id = name[:-len(".sqlite")]
        if not _DIGITS.fullmatch(chat_id):
            not_room += 1
            continue
        present.add(chat_id)
        out.append(_room_container(ctx, root, name, chat_id, man.get(chat_id), names))
    ctx.origin_notes["backup_decrypted"]["not_room"] += not_room
    # SPEC 8.3: a room file that is gone -> no records, note room_gone (C1 reports it)
    before = {k[len("kakao:"):] for (k,) in ctx.state.execute(
        "SELECT key FROM heads WHERE origin='backup_decrypted' AND kind='room' AND op='put'")}
    gone = len(before - present)
    if gone:
        ctx.origin_notes["backup_decrypted"]["room_gone"] += gone
    return out


def _room_container(ctx, root, name, chat_id, man_entry, names=None):
    path = root / name
    c = Container("backup_decrypted", name, canon.source_id(name), asof_s=fsio.mtime_s(path))
    state = {"con": None}

    def it():
        con = fsio.connect_ro(path, immutable=True)
        state["con"] = con
        c.tables = dumper.table_counts(con)
        yield Obs("room", f"kakao:{chat_id}", {"chatId": chat_id}, dict(man_entry or {}), None, c.asof_s)
        yield from dumper.iter_room_obs(con, chat_id, base_for(ctx, "backup_decrypted"), c.notes, c.asof_s,
                                        names=names)

    def cleanup():
        if state["con"] is not None:
            state["con"].close()

    c.iter_obs = it
    c.cleanup = cleanup
    return c
