# -*- coding: utf-8 -*-
"""The query half of an app's search.py. An app's search module subclasses SearchDriver for its row model and
delegates the module-level run()/add_arguments() the CLI calls (rawlayer/search/cli.py) to the subclass
instance; the fold/index half stays where it was, in the module's SourceIndex subclass (common.py):

  class KakaoSearch(SearchDriver):
      name, table, fts_table, sort_cols, ...   # the source's facts
      Filters, hit(), context(), ...           # the source's row shape
  DRIVER = KakaoSearch(SPEC)                   # SPEC: the module's SourceIndex

What the base owns is what the apps had as copies: the run() skeleton (argument checks, query build, keyset
cursor, dispatch, the JSON envelope), the hits/browse/aggregate queries, the time-window and --include-deleted
filters, and the count/timeline/senders text output. An app overrides the hooks its row model needs and adds
its own dispatch (mattermost's --thread) through check_args/extra_result."""
import sys

from . import common
from . import textindex as ti
from .common import UsageError


class Filters:
    """WHERE parts every source shares: --since/--until on the range column and --include-deleted. A source's
    subclass appends its label filters after super().__init__() and widens selector/summary."""
    range_col = "ts"                 # t.<col> the time window filters (ts / ts_ms)
    range_scale = 1                  # what a parsed --since/--until (seconds) multiplies by (ms columns: 1000)
    until_pad = 0                    # an inclusive --until: 999 with a ms column

    def __init__(self, a, tz):
        self.where, self.params = [], []
        self.since = common.parse_when(a.since, tz) if a.since else None
        self.until = common.parse_when(a.until, tz, end=True) if a.until else None
        col = f"t.{self.range_col}"
        if self.since is not None or self.until is not None:
            self.where.append(f"{col} > 0")
        if self.since is not None:
            self.where.append(f"{col} >= ?")
            self.params.append(self.since * self.range_scale)
        if self.until is not None:
            self.where.append(f"{col} <= ?")
            self.params.append(self.until * self.range_scale + self.until_pad)
        if not a.include_deleted:
            self.where.append("t.deleted = 0")
        self.selector = bool(a.sender or self.since is not None or self.until is not None)

    def summary(self, a):
        return {"sender": a.sender, "since": self.since, "until": self.until,
                "include_deleted": bool(a.include_deleted)}


class SearchDriver:
    """Base of an app's search command. Class attributes are the source's facts; the rest is hooks."""
    name = ""                        # the source name: headers, the JSON envelope's "source"
    table = ""                       # the content table (aliased t)
    fts_table = ""                   # its FTS5 shadow table
    sort_cols = ()                   # the keyset sort key, outermost first; the cursor joins a row's values
    group_modes = {}                 # mode -> id column, e.g. {"senders": "t.author_id", "rooms": "t.chat_id"}
    list_flag = ""                   # the --list-... argparse attr, "" when the source has no list mode
    in_page_key = ""                 # browse JSON key: how many groups the page spans (rooms_in_page, ...)
    page_group = ""                  # the hit field that names the group (chat_id / channel_id)
    hits_noun = "messages"           # "--before/--after page through <noun> (--mode hits) only"
    browse_hint = ""                 # the usage error when neither a query nor a selector is given

    def __init__(self, spec):
        self.spec = spec             # the module's SourceIndex (open_search/warn_if_stale stamp against it)

    # ---- arguments and validation
    def check_args(self, a):
        """The app's own incompatibilities (UsageError) and its extra dispatch mode: None, or a name
        extra_result() handles (mattermost: 'thread')."""
        return None

    def parse_cursor(self, s, flag, before=True):
        """The keyset cursor of --before/--after -> the row's sort key values (common.parse_cursor's
        ts.n.row form; a source whose sort key is not all ints overrides)."""
        return common.parse_cursor(s, flag, before=before)

    # ---- the hooks a row model implements
    def filters(self, a, tz):
        return self.Filters(a, tz)

    def hit(self, r, tz, marks, width):
        raise NotImplementedError

    def context(self, con, r, n, a, tz):
        """n rows before and after a hit (--context), the hit itself marked."""
        raise NotImplementedError

    def placeholder(self, r):
        raise NotImplementedError

    def who(self, row):
        raise NotImplementedError

    def flags(self, h):
        raise NotImplementedError

    def group_row(self, con, r, mode, tz):
        """One --mode senders/rooms(channels) row: the label lookup of a (gid, n, first, last) group."""
        raise NotImplementedError

    def group_label(self, r, mode):
        """(label, ident) of a group row for the text output."""
        raise NotImplementedError

    def list_all(self, con, a, tz):
        """The --list-... mode."""
        raise NotImplementedError

    def extra_result(self, con, a, extra, query, tz):
        """The mode check_args() named."""
        raise NotImplementedError

    # ---- the shared queries
    def cursor(self, r):
        """A row's keyset cursor: its sort key values, "."-joined."""
        return ".".join(str(r[c]) for c in self.sort_cols)

    def body(self, q, flt, extra_where=(), extra_params=()):
        if q is not None:
            frm, where, params = ti.sql_parts(q, self.table, self.fts_table, alias="t")
        else:
            frm, where, params = f"{self.table} t", [], []
        where = list(where) + flt.where + list(extra_where)
        params = list(params) + flt.params + list(extra_params)
        return f"FROM {frm} WHERE " + (" AND ".join(where) or "1"), params

    def hits(self, con, a, q, flt, tz):
        body, params = self.body(q, flt)
        total = con.execute(f"SELECT count(*) {body}", params).fetchone()[0]
        order = (f"{self.fts_table}.rank, " if q.fts else "") + ", ".join(f"t.{c} DESC" for c in self.sort_cols)
        rows = con.execute(f"SELECT t.* {body} ORDER BY {order} LIMIT ?", params + [a.limit]).fetchall()
        out = []
        for r in rows:
            h = self.hit(r, tz, q.marks, a.width)
            if a.context:
                h["context"] = self.context(con, r, a.context, a, tz)
            out.append(h)
        return {"mode": "hits", "total": total, "rows": out}

    def browse(self, con, a, q, flt, cursor, tz):
        body, params = self.body(q, flt)
        total = con.execute(f"SELECT count(*) {body}", params).fetchone()[0]
        extra, xp, desc = [], [], True
        if cursor:
            direction, *key = cursor
            desc = direction == "before"
            cols = ", ".join(f"t.{c}" for c in self.sort_cols)
            ph = ", ".join("?" for _ in self.sort_cols)
            extra = [f"({cols}) < ({ph})" if desc else f"({cols}) > ({ph})"]
            xp = key
        body2, params2 = self.body(q, flt, extra, xp)
        d = "DESC" if desc else "ASC"
        order = ", ".join(f"t.{c} {d}" for c in self.sort_cols)
        rows = con.execute(f"SELECT t.* {body2} ORDER BY {order} LIMIT ?", params2 + [a.limit + 1]).fetchall()
        more = len(rows) > a.limit
        rows = rows[:a.limit]
        if desc:
            rows.reverse()
        marks = q.marks if q else []
        out = [self.hit(r, tz, marks, a.width) for r in rows]
        page = {"before": out[0]["cursor"] if out else None, "after": out[-1]["cursor"] if out else None,
                "more_older": more if desc else None, "more_newer": more if not desc else None}
        return {"mode": "browse", "total": total, "rows": out, "page": page,
                self.in_page_key: len({h[self.page_group] for h in out})}

    def aggregate(self, con, a, q, flt, tz):
        body, params = self.body(q, flt)
        total = con.execute(f"SELECT count(*) {body}", params).fetchone()[0]
        if a.mode == "count":
            return {"mode": "count", "total": total, "rows": []}
        if a.mode == "timeline":
            rows = con.execute(f"SELECT {common.month_sql('t.ts', tz)} AS bucket, count(*) AS n {body} AND t.ts > 0 "
                               f"GROUP BY bucket ORDER BY bucket", params).fetchall()
            kept, omitted = common.clip_buckets([{"bucket": r["bucket"], "n": r["n"]} for r in rows], a.limit)
            return {"mode": "timeline", "total": total, "rows": kept, "omitted_buckets": omitted}
        col = self.group_modes[a.mode]
        rows = con.execute(f"SELECT {col} AS gid, count(*) AS n, MIN(CASE WHEN t.ts > 0 THEN t.ts END) AS f, "
                           f"MAX(CASE WHEN t.ts > 0 THEN t.ts END) AS l {body} GROUP BY {col} ORDER BY n DESC, gid "
                           f"LIMIT ?", params + [a.limit]).fetchall()
        return {"mode": a.mode, "total": total, "rows": [self.group_row(con, r, a.mode, tz) for r in rows]}

    # ---- entry point (what the module-level run() delegates to)
    def run(self, a):
        data_root = common.load_data_root(a.config)
        tz = common.parse_tz(a.tz)
        common.check_numbers(a)
        query = common.query_text(a)
        if a.before and a.after:
            raise UsageError("give --before or --after, not both")
        extra = self.check_args(a)
        cursor = None
        if extra is None:
            if a.before:
                cursor = ("before",) + self.parse_cursor(a.before, "--before", before=True)
            elif a.after:
                cursor = ("after",) + self.parse_cursor(a.after, "--after", before=False)
        q = None
        if query is not None and extra is None:
            try:
                q = ti.build_fts_query(query)
            except ti.QueryError as e:
                raise UsageError(f"bad query: {e}") from None
        con, meta, fresh = common.open_search(data_root, self.spec)
        try:
            common.warn_if_stale(self.spec, fresh)
            flt = self.filters(a, tz)
            if extra is not None:
                res = self.extra_result(con, a, extra, query, tz)
            elif self.list_flag and getattr(a, self.list_flag):
                res = self.list_all(con, a, tz)
            else:
                if q is None and not flt.selector:
                    raise UsageError(self.browse_hint)
                if cursor and a.mode != "hits":
                    raise UsageError(f"--before/--after page through {self.hits_noun} (--mode hits) only")
                if q is not None:
                    if a.explain and not a.json:
                        print(f"# fts5 index expr : {common.show_fts(q.fts) or '(none -- full scan)'}")
                        print(f"# literal verify  : {q.verify or '-'}   exclude: {q.exclude or '-'}   "
                              f"weak: {q.weak or '-'}   exclude expr: {common.show_fts(q.exclude_fts) or '-'}")
                    if q.weak:
                        common.note(f"{', '.join(q.weak)}: a single character cannot use the n-gram index; "
                                    f"falling back to a slower substring scan")
                    elif not q.fts:
                        common.note("no term could use the index, so this is a full scan")
                if a.mode == "hits" and (q is None or cursor):
                    res = self.browse(con, a, q, flt, cursor, tz)
                elif a.mode == "hits":
                    res = self.hits(con, a, q, flt, tz)
                else:
                    res = self.aggregate(con, a, q, flt, tz)
            if a.json:
                env = {"source": self.name, "mode": res["mode"], "query": query,
                       "fts": common.show_fts(q.fts) if q else None,
                       "exclude_fts": common.show_fts(q.exclude_fts) if q else None,
                       "weak": q.weak if q else [], "verify": q.verify if q else [], "exclude": q.exclude if q else [],
                       "filters": flt.summary(a), "tz": common.tz_label(tz)}
                env.update({k: v for k, v in res.items() if k != "mode"})
                env.update(built_at=meta.get("built_at"), updated_at=meta.get("updated_at"),
                           index={"seq": fresh["seq"], "ledger_seq": fresh["ledger_seq"], "stale": fresh["stale"]})
                print(common.dumps(env))
            else:
                self.render(res, a, tz)
        finally:
            con.close()
        return common.EXIT_OK

    # ---- text output
    def render(self, res, a, tz):
        mode, rows, total = res["mode"], res["rows"], res["total"]
        out = sys.stdout.write
        if mode == "count":
            out(f"=== {self.name}: {total} match(es) ===\n")
            return
        if mode == "timeline":
            out(f"=== {self.name}: {total} match(es) by month ({common.tz_label(tz)}) ===\n")
            if not rows:
                out("  (no matches)\n")
            if res.get("omitted_buckets"):
                out(f"  ... {res['omitted_buckets']} earlier month(s) omitted by --limit {a.limit}; raise --limit, "
                    f"or set --until to walk the window back over them\n")
            peak = max((r["n"] for r in rows), default=1)
            for r in rows:
                out(f"  {r['bucket']}  {r['n']:6,}  {'#' * max(1, round(28 * r['n'] / peak))}\n")
            return
        if mode in self.group_modes:
            out(f"=== {self.name}: top {mode} for this query ({total} match(es) total) ===\n")
            if not rows:
                out("  (no matches)\n")
            for r in rows:
                span = f"{r['first']}..{r['last']}" if r["first"] else ""
                label, ident = self.group_label(r, mode)
                out(f"  {r['n']:7,}  {common.pad(label, 38)} {span:22} {ident}\n")
            return
        self.render_rest(res, a, tz)

    def render_rest(self, res, a, tz):
        """The source's own modes (list, thread, browse, hits)."""
        raise NotImplementedError
