"""Ledger-only Discord FTS5 index and search."""
import json
import re
from collections import Counter
from rawlayer.search import common, search as drv, textindex as ti
from .keys import snowflake_ms

NAME = "discord"
HELP = "search Discord messages and threads"
ORIGIN = "dc_archive"
SCHEMA = """
CREATE TABLE msgs(
 id INTEGER PRIMARY KEY, key TEXT NOT NULL UNIQUE, msg_id TEXT NOT NULL,
 sid INTEGER NOT NULL, rev INTEGER NOT NULL, seq INTEGER NOT NULL, held INTEGER NOT NULL,
 channel_id TEXT NOT NULL, guild_id TEXT NOT NULL DEFAULT '', channel TEXT NOT NULL DEFAULT '',
 guild TEXT NOT NULL DEFAULT '', user_id TEXT NOT NULL, sender TEXT NOT NULL DEFAULT '',
 ts INTEGER NOT NULL, ts_ms INTEGER NOT NULL, reference_id TEXT NOT NULL,
 deleted INTEGER NOT NULL, edited INTEGER NOT NULL, body_from TEXT NOT NULL,
 message TEXT NOT NULL, ng TEXT NOT NULL);
CREATE INDEX msgs_time ON msgs(ts_ms,sid,channel_id);
CREATE INDEX msgs_context ON msgs(channel_id,ts_ms,sid);
CREATE INDEX msgs_reference ON msgs(reference_id);
CREATE TABLE ch_src(channel_id TEXT PRIMARY KEY, rev INTEGER NOT NULL, seq INTEGER NOT NULL,
 held INTEGER NOT NULL, name TEXT NOT NULL, parent_id TEXT NOT NULL, guild_id TEXT NOT NULL,
 ch_type TEXT NOT NULL) WITHOUT ROWID;
CREATE TABLE guild_src(guild_id TEXT PRIMARY KEY, rev INTEGER NOT NULL, seq INTEGER NOT NULL,
 held INTEGER NOT NULL, name TEXT NOT NULL) WITHOUT ROWID;
CREATE TABLE users(user_id TEXT PRIMARY KEY, rev INTEGER NOT NULL, seq INTEGER NOT NULL,
 held INTEGER NOT NULL, name TEXT NOT NULL, nicks TEXT NOT NULL) WITHOUT ROWID;
CREATE TABLE channels(channel_id TEXT PRIMARY KEY, label TEXT NOT NULL, guild_id TEXT NOT NULL,
 guild TEXT NOT NULL, parent_id TEXT NOT NULL, n INTEGER NOT NULL, senders INTEGER NOT NULL,
 n_deleted INTEGER NOT NULL) WITHOUT ROWID;
"""
_LABEL = """channel=coalesce((SELECT CASE WHEN p.name IS NOT NULL THEN p.name || ' › ' || c.name
 ELSE c.name END FROM ch_src c LEFT JOIN ch_src p ON p.channel_id=c.parent_id
 WHERE c.channel_id=msgs.channel_id),''),
 guild_id=coalesce((SELECT guild_id FROM ch_src WHERE channel_id=msgs.channel_id),''),
 guild=coalesce((SELECT g.name FROM guild_src g JOIN ch_src c ON c.guild_id=g.guild_id
 WHERE c.channel_id=msgs.channel_id),''),
 sender=coalesce((SELECT name FROM users WHERE user_id=msgs.user_id),'')"""


def _s(v):
    return common.clean(v) if isinstance(v, str) else ""


def _texts(value):
    if isinstance(value, list):
        return [t for v in value for t in _texts(v)]
    if isinstance(value, dict):
        return [t for k, v in value.items() if k != "url" for t in _texts(v)]
    return [value.strip()] if isinstance(value, str) and value.strip() else []


def msg_text(native, counts=None):
    text = native.get("content")
    if isinstance(text, str) and text.strip():
        return _s(text.strip()), "content"
    if text is not None and not isinstance(text, str) and counts is not None:
        counts["body_other"] += 1
    embeds = _texts(native.get("embeds"))
    if embeds:
        return _s("\n".join(embeds)), "embeds"
    names = native.get("attachments")
    names = [_s(v.get("fileName")) for v in names if isinstance(v, dict)] if isinstance(names, list) else []
    forward = _texts(native.get("forwardedMessage"))
    parts = [n for n in names if n] + forward
    return (_s("\n".join(parts)), "files" if names else "forwarded") if parts else ("", "")


class DiscordApplier:
    def __init__(self, con, full):
        self.con, self.full = con, full
        self.counts = Counter()
        self.dirty = False

    def record(self, rec, seq):
        if not isinstance(rec, dict) or rec.get("origin") != ORIGIN or rec.get("op") not in ("put", "absent", "purged") \
                or not common.is_int(rec.get("rev")) or not 0 < rec["rev"] < 2 ** 63:
            self.counts["bad_record"] += 1
            return
        key, kind, rev, op = rec.get("key"), rec.get("kind"), rec["rev"], rec["op"]
        match = re.fullmatch(r"dc:(guild|ch|user):([^:]+)|dc:(msg):([^:]+):([^:]+)", key or "") if isinstance(key, str) else None
        if not match or kind != (match[1] or match[3]):
            self.counts["bad_key"] += 1
            return
        native = rec.get("native")
        if op == "put" and not isinstance(native, dict):
            self.counts["bad_native"] += 1
            return
        n = native if isinstance(native, dict) else {}
        held = int(op == "put")
        if kind == "msg":
            cid, mid = match[4], match[5]
            ts_ms = snowflake_ms(mid)
            if ts_ms is None:
                self.counts["ts_unknown"] += 1
                return
            cur = self.con.execute("SELECT rev,message,body_from,edited FROM msgs WHERE key=?", (key,)).fetchone()
            if cur and rev <= cur[0]:
                self.counts["stale_rev"] += 1
                return
            if op != "put":
                self.con.execute("UPDATE msgs SET rev=?,seq=?,held=0,deleted=1 WHERE key=?", (rev, seq, key))
                if op == "purged":
                    self.con.execute("UPDATE msgs SET message='',ng='',body_from='' WHERE key=?", (key,))
            else:
                text, body = msg_text(n, self.counts)
                edited = bool(n.get("timestampEdited")) or bool(cur and (cur[3] or (text and cur[1] and text != cur[1])))
                if not text and cur:
                    text, body = cur[1:3]
                author, ref = n.get("author"), n.get("reference")
                uid = _s(author.get("id")) if isinstance(author, dict) else ""
                ref = _s(ref.get("messageId")) if isinstance(ref, dict) else ""
                vals = (key, mid, int(mid), rev, seq, held, cid, uid, ts_ms // 1000, ts_ms,
                        ref, int(bool(n.get("_removedAt"))), int(edited), body, text, ti.ng_text(text))
                self.con.execute("""INSERT INTO msgs(key,msg_id,sid,rev,seq,held,channel_id,user_id,ts,ts_ms,
                  reference_id,deleted,edited,body_from,message,ng) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                  ON CONFLICT(key) DO UPDATE SET rev=excluded.rev,seq=excluded.seq,held=excluded.held,
                  user_id=excluded.user_id,reference_id=excluded.reference_id,deleted=excluded.deleted,
                  edited=excluded.edited,body_from=excluded.body_from,message=excluded.message,ng=excluded.ng""", vals)
        else:
            ident = match[2]
            table, column = {"ch": ("ch_src", "channel_id"), "guild": ("guild_src", "guild_id"), "user": ("users", "user_id")}[kind]
            cur = self.con.execute(f"SELECT rev FROM {table} WHERE {column}=?", (ident,)).fetchone()
            if cur and rev <= cur[0]:
                self.counts["stale_rev"] += 1
                return
            if op != "put":
                self.con.execute(f"UPDATE {table} SET rev=?,seq=?,held=0 WHERE {column}=?", (rev, seq, ident))
                if op == "purged":
                    self.con.execute(f"UPDATE {table} SET name='' WHERE {column}=?", (ident,))
            elif kind == "ch":
                self.con.execute("INSERT OR REPLACE INTO ch_src VALUES(?,?,?,?,?,?,?,?)",
                                 (ident, rev, seq, held, _s(n.get("name")), _s(n.get("parent_id")),
                                  _s(n.get("guild_id")), _s(n.get("type"))))
            elif kind == "guild":
                self.con.execute("INSERT OR REPLACE INTO guild_src VALUES(?,?,?,?,?)", (ident, rev, seq, held, _s(n.get("name"))))
            else:
                self.con.execute("INSERT OR REPLACE INTO users VALUES(?,?,?,?,?,?)",
                                 (ident, rev, seq, held, _s(n.get("name")), json.dumps(n.get("nicks", {}))))
        self.dirty = True

    def end_manifest(self, seq):
        common.bump_counts(self.con, self.counts)
        self.counts.clear()
        if not self.full and self.dirty:
            self.finish()
        self.dirty = False

    def finish(self):
        con = self.con
        con.execute("UPDATE msgs SET " + _LABEL)
        con.execute("DELETE FROM channels")
        con.execute("""INSERT INTO channels SELECT c.channel_id,
         CASE WHEN p.name IS NOT NULL THEN p.name || ' › ' || c.name ELSE c.name END,
         c.guild_id,coalesce(g.name,''),c.parent_id,
         (SELECT count(*) FROM msgs m WHERE m.channel_id=c.channel_id AND deleted=0),
         (SELECT count(DISTINCT user_id) FROM msgs m WHERE m.channel_id=c.channel_id AND deleted=0),
         (SELECT count(*) FROM msgs m WHERE m.channel_id=c.channel_id AND deleted=1)
         FROM ch_src c LEFT JOIN ch_src p ON p.channel_id=c.parent_id LEFT JOIN guild_src g ON g.guild_id=c.guild_id""")


class DiscordIndex(common.SourceIndex):
    name = NAME
    origins = frozenset({ORIGIN})
    schema_version = extractor_version = "discord-1"
    fts_tables = ("msgs_fts",)

    def create_tables(self, con):
        con.executescript(SCHEMA)
        con.execute(ti.fts_ddl("msgs"))

    def create_triggers(self, con):
        common.create_triggers(con, "msgs", "msgs_fts")

    def applier(self, con, full):
        return DiscordApplier(con, full)

    def stats(self, con):
        return {"rows": con.execute("SELECT count(*) FROM msgs").fetchone()[0],
                "deleted": con.execute("SELECT coalesce(sum(deleted),0) FROM msgs").fetchone()[0],
                **common.read_counts(con)}


SPEC = DiscordIndex()


def add_arguments(p):
    common.add_search_arguments(p, ["hits", "count", "timeline", "senders", "channels"])
    p.add_argument("--channel", action="append", default=[])
    p.add_argument("--guild")
    p.add_argument("--thread", metavar="MSG_ID")
    p.add_argument("--list-channels", action="store_true")
    p.add_argument("--before")
    p.add_argument("--after")


class Filters(drv.Filters):
    range_col, range_scale, until_pad = "ts_ms", 1000, 999

    def __init__(self, a, tz):
        super().__init__(a, tz)
        self.selector |= bool(a.channel or a.guild)
        for values, label, ident in ((a.channel, "channel", "channel_id"),
                                     ([a.guild] if a.guild else [], "guild", "guild_id"),
                                     ([a.sender] if a.sender else [], "sender", "user_id")):
            if values:
                ors = []
                for value in values:
                    sql, ps = common.like("t." + label, value)
                    if label == "sender":
                        nick, ns = common.like("nicks", value)
                        ors.append(f"({sql} OR t.{ident}=? OR t.user_id IN (SELECT user_id FROM users WHERE {nick}))")
                        self.params += ps + [value] + ns
                    else:
                        ors.append(f"({sql} OR t.{ident}=?)")
                        self.params += ps + [value]
                self.where.append("(" + " OR ".join(ors) + ")")

    def summary(self, a):
        return {**super().summary(a), "channel": a.channel, "guild": a.guild}


class DiscordSearch(drv.SearchDriver):
    name, table, fts_table = NAME, "msgs", "msgs_fts"
    # Channel disambiguates duplicated parent/thread starter snowflakes for paging.
    sort_cols = ("ts_ms", "sid", "channel_id")
    group_modes = {"senders": "t.user_id", "channels": "t.channel_id"}
    list_flag, in_page_key, page_group = "list_channels", "channels_in_page", "channel_id"
    hits_noun, browse_hint, Filters = "messages", "give a query or --channel/--guild/--sender/--since/--until", Filters
    key_col, container_col, container_src, chunk_gap_s = "key", "channel_id", "ch_src", 1800

    def chunk_units(self, con, container):
        rows = con.execute("SELECT key, ts, guild, channel, sender, user_id, message FROM msgs WHERE channel_id=? "
                           "AND deleted=0 AND message!='' ORDER BY ts_ms, sid", (container,)).fetchall()
        label = " › ".join(p for p in (rows[-1]["guild"], rows[-1]["channel"] or container) if p) if rows else ""
        return label, [(r["key"], r["ts"], f"{r['sender'] or r['user_id']}: {r['message']}", False) for r in rows]

    def check_args(self, a):
        if a.thread is not None:
            if a.list_channels or snowflake_ms(a.thread) is None:
                raise common.UsageError("--thread needs a snowflake and cannot be combined with --list-channels")
            return "thread"

    def parse_cursor(self, value, flag, before=True):
        parts = (value or "").split(".")
        if len(parts) != 3 or not all(len(p) <= 19 and p.isascii() and p.isdecimal() for p in parts):
            raise common.UsageError(f"{flag}: expected ts_ms.snowflake.channel_id")
        if any(int(p) >= 2 ** 63 for p in parts[:2]):
            raise common.UsageError(f"{flag}: cursor out of range")
        return int(parts[0]), int(parts[1]), parts[2]

    def hit(self, r, tz, marks, width):
        return {k: r[k] for k in ("id", "key", "msg_id", "channel_id", "channel", "guild_id", "guild", "user_id", "sender", "ts", "ts_ms", "body_from")} | {
            "deleted": bool(r["deleted"]), "edited": bool(r["edited"]), "reference_id": r["reference_id"],
            "text": r["message"], "snippet": ti.highlight(r["message"], marks, width),
            "date": common.fmt_ts(r["ts"], tz), "cursor": self.cursor(r)}

    def context(self, con, r, n, a, tz):
        hide = "" if a.include_deleted else " AND deleted=0"
        args = (r["channel_id"], r["ts_ms"], r["sid"], n)
        before = con.execute("SELECT * FROM msgs WHERE channel_id=? AND (ts_ms,sid)<(?,?)" + hide +
                             " ORDER BY ts_ms DESC,sid DESC LIMIT ?", args).fetchall()
        after = con.execute("SELECT * FROM msgs WHERE channel_id=? AND (ts_ms,sid)>(?,?)" + hide +
                            " ORDER BY ts_ms,sid LIMIT ?", args).fetchall()
        return [{**self.hit(c, tz, [], a.width), "hit": c["id"] == r["id"]} for c in [*reversed(before), r, *after]]

    def who(self, h):
        return h.get("sender") or h.get("user_id") or "?"

    def flags(self, h):
        return " [deleted]" if h.get("deleted") else ""

    def group_row(self, con, r, mode, tz):
        table, column, label = ("users", "user_id", "name") if mode == "senders" else ("channels", "channel_id", "label")
        row = con.execute(f"SELECT {label} FROM {table} WHERE {column}=?", (r["gid"],)).fetchone()
        return {column: r["gid"], "label": (row[0] if row else "") or r["gid"], "n": r["n"],
                "first": common.fmt_ts(r["f"], tz, "%Y-%m-%d") if r["f"] else "",
                "last": common.fmt_ts(r["l"], tz, "%Y-%m-%d") if r["l"] else ""}

    def group_label(self, r, mode):
        col = "user_id" if mode == "senders" else "channel_id"
        return r["label"], col + "=" + r[col]

    def list_all(self, con, a, tz):
        where, params = [], []
        if a.channel:
            ors = []
            for value in a.channel:
                sql, ps = common.like("label", value)
                ors.append(f"({sql} OR channel_id=?)")
                params += ps + [value]
            where.append("(" + " OR ".join(ors) + ")")
        if a.guild:
            sql, ps = common.like("guild", a.guild)
            where.append(f"({sql} OR guild_id=?)")
            params += ps + [a.guild]
        clause = " WHERE " + " AND ".join(where) if where else ""
        rows = con.execute("SELECT * FROM channels" + clause + " ORDER BY n DESC,channel_id LIMIT ?", params + [a.limit]).fetchall()
        return {"mode": "list", "total": con.execute("SELECT count(*) FROM channels" + clause, params).fetchone()[0],
                "rows": [dict(r) for r in rows]}

    def extra_result(self, con, a, extra, query, tz):
        mid = a.thread
        seed = con.execute("SELECT * FROM msgs WHERE msg_id=? ORDER BY channel_id LIMIT 1", (mid,)).fetchone()
        if seed is None:
            raise common.UsageError("message not in Discord index")
        channel = con.execute("SELECT parent_id FROM ch_src WHERE channel_id=?", (seed["channel_id"],)).fetchone()
        root = seed["channel_id"] if channel and channel[0] else seed["reference_id"] or mid
        seen = {mid}
        while root not in seen:
            seen.add(root)
            parent = con.execute("SELECT reference_id FROM msgs WHERE msg_id=? ORDER BY channel_id LIMIT 1", (root,)).fetchone()
            if not parent or not parent[0]:
                break
            root = parent[0]
        hide = "" if a.include_deleted else " AND deleted=0"
        rows = con.execute("SELECT * FROM msgs WHERE (msg_id=? OR channel_id=? OR reference_id=?)" + hide +
                           " ORDER BY ts_ms,sid,channel_id", (root, root, root)).fetchall()
        if query or a.channel or a.guild or a.sender or a.since or a.until or a.before or a.after or a.mode != "hits":
            common.note("query and selection filters are not applied to --thread")
        return {"mode": "thread", "root_id": root, "total": len(rows), "rows": [self.hit(r, tz, [], a.width) for r in rows]}

    def render_rest(self, res, a, tz):
        print(f"{NAME}: {res['total']} {res['mode']} result(s)")
        for row in res["rows"]:
            print(json.dumps(row, ensure_ascii=False))


DRIVER = DiscordSearch(SPEC)


def run(a):
    return DRIVER.run(a)
