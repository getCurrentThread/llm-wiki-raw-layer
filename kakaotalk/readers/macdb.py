# -*- coding: utf-8 -*-
"""macdb (the macOS client's database, kakaocli port): one decrypted NT-schema SQLite — the file the macdb
command writes to the mac_db option path, opened read-only and immutable.

NTChatMessage carries logId, chatId, authorId, message, type and the timestamp under the name `sentAt`
(the client's spelling; the kakatok producers and the app's value rules use `sendAt`) — the reader maps
that one name for the timestamp, the projection hash and nothing else, so message keys stay
kakao:<chatId>:msg:<logId> and the hash matches: a message seen on both a PC and a Mac is one row across
origins. A database without NTChatMessage (a placeholder, a non-KakaoTalk file) is noted not_mac and reads
nothing; on a real Mac database that lost its message table the origin reads nothing rather than erroring,
the same way a room-DB file that is not a room counts as not_room."""
import os
import re
import sqlite3
from pathlib import Path

from rawlayer.capture import canon, fsio
from rawlayer.capture.model import Container, Obs

from . import dumper
from ..keys import kakao_ts_ms
from ..state import base_for

NT_TABLES = ("NTChatMessage", "NTChatRoom", "NTChatMeta", "NTUser", "NTOpenLink")
_DIGITS = re.compile(r"^[0-9]+$")


def _user_names(con, tables):
    """{userId: name} from NTUser (linkId 0): displayName, else friendNickName, else nickName."""
    if "NTUser" not in tables:
        return {}
    cols = {r[1] for r in con.execute("PRAGMA table_info(NTUser)")}
    if "userId" not in cols:
        return {}
    name_cols = [c for c in ("displayName", "friendNickName", "nickName") if c in cols]
    if not name_cols:
        return {}
    link = "WHERE linkId = 0" if "linkId" in cols else ""
    out = {}
    for row in con.execute(f"SELECT userId, {', '.join(name_cols)} FROM NTUser {link}"):
        nm = next((str(v).strip() for v in row[1:] if isinstance(v, str) and v.strip()), None)
        if nm is not None:
            out[row[0]] = nm
    return out


def _titles(con, tables, names):
    """{chatId(str): title} for NTChatRoom, named the way the client names rooms (kakaocli's fallback chain:
    chatName, NTChatMeta type 3, NTOpenLink linkName, the direct-chat partner)."""
    if "NTChatRoom" not in tables:
        return {}
    cols = {r[1] for r in con.execute("PRAGMA table_info(NTChatRoom)")}
    if "chatId" not in cols:
        return {}
    meta = {}
    if "NTChatMeta" in tables:
        mcols = {r[1] for r in con.execute("PRAGMA table_info(NTChatMeta)")}
        if {"chatId", "content"}.issubset(mcols):
            for cid, content in con.execute("SELECT chatId, content FROM NTChatMeta WHERE type = 3"):
                if isinstance(content, str) and content.strip():
                    meta[cid] = content
    links = {}
    if "NTOpenLink" in tables:
        lcols = {r[1] for r in con.execute("PRAGMA table_info(NTOpenLink)")}
        if {"linkId", "linkName"}.issubset(lcols):
            for lid, nm in con.execute("SELECT linkId, linkName FROM NTOpenLink"):
                if isinstance(nm, str) and nm.strip():
                    links[lid] = nm
    out = {}
    cols = [r[1] for r in con.execute("PRAGMA table_info(NTChatRoom)")]
    for row in con.execute("SELECT * FROM NTChatRoom"):
        raw = dict(zip(cols, row))
        cid = canon.pk_text(raw.get("chatId"))
        if cid is None:
            continue
        for cand in (raw.get("chatName"), meta.get(raw.get("chatId")),
                     links.get(raw.get("linkId")), names.get(raw.get("directChatMemberUserId"))):
            if isinstance(cand, str) and cand.strip():
                out[cid] = cand
                break
    return out


def macdb_containers(ctx):
    path = Path(ctx.inputs.mac_db)
    # the name is persisted (rel, source id): the basename of the config's own string, as always
    rel = os.path.basename(ctx.cfg.mac_db)
    c = Container("macdb", rel, canon.source_id(rel), asof_s=fsio.mtime_s(path))
    con = fsio.connect_ro(path, immutable=True)
    try:
        c.tables = dumper.table_counts(con)
    except sqlite3.Error:
        con.close()
        c.status = "error"
        c.notes["not_mac"] += 1            # not an SQLite database at all (a placeholder written wrong)
        return [c]
    tables = set(dumper.list_tables(con))

    def it():
        if "NTChatMessage" not in tables:
            c.notes["not_mac"] += 1
            return
        names = _user_names(con, tables)
        base = base_for(ctx, "macdb")
        for t in NT_TABLES:                # schema first, like every origin
            if t in tables:
                yield Obs("schema", f"kakao:*@{t}", dumper.schema_native(t, dumper.table_info(con, t)),
                          {}, None, c.asof_s)
        info = dumper.table_info(con, "NTChatMessage")
        cols = [r[1] for r in info]
        defaults = {r[1]: r[4] for r in info}
        order = " ORDER BY logId" if "logId" in cols else ""
        for row in con.execute(f'SELECT * FROM "NTChatMessage"{order}'):
            raw = dict(zip(cols, row))
            cid, lid = canon.pk_text(raw.get("chatId")), canon.pk_text(raw.get("logId"))
            if cid is None or lid is None or not _DIGITS.fullmatch(cid) or not _DIGITS.fullmatch(lid):
                c.notes["pk_bad"] += 1
                continue
            native = dumper.make_row_native(cols, row, base("kakao:*@NTChatMessage"), defaults)
            send_at = raw.get("sendAt", raw.get("sentAt"))   # the client's `sentAt` is the app's `sendAt`
            ts, bad = kakao_ts_ms(send_at)
            if bad:
                c.notes["ts_out_of_range"] += 1
            sender = names.get(raw.get("authorId"))
            yield Obs("msg", f"kakao:{cid}:msg:{lid}", native,
                      {"sender": sender} if sender is not None else {}, ts, c.asof_s)
        for cid, title in sorted(_titles(con, tables, names).items()):
            yield Obs("room", f"kakao:{cid}", {"chatId": cid}, {"title": title}, None, c.asof_s)

    c.iter_obs = it
    c.cleanup = con.close
    return [c]
