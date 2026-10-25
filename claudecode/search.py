"""Ledger-only Claude Code FTS5 index and session search."""
import json
from collections import Counter
from rawlayer.search import common, search as drv, textindex as ti
from .keys import cc_id, iso_ms

NAME = "claudecode"
HELP = "search Claude Code prompts, replies and tool calls"
ORIGIN = "cc_archive"
SCHEMA = """
CREATE TABLE msgs(
 id INTEGER PRIMARY KEY, key TEXT NOT NULL UNIQUE, container TEXT NOT NULL,
 session_id TEXT NOT NULL, agent TEXT NOT NULL, uuid TEXT NOT NULL, sid INTEGER NOT NULL,
 rev INTEGER NOT NULL, seq INTEGER NOT NULL, held INTEGER NOT NULL, ts INTEGER NOT NULL,
 ts_ms INTEGER NOT NULL, role TEXT NOT NULL, project TEXT NOT NULL, branch TEXT NOT NULL,
 model TEXT NOT NULL, tools TEXT NOT NULL, message TEXT NOT NULL, ng TEXT NOT NULL);
CREATE INDEX msgs_time ON msgs(ts_ms,sid,container);
CREATE INDEX msgs_session ON msgs(session_id,ts_ms,sid,container);
CREATE TABLE sessions(session_id TEXT PRIMARY KEY, label TEXT NOT NULL, project TEXT NOT NULL,
 n INTEGER NOT NULL, first INTEGER NOT NULL, last INTEGER NOT NULL) WITHOUT ROWID;
CREATE VIEW search_msgs AS SELECT *, message || char(10) || tools AS search_text FROM msgs;
"""


def _s(value):
    return common.clean(value) if isinstance(value, str) else ""


def tool_text(value):
    return "\n".join(f"{common.flat(_s(v.get('name')))}: {common.flat(_s(v.get('summary')))}"
                     for v in value if isinstance(v, dict)) \
        if isinstance(value, list) else ""


def _body(row):
    return "\n".join(v for v in (row["message"], row["tools"]) if v)


class ClaudeCodeApplier:
    def __init__(self, con, full):
        self.con, self.full = con, full
        self.counts = Counter()
        self.dirty = False

    def record(self, rec, seq):
        if not isinstance(rec, dict) or rec.get("origin") != ORIGIN or rec.get("op") not in ("put", "absent", "purged") \
                or not common.is_int(rec.get("rev")) or not 0 < rec["rev"] < 2 ** 63:
            self.counts["bad_record"] += 1
            return
        key, rev, op = rec.get("key"), rec["rev"], rec["op"]
        parts = key.split(":") if isinstance(key, str) else []
        if len(parts) != 4 or parts[:2] != ["cc", "msg"] or rec.get("kind") != "msg" \
                or cc_id(parts[2]) is None or cc_id(parts[3]) is None or _s(key) != key:
            self.counts["bad_key"] += 1
            return
        cur = self.con.execute("SELECT rev FROM msgs WHERE key=?", (key,)).fetchone()
        if cur and rev <= cur[0]:
            self.counts["stale_rev"] += 1
            return
        if op != "put":
            self.con.execute("UPDATE msgs SET rev=?,seq=?,held=0 WHERE key=?", (rev, seq, key))
            if op == "purged":
                self.con.execute("UPDATE msgs SET message='',tools='',ng='' WHERE key=?", (key,))
        else:
            n = rec.get("native")
            if not isinstance(n, dict):
                self.counts["bad_native"] += 1
                return
            ts_ms = iso_ms(n.get("ts"))
            sid = n.get("n")
            if ts_ms is None or not -(2 ** 63) <= ts_ms < 2 ** 63:
                self.counts["ts_unknown"] += 1
                return
            if not common.is_int(sid) or not 0 <= sid < 2 ** 63 or cc_id(n.get("session")) is None \
                    or n.get("uuid") != parts[3]:
                self.counts["bad_native"] += 1
                return
            text, tools = _s(n.get("text")), tool_text(n.get("tools"))
            values = (key, parts[2], _s(n["session"]), _s(n.get("agent")), parts[3], sid, rev, seq, 1,
                      ts_ms // 1000, ts_ms, _s(n.get("role")), _s(n.get("cwd")), _s(n.get("branch")),
                      _s(n.get("model")), tools, text, ti.ng_text("\n".join((text, tools))))
            self.con.execute("""INSERT INTO msgs(key,container,session_id,agent,uuid,sid,rev,seq,held,ts,ts_ms,
             role,project,branch,model,tools,message,ng) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
             ON CONFLICT(key) DO UPDATE SET container=excluded.container,session_id=excluded.session_id,
             agent=excluded.agent,uuid=excluded.uuid,sid=excluded.sid,rev=excluded.rev,seq=excluded.seq,
             held=excluded.held,ts=excluded.ts,ts_ms=excluded.ts_ms,role=excluded.role,project=excluded.project,
             branch=excluded.branch,model=excluded.model,tools=excluded.tools,message=excluded.message,ng=excluded.ng""", values)
        self.dirty = True

    def end_manifest(self, seq):
        common.bump_counts(self.con, self.counts)
        self.counts.clear()
        if not self.full and self.dirty:
            self.finish()
        self.dirty = False

    def finish(self):
        self.con.execute("DELETE FROM sessions")
        self.con.execute("""INSERT INTO sessions SELECT session_id,
         coalesce((SELECT substr(message,1,80) FROM msgs p WHERE p.session_id=m.session_id AND p.held=1
          AND p.role IN ('user','prompt') ORDER BY p.ts_ms,p.sid,p.container LIMIT 1),''),
         (SELECT project FROM msgs p WHERE p.session_id=m.session_id AND p.held=1
          ORDER BY p.ts_ms,p.sid,p.container LIMIT 1),count(*),min(ts),max(ts)
         FROM msgs m WHERE held=1 GROUP BY session_id""")


class ClaudeCodeIndex(common.SourceIndex):
    name = NAME
    origins = frozenset({ORIGIN})
    schema_version = extractor_version = "claudecode-1"
    fts_tables = ("msgs_fts",)

    def create_tables(self, con):
        con.executescript(SCHEMA)
        con.execute(ti.fts_ddl("msgs"))

    def create_triggers(self, con):
        common.create_triggers(con, "msgs", "msgs_fts")

    def applier(self, con, full):
        return ClaudeCodeApplier(con, full)

    def stats(self, con):
        return {"rows": con.execute("SELECT count(*) FROM msgs").fetchone()[0],
                "deleted": con.execute("SELECT count(*) FROM msgs WHERE held=0").fetchone()[0],
                **common.read_counts(con)}


SPEC = ClaudeCodeIndex()


def add_arguments(p):
    common.add_search_arguments(p, ["hits", "count", "timeline", "senders", "sessions", "projects"])
    p.add_argument("--project", action="append", default=[], help="substring of the working folder")
    p.add_argument("--role", choices=("user", "assistant", "prompt"))
    p.add_argument("--session", metavar="ID", help="show the whole session, including subagents, in order")
    p.add_argument("--list-sessions", action="store_true")
    p.add_argument("--before", help="page before ts_ms.sid.container")
    p.add_argument("--after", help="page after ts_ms.sid.container")


class Filters(drv.Filters):
    range_col, range_scale, until_pad = "ts_ms", 1000, 999

    def __init__(self, a, tz):
        super().__init__(a, tz)
        self.where = ["t.held = 1" if w == "t.deleted = 0" else w for w in self.where]
        self.selector |= bool(a.project or a.role)
        if a.project:
            clauses = [common.like("t.project", value) for value in a.project]
            self.where.append("(" + " OR ".join(sql for sql, _ in clauses) + ")")
            self.params += [p for _, ps in clauses for p in ps]
        if a.role:
            self.where.append("t.role=?")
            self.params.append(a.role)
        if a.sender:
            sql, ps = common.like("t.role", a.sender)
            self.where.append(sql)
            self.params += ps

    def summary(self, a):
        return {**super().summary(a), "project": a.project, "role": a.role, "session": a.session}


class ClaudeCodeSearch(drv.SearchDriver):
    name, table, fts_table = NAME, "search_msgs", "msgs_fts"
    sort_cols = ("ts_ms", "sid", "container")
    group_modes = {"senders": "t.role", "sessions": "t.session_id", "projects": "t.project"}
    list_flag, in_page_key, page_group = "list_sessions", "sessions_in_page", "session_id"
    hits_noun, browse_hint, Filters = "messages", "give a query or --project/--role/--sender/--since/--until", Filters
    key_col, container_col = "key", "session_id"

    def body(self, q, flt, extra_where=(), extra_params=()):
        # Literal verification must see the same tool summaries as the n-gram index.
        if q is None:
            frm, where, params = f"{self.table} t", [], []
        else:
            frm, where, params = ti.sql_parts(q, self.table, self.fts_table, text_col="search_text")
        where = list(where) + flt.where + list(extra_where)
        return f"FROM {frm} WHERE " + (" AND ".join(where) or "1"), list(params) + flt.params + list(extra_params)

    def rows_by_key(self, con, keys, q, flt):
        keys, out = list(keys), {}
        for i in range(0, len(keys), 400):
            kw, kp = self.key_where(keys[i:i + 400])
            body, ps = self.body(q._replace(fts="", verify=[], weak=[]), flt, [kw], kp)
            out.update((r["key"], r) for r in con.execute(f"SELECT t.* {body}", ps))
        return out

    def chunk_units(self, con, container):
        rows = con.execute("SELECT * FROM msgs WHERE session_id=? AND held=1 AND (message!='' OR tools!='') "
                           "ORDER BY ts_ms,sid,container", (container,)).fetchall()
        session = con.execute("SELECT label,project FROM sessions WHERE session_id=?", (container,)).fetchone()
        label = " › ".join(v for v in (session["project"], session["label"] or container) if v) if session else container
        return label, [(r["key"], r["ts"], f"{r['role']}{' (' + r['agent'] + ')' if r['agent'] else ''}: {_body(r)}", False)
                       for r in rows]

    def check_args(self, a):
        if a.session is not None:
            if cc_id(a.session) is None or a.list_sessions:
                raise common.UsageError("--session needs a valid id and cannot be combined with --list-sessions")
            return "session"

    def parse_cursor(self, value, flag, before=True):
        parts = (value or "").split(".", 2)
        if len(parts) != 3 or not all(len(p) <= 19 and p.isascii() and p.isdecimal() for p in parts[:2]) \
                or cc_id(parts[2]) is None:
            raise common.UsageError(f"{flag}: expected ts_ms.sid.container")
        if any(int(p) >= 2 ** 63 for p in parts[:2]):
            raise common.UsageError(f"{flag}: cursor out of range")
        return int(parts[0]), int(parts[1]), parts[2]

    def hit(self, r, tz, marks, width):
        return {k: r[k] for k in ("id", "key", "session_id", "agent", "role", "project", "branch", "model", "ts", "ts_ms")} | {
            "text": r["message"], "tools": r["tools"], "snippet": ti.highlight(_body(r), marks, width),
            "date": common.fmt_ts(r["ts"], tz), "cursor": self.cursor(r)}

    def context(self, con, r, n, a, tz):
        hide = "" if a.include_deleted else " AND held=1"
        args = (r["session_id"], r["ts_ms"], r["sid"], r["container"], n)
        before = con.execute("SELECT * FROM msgs WHERE session_id=? AND (ts_ms,sid,container)<(?,?,?)" + hide +
                             " ORDER BY ts_ms DESC,sid DESC,container DESC LIMIT ?", args).fetchall()
        after = con.execute("SELECT * FROM msgs WHERE session_id=? AND (ts_ms,sid,container)>(?,?,?)" + hide +
                            " ORDER BY ts_ms,sid,container LIMIT ?", args).fetchall()
        return [{**self.hit(c, tz, [], a.width), "hit": c["id"] == r["id"]} for c in [*reversed(before), r, *after]]

    def who(self, h):
        return h["role"]

    def flags(self, h):
        return ""

    def group_row(self, con, r, mode, tz):
        col = {"senders": "role", "sessions": "session_id", "projects": "project"}[mode]
        label = r["gid"]
        if mode == "sessions":
            session = con.execute("SELECT label FROM sessions WHERE session_id=?", (r["gid"],)).fetchone()
            label = (session[0] if session else "") or label
        return {col: r["gid"], "label": label, "n": r["n"],
                "first": common.fmt_ts(r["f"], tz, "%Y-%m-%d") if r["f"] else "",
                "last": common.fmt_ts(r["l"], tz, "%Y-%m-%d") if r["l"] else ""}

    def group_label(self, r, mode):
        col = {"senders": "role", "sessions": "session_id", "projects": "project"}[mode]
        return r["label"], col + "=" + r[col]

    def list_all(self, con, a, tz):
        flt = self.filters(a, tz)
        body, ps = self.body(None, flt)
        rows = con.execute("SELECT t.session_id,coalesce((SELECT label FROM sessions s WHERE "
                           "s.session_id=t.session_id),'') AS label,t.project,count(*) AS n,"
                           "min(t.ts) AS first,max(t.ts) AS last " + body +
                           " GROUP BY t.session_id ORDER BY n DESC,t.session_id LIMIT ?", ps + [a.limit]).fetchall()
        total = con.execute(f"SELECT count(DISTINCT t.session_id) {body}", ps).fetchone()[0]
        return {"mode": "list", "total": total, "rows": [dict(r) for r in rows]}

    def extra_result(self, con, a, extra, query, tz):
        hide = "" if a.include_deleted else " AND held=1"
        rows = con.execute("SELECT * FROM msgs WHERE session_id=?" + hide + " ORDER BY ts_ms,sid,container",
                           (a.session,)).fetchall()
        if not rows:
            raise common.UsageError("session not in Claude Code index")
        if query or a.project or a.role or a.sender or a.since or a.until or a.before or a.after or a.mode != "hits":
            common.note("query and selection filters are not applied to --session")
        return {"mode": "session", "session_id": a.session, "total": len(rows),
                "rows": [self.hit(r, tz, [], a.width) for r in rows]}

    def render_rest(self, res, a, tz):
        print(f"{NAME}: {res['total']} {res['mode']} result(s)")
        for row in res["rows"]:
            print(json.dumps(row, ensure_ascii=False))


DRIVER = ClaudeCodeSearch(SPEC)


def run(a):
    return DRIVER.run(a)
