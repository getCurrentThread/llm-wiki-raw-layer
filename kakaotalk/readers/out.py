# -*- coding: utf-8 -*-
"""merged_out, live_out (kakatok outputs) and the static decrypted room DBs (SPEC 7.1-7.3, 10.2)."""
import json
import os
import re
import sqlite3
from pathlib import Path

from rawlayer.capture import canon, fsio
from rawlayer.capture.model import Container, ContainerError, Obs

from . import dumper
from ..keys import kakao_ts_ms
from ..state import base_for

_DIGITS = re.compile(r"^[0-9]+$")
MSG_NATIVE = ("chatId", "logId", "sendAt", "authorId", "type", "deleted", "message")
MSG_DERIVED = ("sender", "type_name", "date", "source")


def _msg_obs(row, cols, notes, asof_s, captured_col=None):
    d = dict(zip(cols, row))
    cid, lid = canon.pk_text(d["chatId"]), canon.pk_text(d["logId"])
    if cid is None or lid is None or not _DIGITS.fullmatch(cid):
        notes["pk_bad"] += 1
        return None
    native = {k: d[k] for k in MSG_NATIVE}
    derived = {k: d[k] for k in MSG_DERIVED}
    ts, bad = kakao_ts_ms(d["sendAt"])
    if bad:
        notes["ts_out_of_range"] += 1
    src_asof = d[captured_col] if captured_col else asof_s
    if captured_col and not (isinstance(src_asof, int) and not isinstance(src_asof, bool)):
        src_asof = None
    return Obs("msg", f"kakao:{cid}:msg:{lid}", native, derived, ts, src_asof)


def _schema_obs(con, table, key_prefix, asof_s):
    info = dumper.table_info(con, table)
    if not info:
        raise ContainerError("error", "missing_table")
    return Obs("schema", f"{key_prefix}@{table}", dumper.schema_native(table, info), {}, None, asof_s)


def merged_containers(ctx):
    path = ctx.inputs.merged_out
    # the name is persisted (rel, source id): the basename of config.json's own string, as always (Path(...).name
    # would drop a trailing "." part that basename keeps, and Config accepts such a value)
    rel = os.path.basename(ctx.cfg.sources["merged_out"])
    c = Container("merged_out", rel, canon.source_id(rel), asof_s=fsio.mtime_s(path))
    con = fsio.connect_ro(path, immutable=True)
    try:
        c.tables = dumper.table_counts(con)
    except BaseException:
        con.close()
        raise

    def it():
        yield _schema_obs(con, "messages", "kakao:*", c.asof_s)
        yield _schema_obs(con, "rooms", "kakao:*", c.asof_s)
        cur = con.execute("SELECT " + ", ".join(MSG_NATIVE + MSG_DERIVED) + " FROM messages")
        cols = [d[0] for d in cur.description]
        for row in cur:
            ob = _msg_obs(row, cols, c.notes, c.asof_s)
            if ob:
                yield ob
        for cid, title, messages, first, last, sources in con.execute(
                "SELECT chatId, title, messages, first, last, sources FROM rooms"):
            t = canon.pk_text(cid)
            if t is None or not _DIGITS.fullmatch(t):
                c.notes["pk_bad"] += 1
                continue
            yield Obs("room", f"kakao:{t}", {"chatId": cid},
                      {"title": title, "messages": messages, "first": first, "last": last, "sources": sources},
                      None, c.asof_s)

    c.iter_obs = it
    c.cleanup = con.close
    return [c]


def _open_live(ctx):
    path = ctx.inputs.live_out
    if ctx.inputs.live_frozen:
        return fsio.connect_ro(path, immutable=True)
    # Production (SYSTEM): copy into memory in one step and let go of the source at once, so the
    # collector's checkpoints are blocked for the backup only (0.2-2 s), never for the full scan.
    src = fsio.connect_ro(path, immutable=False)
    mem = sqlite3.connect(":memory:")
    try:
        src.backup(mem, pages=-1)
    finally:
        src.close()
    mem.execute("PRAGMA query_only=1")
    mem.execute("PRAGMA temp_store=MEMORY")
    return mem


def live_containers(ctx):
    path = ctx.inputs.live_out
    rel = "live.sqlite"
    c = Container("live_out", rel, canon.source_id(rel), asof_s=fsio.mtime_s(path))
    con = _open_live(ctx)
    qc = con.execute("PRAGMA quick_check").fetchone()[0]
    if qc != "ok":
        con.close()
        c.status = "error"
        c.notes["quick_check"] += 1
        return [c]
    c.tables = dumper.table_counts(con)

    def it():
        yield _schema_obs(con, "messages", "kakao:*", c.asof_s)
        yield _schema_obs(con, "cursors", "kakao:*", c.asof_s)
        cur = con.execute("SELECT " + ", ".join(MSG_NATIVE + MSG_DERIVED + ("captured_at",)) + " FROM messages")
        cols = [d[0] for d in cur.description]
        for row in cur:
            ob = _msg_obs(row, cols, c.notes, c.asof_s, captured_col="captured_at")
            if ob:
                yield ob
        for cid, last_logid, title, count, first_ts, last_ts in con.execute(
                "SELECT chatId, last_logId, title, count, first_ts, last_ts FROM cursors"):
            t = canon.pk_text(cid)
            if t is None or not _DIGITS.fullmatch(t):
                c.notes["pk_bad"] += 1
                continue
            yield Obs("room", f"kakao:{t}", {"chatId": cid},
                      {"last_logId": last_logid, "title": title, "count": count, "first_ts": first_ts, "last_ts": last_ts},
                      None, c.asof_s)

    c.iter_obs = it
    c.cleanup = con.close
    return [c]


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


def static_room_containers(ctx, origin):
    """backup_decrypted / decrypted_live: <chatId>.sqlite, opened mode=ro&immutable=1."""
    root = Path(getattr(ctx.inputs, origin))
    man = _read_manifest(root)
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
        out.append(_room_container(ctx, origin, root, name, chat_id, man.get(chat_id)))
    ctx.origin_notes[origin]["not_room"] += not_room
    # SPEC 8.3: a room file that is gone -> no records, note room_gone (C1 reports it)
    before = {k[len("kakao:"):] for (k,) in ctx.state.execute(
        "SELECT key FROM heads WHERE origin=? AND kind='room' AND op='put'", (origin,))}
    gone = len(before - present)
    if gone:
        ctx.origin_notes[origin]["room_gone"] += gone
    return out


def _room_container(ctx, origin, root, name, chat_id, man_entry):
    path = root / name
    c = Container(origin, name, canon.source_id(name), asof_s=fsio.mtime_s(path))
    state = {"con": None}

    def it():
        con = fsio.connect_ro(path, immutable=True)
        state["con"] = con
        c.tables = dumper.table_counts(con)
        yield Obs("room", f"kakao:{chat_id}", {"chatId": chat_id}, dict(man_entry or {}), None, c.asof_s)
        yield from dumper.iter_room_obs(con, chat_id, base_for(ctx, origin), c.notes, c.asof_s)

    def cleanup():
        if state["con"] is not None:
            state["con"].close()

    c.iter_obs = it
    c.cleanup = cleanup
    return c
