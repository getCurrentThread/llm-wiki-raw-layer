"""Block-level Notion search. Derived labels/order/deletion use one SQL expression in both build modes."""
import json
import re
from collections import Counter
from rawlayer.search import common, search as drv, textindex as ti
from .keys import nt_id, iso_ms

NAME = "notion"
HELP = "search Notion blocks, page titles/properties and comments"
ORIGIN = "nt_archive"
SCHEMA = """
CREATE TABLE nrows(
 id INTEGER PRIMARY KEY, key TEXT NOT NULL UNIQUE, kind TEXT NOT NULL, native_id TEXT NOT NULL,
 page_id TEXT NOT NULL, rev INTEGER NOT NULL, seq INTEGER NOT NULL, held INTEGER NOT NULL,
 ts INTEGER NOT NULL, ts_ms INTEGER NOT NULL, user_id TEXT NOT NULL,
 sender TEXT NOT NULL DEFAULT '', page_title TEXT NOT NULL DEFAULT '',
 own_deleted INTEGER NOT NULL, deleted INTEGER NOT NULL DEFAULT 0,
 ord INTEGER, depth INTEGER, message TEXT NOT NULL, ng TEXT NOT NULL);
CREATE INDEX nrows_page ON nrows(page_id,ord);
CREATE INDEX nrows_time ON nrows(ts_ms,key);
CREATE TABLE page_src(page_id TEXT PRIMARY KEY, rev INTEGER NOT NULL, seq INTEGER NOT NULL,
 held INTEGER NOT NULL, title TEXT NOT NULL, parent_id TEXT NOT NULL, deleted INTEGER NOT NULL) WITHOUT ROWID;
CREATE TABLE page_order(page_id TEXT NOT NULL, block_id TEXT NOT NULL, ord INTEGER NOT NULL,
 depth INTEGER NOT NULL, parent_id TEXT, PRIMARY KEY(page_id,block_id)) WITHOUT ROWID;
CREATE TABLE users(user_id TEXT PRIMARY KEY, rev INTEGER NOT NULL, seq INTEGER NOT NULL,
 held INTEGER NOT NULL, name TEXT NOT NULL) WITHOUT ROWID;
CREATE TABLE ds_src(ds_id TEXT PRIMARY KEY, rev INTEGER NOT NULL, seq INTEGER NOT NULL,
 held INTEGER NOT NULL, title TEXT NOT NULL, parent_id TEXT NOT NULL) WITHOUT ROWID;
CREATE TABLE pages(page_id TEXT PRIMARY KEY, title TEXT NOT NULL, parent_id TEXT NOT NULL,
 deleted INTEGER NOT NULL, n INTEGER NOT NULL, senders INTEGER NOT NULL) WITHOUT ROWID;
"""
_LABELS = """sender=coalesce((SELECT name FROM users WHERE user_id=nrows.user_id),''),
 page_title=coalesce((SELECT title FROM page_src WHERE page_id=nrows.page_id),''),
 ord=CASE WHEN kind='page' THEN -1 ELSE (SELECT ord FROM page_order o
 WHERE o.page_id=nrows.page_id AND o.block_id=nrows.native_id) END,
 depth=CASE WHEN kind='page' THEN 0 ELSE (SELECT depth FROM page_order o
 WHERE o.page_id=nrows.page_id AND o.block_id=nrows.native_id) END,
 deleted=(own_deleted OR coalesce((SELECT deleted FROM page_src p WHERE p.page_id=nrows.page_id),0)
 OR (kind='block' AND NOT EXISTS(SELECT 1 FROM page_order o
 WHERE o.page_id=nrows.page_id AND o.block_id=nrows.native_id)))"""


def _s(value):
    return common.clean(value) if isinstance(value, str) else ""


def rich_text(value):
    if not isinstance(value, list):
        return ""
    out = []
    for v in value:
        if not isinstance(v, dict):
            continue
        text = v.get("text")
        plain = v.get("plain_text")
        if not isinstance(plain, str) and isinstance(text, dict):
            plain = text.get("content")
        if not isinstance(plain, str) and v.get("type") == "equation":
            eq = v.get("equation")
            plain = eq.get("expression") if isinstance(eq, dict) else ""
        out.append(_s(plain))
    return "".join(out)


def page_title(native):
    props = native.get("properties")
    for value in props.values() if isinstance(props, dict) else ():
        if isinstance(value, dict) and value.get("type") == "title":
            return rich_text(value.get("title"))
    return rich_text(native.get("title"))


def _property(value):
    if not isinstance(value, dict):
        return ""
    typ = value.get("type")
    if typ in ("formula", "rollup"):
        return ""
    v = value.get(typ) if isinstance(typ, str) else None
    if typ in ("title", "rich_text"):
        return rich_text(v)
    if typ in ("select", "status") and isinstance(v, dict):
        return _s(v.get("name"))
    if typ in ("multi_select", "people", "files", "relation") and isinstance(v, list):
        return " ".join(_s(x.get("name") or x.get("id")) for x in v if isinstance(x, dict))
    if typ == "date" and isinstance(v, dict):
        return " ".join(_s(v.get(k)) for k in ("start", "end"))
    return _s(v) if isinstance(v, str) else str(v) if isinstance(v, (int, float, bool)) else ""


def row_text(kind, native, counts=None):
    if kind == "page":
        props = native.get("properties")
        parts = [page_title(native)]
        for name, value in props.items() if isinstance(props, dict) else ():
            if isinstance(value, dict) and value.get("type") != "title":
                text = _property(value)
                if text:
                    parts.append(_s(name) + ": " + text)
        return "\n".join(p for p in parts if p)
    if kind == "comment":
        return rich_text(native.get("rich_text"))
    typ = native.get("type")
    value = native.get(typ) if isinstance(typ, str) else None
    if not isinstance(value, dict):
        if counts is not None:
            counts["body_other"] += 1
        return ""
    parts = [rich_text(value.get("rich_text")), rich_text(value.get("caption"))]
    if typ == "table_row" and isinstance(value.get("cells"), list):
        parts.append(" | ".join(rich_text(v) for v in value["cells"]))
    for field in ("title", "name", "expression", "url"):
        if isinstance(value.get(field), str):
            parts.append(_s(value[field]))
    return "\n".join(p for p in parts if p)


def parent_id(native):
    parent = native.get("parent")
    if not isinstance(parent, dict):
        return ""
    typ = parent.get("type")
    return nt_id(parent.get(typ)) or "" if isinstance(typ, str) else ""


class NotionApplier:
    def __init__(self, con, full):
        self.con, self.full = con, full
        self.counts, self.touched = Counter(), set()

    def record(self, rec, seq):
        if not isinstance(rec, dict) or rec.get("origin") != ORIGIN or rec.get("op") not in ("put", "absent", "purged") \
                or not common.is_int(rec.get("rev")) or not 0 < rec["rev"] < 2 ** 63:
            self.counts["bad_record"] += 1
            return
        key, kind, rev, op = rec.get("key"), rec.get("kind"), rec["rev"], rec["op"]
        parts = key.split(":") if isinstance(key, str) else []
        if len(parts) not in (3, 4) or parts[0] != "nt" or parts[1] != kind \
                or kind not in ("page", "block", "comment", "user", "ds") \
                or len(parts) != (4 if kind in ("block", "comment") else 3) \
                or any(nt_id(v) != v for v in parts[2:]):
            self.counts["bad_key"] += 1
            return
        native = rec.get("native")
        if op == "put" and not isinstance(native, dict):
            self.counts["bad_native"] += 1
            return
        n = native if isinstance(native, dict) else {}
        ident, held = parts[-1], int(op == "put")
        pid = parts[2] if kind in ("page", "block", "comment") else None
        if kind in ("page", "user", "ds"):
            table, column = {"page": ("page_src", "page_id"), "user": ("users", "user_id"), "ds": ("ds_src", "ds_id")}[kind]
            cur = self.con.execute(f"SELECT rev FROM {table} WHERE {column}=?", (ident,)).fetchone()
            if cur and rev <= cur[0]:
                self.counts["stale_rev"] += 1
                return
            if kind == "page":
                if op == "put":
                    self.con.execute("INSERT OR REPLACE INTO page_src VALUES(?,?,?,?,?,?,?)",
                                     (ident, rev, seq, held, page_title(n), parent_id(n),
                                      int(bool(n.get("in_trash") or n.get("archived") or n.get("_gone_at")))))
                    self.con.execute("DELETE FROM page_order WHERE page_id=?", (ident,))
                    derived = rec.get("derived")
                    order = derived.get("order") if isinstance(derived, dict) else None
                    if not isinstance(order, list):
                        self.counts["bad_order"] += 1
                        order = []
                    for ord_, item in enumerate(order):
                        if not isinstance(item, list) or len(item) != 3 or nt_id(item[0]) is None \
                                or not common.is_int(item[1]) or not 0 <= item[1] < 2 ** 63:
                            self.counts["bad_order"] += 1
                            continue
                        self.con.execute("INSERT OR IGNORE INTO page_order VALUES(?,?,?,?,?)",
                                         (ident, nt_id(item[0]), ord_, item[1], nt_id(item[2])))
                else:
                    self.con.execute("UPDATE page_src SET rev=?,seq=?,held=0,deleted=1 WHERE page_id=?", (rev, seq, ident))
                    if op == "purged":
                        self.con.execute("UPDATE page_src SET title='' WHERE page_id=?", (ident,))
                        self.con.execute("DELETE FROM page_order WHERE page_id=?", (ident,))
            elif kind == "user":
                if op == "put":
                    self.con.execute("INSERT OR REPLACE INTO users VALUES(?,?,?,?,?)", (ident, rev, seq, held, _s(n.get("name"))))
                else:
                    self.con.execute("UPDATE users SET rev=?,seq=?,held=0,name=CASE WHEN ?='purged' THEN '' ELSE name END WHERE user_id=?",
                                     (rev, seq, op, ident))
                self.touched.update(r[0] for r in self.con.execute("SELECT DISTINCT page_id FROM nrows WHERE user_id=?", (ident,)))
                return
            else:
                if op == "put":
                    self.con.execute("INSERT OR REPLACE INTO ds_src VALUES(?,?,?,?,?,?)", (ident, rev, seq, held, page_title(n), parent_id(n)))
                else:
                    self.con.execute("UPDATE ds_src SET rev=?,seq=?,held=0,title=CASE WHEN ?='purged' THEN '' ELSE title END WHERE ds_id=?",
                                     (rev, seq, op, ident))
                return
        cur = self.con.execute("SELECT rev,message FROM nrows WHERE key=?", (key,)).fetchone()
        if cur and rev <= cur[0]:
            self.counts["stale_rev"] += 1
            return
        if op != "put":
            self.con.execute("UPDATE nrows SET rev=?,seq=?,held=0,own_deleted=1 WHERE key=?", (rev, seq, key))
            if op == "purged":
                self.con.execute("UPDATE nrows SET message='',ng='' WHERE key=?", (key,))
        else:
            ts_ms = iso_ms(n.get("last_edited_time") or n.get("created_time"))
            if ts_ms is None or not 0 < ts_ms < 2 ** 63:
                ts_ms = 0
                self.counts["ts_unknown"] += 1
            user = n.get("last_edited_by") or n.get("created_by")
            uid = nt_id(user.get("id")) or "" if isinstance(user, dict) else ""
            text = row_text(kind, n, self.counts)
            if not text and cur and n.get("_removed_at"):
                text = cur[1]
            own_deleted = int(bool(n.get("_removed_at") or n.get("in_trash") or n.get("archived")))
            self.con.execute("""INSERT INTO nrows(key,kind,native_id,page_id,rev,seq,held,ts,ts_ms,user_id,
             own_deleted,message,ng) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET
             rev=excluded.rev,seq=excluded.seq,held=excluded.held,ts=excluded.ts,ts_ms=excluded.ts_ms,
             user_id=excluded.user_id,own_deleted=excluded.own_deleted,message=excluded.message,ng=excluded.ng""",
                             (key, kind, ident, pid, rev, seq, held, ts_ms // 1000, ts_ms, uid, own_deleted, text, ti.ng_text(text)))
        self.touched.add(pid)

    def end_manifest(self, seq):
        common.bump_counts(self.con, self.counts)
        self.counts.clear()
        if not self.full:
            for pid in sorted(self.touched):
                self._derive(pid)
        self.touched.clear()

    def _derive(self, pid):
        self.con.execute("UPDATE nrows SET " + _LABELS + " WHERE page_id=?", (pid,))
        self.con.execute("""INSERT OR REPLACE INTO pages SELECT page_id,title,parent_id,deleted,
         (SELECT count(*) FROM nrows n WHERE n.page_id=p.page_id AND n.deleted=0),
         (SELECT count(DISTINCT user_id) FROM nrows n WHERE n.page_id=p.page_id AND n.deleted=0)
         FROM page_src p WHERE page_id=?""", (pid,))

    def finish(self):
        for pid, in self.con.execute("SELECT page_id FROM page_src UNION SELECT DISTINCT page_id FROM nrows").fetchall():
            self._derive(pid)


class NotionIndex(common.SourceIndex):
    name = NAME
    origins = frozenset({ORIGIN})
    schema_version = extractor_version = "notion-1"
    fts_tables = ("nrows_fts",)

    def create_tables(self, con):
        con.executescript(SCHEMA)
        con.execute(ti.fts_ddl("nrows"))

    def create_triggers(self, con):
        common.create_triggers(con, "nrows", "nrows_fts")

    def applier(self, con, full):
        return NotionApplier(con, full)

    def stats(self, con):
        return {"rows": con.execute("SELECT count(*) FROM nrows").fetchone()[0],
                "deleted": con.execute("SELECT coalesce(sum(deleted),0) FROM nrows").fetchone()[0],
                **common.read_counts(con)}


SPEC = NotionIndex()


def add_arguments(p):
    common.add_search_arguments(p, ["hits", "count", "timeline", "senders", "pages"])
    p.add_argument("--page", action="append", default=[])
    p.add_argument("--page-text", metavar="PAGE_ID")
    p.add_argument("--list-pages", action="store_true")
    p.add_argument("--before")
    p.add_argument("--after")


class Filters(drv.Filters):
    range_col, range_scale, until_pad = "ts_ms", 1000, 999

    def __init__(self, a, tz):
        super().__init__(a, tz)
        self.selector |= bool(a.page)
        if a.page:
            ors = []
            for value in a.page:
                sql, ps = common.like("t.page_title", value)
                ors.append(f"({sql} OR t.page_id=?)")
                self.params += ps + [nt_id(value) or value]
            self.where.append("(" + " OR ".join(ors) + ")")
        if a.sender:
            sql, ps = common.like("t.sender", a.sender)
            self.where.append(f"({sql} OR t.user_id=?)")
            self.params += ps + [nt_id(a.sender) or a.sender]

    def summary(self, a):
        return {**super().summary(a), "page": a.page}


class NotionSearch(drv.SearchDriver):
    name, table, fts_table = NAME, "nrows", "nrows_fts"
    sort_cols = ("ts_ms", "key")
    group_modes = {"senders": "t.user_id", "pages": "t.page_id"}
    list_flag, in_page_key, page_group = "list_pages", "pages_in_page", "page_id"
    hits_noun, browse_hint, Filters = "rows", "give a query or --page/--sender/--since/--until", Filters

    def check_args(self, a):
        if a.page_text is not None:
            if a.list_pages or nt_id(a.page_text) is None:
                raise common.UsageError("--page-text needs a UUID and cannot be combined with --list-pages")
            return "page-text"

    def parse_cursor(self, value, flag, before=True):
        parts = (value or "").split(".", 1)
        if len(parts) != 2 or len(parts[0]) > 19 or not parts[0].isascii() or not parts[0].isdecimal() or int(parts[0]) >= 2 ** 63 \
                or not re.fullmatch(r"nt:(page|block|comment):[0-9a-f:-]+", parts[1]):
            raise common.UsageError(f"{flag}: expected ts_ms.nt:key")
        return int(parts[0]), parts[1]

    def hit(self, r, tz, marks, width):
        return {k: r[k] for k in ("id", "key", "kind", "native_id", "page_id", "page_title", "ord", "depth", "user_id", "sender", "ts", "ts_ms")} | {
            "deleted": bool(r["deleted"]), "text": r["message"], "snippet": ti.highlight(r["message"], marks, width),
            "date": common.fmt_ts(r["ts"], tz), "cursor": self.cursor(r)}

    def context(self, con, r, n, a, tz):
        if r["ord"] is None:
            return [{**self.hit(r, tz, [], a.width), "hit": True}]
        hide = "" if a.include_deleted else " AND deleted=0"
        rows = con.execute("SELECT * FROM nrows WHERE page_id=? AND kind='block' AND ord BETWEEN ? AND ?" + hide +
                           " ORDER BY ord,key", (r["page_id"], r["ord"] - n, r["ord"] + n)).fetchall()
        if r["kind"] != "block":
            rows = [r, *rows]
        return [{**self.hit(c, tz, [], a.width), "hit": c["id"] == r["id"]} for c in rows]

    def who(self, h):
        return h.get("sender") or h.get("user_id") or "?"

    def flags(self, h):
        return " [deleted]" if h.get("deleted") else ""

    def group_row(self, con, r, mode, tz):
        table, col, label = ("users", "user_id", "name") if mode == "senders" else ("pages", "page_id", "title")
        row = con.execute(f"SELECT {label} FROM {table} WHERE {col}=?", (r["gid"],)).fetchone()
        return {col: r["gid"], "label": (row[0] if row else "") or r["gid"], "n": r["n"],
                "first": common.fmt_ts(r["f"], tz, "%Y-%m-%d") if r["f"] else "",
                "last": common.fmt_ts(r["l"], tz, "%Y-%m-%d") if r["l"] else ""}

    def group_label(self, r, mode):
        col = "user_id" if mode == "senders" else "page_id"
        return r["label"], col + "=" + r[col]

    def breadcrumb(self, con, pid):
        # UNION (not UNION ALL) terminates malformed parent cycles.
        rows = con.execute("""WITH RECURSIVE ancestors(id) AS (SELECT ? UNION
         SELECT p.parent_id FROM page_src p JOIN ancestors a ON p.page_id=a.id WHERE p.parent_id!='')
         SELECT p.page_id,p.title,p.parent_id FROM ancestors a JOIN page_src p ON p.page_id=a.id""", (pid,)).fetchall()
        nodes = {r[0]: (r[1], r[2]) for r in rows}
        labels, seen = [], set()
        while pid in nodes and pid not in seen:
            seen.add(pid)
            title, parent = nodes[pid]
            labels.append(title or pid)
            pid = parent
        return list(reversed(labels))

    def list_all(self, con, a, tz):
        where, params = [], []
        if not a.include_deleted:
            where.append("deleted=0")
        if a.page:
            ors = []
            for value in a.page:
                sql, ps = common.like("title", value)
                ors.append(f"({sql} OR page_id=?)")
                params += ps + [nt_id(value) or value]
            where.append("(" + " OR ".join(ors) + ")")
        clause = " WHERE " + " AND ".join(where) if where else ""
        rows = con.execute("SELECT * FROM pages" + clause + " ORDER BY n DESC,page_id LIMIT ?", params + [a.limit]).fetchall()
        return {"mode": "list", "total": con.execute("SELECT count(*) FROM pages" + clause, params).fetchone()[0],
                "rows": [{**dict(r), "label": r["title"] or r["page_id"], "breadcrumb": self.breadcrumb(con, r["page_id"])} for r in rows]}

    def extra_result(self, con, a, extra, query, tz):
        pid = nt_id(a.page_text)
        if con.execute("SELECT 1 FROM page_src WHERE page_id=?", (pid,)).fetchone() is None:
            raise common.UsageError("page not in Notion index")
        hide = "" if a.include_deleted else " AND deleted=0"
        rows = con.execute("SELECT * FROM nrows WHERE page_id=?" + hide +
                           " ORDER BY CASE kind WHEN 'page' THEN 0 WHEN 'block' THEN 1 ELSE 2 END,ord,key", (pid,)).fetchall()
        if query or a.page or a.sender or a.since or a.until or a.before or a.after or a.mode != "hits":
            common.note("query and selection filters are not applied to --page-text")
        return {"mode": "page-text", "page_id": pid, "breadcrumb": self.breadcrumb(con, pid),
                "total": len(rows), "rows": [self.hit(r, tz, [], a.width) for r in rows]}

    def render_rest(self, res, a, tz):
        print(f"{NAME}: {res['total']} {res['mode']} result(s)")
        for row in res["rows"]:
            print(json.dumps(row, ensure_ascii=False))


DRIVER = NotionSearch(SPEC)


def run(a):
    return DRIVER.run(a)
