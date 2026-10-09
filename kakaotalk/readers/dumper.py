# -*- coding: utf-8 -*-
"""Generic room-DB dumper shared by backup_decrypted, decrypted_live and edb (SPEC 7.3)."""
import hashlib

from rawlayer.capture import canon
from rawlayer.capture.model import ContainerError, Obs

from ..keys import kakao_ts_ms

TABLE_KIND = {"chatLogs": "msg", "chatLogHistory": "hist", "chatLogMeta": "meta", "chatLog_attachment": "attachment"}
KNOWN_TABLES = frozenset((
    "chatLogs", "chatLogHistory", "chatLogMeta", "chatLog_attachment", "chatLogDrafts", "chatLogOutdated",
    "chatLogTagMapping", "chatLogTagRevision", "chatLogMetaPendingRestoreRanges", "messages", "rooms", "cursors",
    "NTChatMessage", "NTChatRoom", "NTChatMeta", "NTUser", "NTOpenLink"))     # the macOS client's (macdb)


def qi(name):
    return '"' + name.replace('"', '""') + '"'


def table_label(name):
    return name if name in KNOWN_TABLES else "t_" + hashlib.sha256(name.encode("utf-8")).hexdigest()[:8]


def list_tables(con):
    return [r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND substr(name,1,7) <> 'sqlite_' ORDER BY name")]


def table_info(con, table):
    return [tuple(r) for r in con.execute(f"PRAGMA table_info({qi(table)})")]


def schema_native(table, info):
    return {"table": table, "columns": [[name, typ, notnull, dflt, pk] for _cid, name, typ, notnull, dflt, pk in info]}


def table_counts(con):
    out = {}
    for t in list_tables(con):
        out[table_label(t)] = con.execute(f"SELECT COUNT(*) FROM {qi(t)}").fetchone()[0]
    return out


def make_row_native(cols, row, base, defaults):
    """Column dict minus later-added columns whose value equals their declared default (SPEC 8.4)."""
    out = {}
    for c, v in zip(cols, row):
        if base is not None and c not in base and canon.default_equal(v, defaults.get(c)):
            continue
        out[c] = v
    return out


def iter_room_obs(con, chat_id, base_for, notes, asof_s, check_counts=True):
    """Yield schema + row observations of one room DB. `base_for(schema_key)` returns the rev-1 column
    names (or None). Raises ContainerError('error'|'unreadable', ...) when COUNT(*) disagrees."""
    tables = list_tables(con)
    for t in tables:
        if t not in TABLE_KIND:
            continue
        info = table_info(con, t)
        skey = f"kakao:{chat_id}@{t}"
        yield Obs("schema", skey, schema_native(t, info), {}, None, asof_s)
    for t in tables:
        kind = TABLE_KIND.get(t)
        if kind is None:
            continue
        info = table_info(con, t)
        pk = [name for _cid, name, _ty, _nn, _d, p in sorted(info, key=lambda r: r[5]) if p > 0]
        if not pk:
            notes["no_pk"] += 1
            continue
        defaults = {name: d for _cid, name, _ty, _nn, d, _p in info}
        base = base_for(f"kakao:{chat_id}@{t}")
        base = set(base) if base is not None else None
        expected = con.execute(f"SELECT COUNT(*) FROM {qi(t)}").fetchone()[0]
        cur = con.execute(f"SELECT * FROM {qi(t)} ORDER BY " + ", ".join(qi(c) for c in pk))
        cols = [d[0] for d in cur.description]
        idx = [cols.index(c) for c in pk]
        seen = 0
        for row in cur:
            seen += 1
            parts = [canon.pk_text(row[i]) for i in idx]
            if any(p is None for p in parts):
                notes["pk_bad"] += 1
                continue
            key = f"kakao:{chat_id}:{kind}:" + ":".join(parts)
            raw = dict(zip(cols, row))
            native = make_row_native(cols, row, base, defaults)
            ts = None
            if kind == "msg":
                ts, bad = kakao_ts_ms(raw.get("sendAt"))
                if bad:
                    notes["ts_out_of_range"] += 1
            yield Obs(kind, key, native, {}, ts, asof_s)
        if check_counts and seen != expected:
            raise ContainerError("count_mismatch", "count_mismatch")
