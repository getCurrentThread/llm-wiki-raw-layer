"""Ledger-only Gmail FTS5 with TEXT Gmail IDs and independent label state."""
import json
import re
from collections import Counter
from rawlayer.capture.canon import pk_text
from rawlayer.search import common, search as drv, textindex as ti
from .keys import gm_id, MAX_MS

NAME, HELP, ORIGIN = "gmail", "search Gmail messages, labels and threads", "gm_archive"
SCHEMA = """
CREATE TABLE msgs(id INTEGER PRIMARY KEY, key TEXT NOT NULL UNIQUE, msg_id TEXT NOT NULL UNIQUE,
 thread_id TEXT NOT NULL, rev INTEGER NOT NULL, seq INTEGER NOT NULL, held INTEGER NOT NULL,
 ts INTEGER NOT NULL, ts_ms INTEGER NOT NULL, sender TEXT NOT NULL, subject TEXT NOT NULL,
 recipients TEXT NOT NULL, body_text TEXT NOT NULL, attachments TEXT NOT NULL,
 deleted INTEGER NOT NULL DEFAULT 0, message TEXT NOT NULL, ng TEXT NOT NULL, tng TEXT NOT NULL DEFAULT '');
CREATE INDEX msgs_time ON msgs(ts_ms,msg_id);
CREATE INDEX msgs_thread ON msgs(thread_id,ts_ms);
CREATE TABLE states(msg_id TEXT PRIMARY KEY, rev INTEGER NOT NULL, seq INTEGER NOT NULL,
 held INTEGER NOT NULL, removed INTEGER NOT NULL, labels TEXT NOT NULL) WITHOUT ROWID;
CREATE TABLE labels(label_id TEXT PRIMARY KEY, rev INTEGER NOT NULL, seq INTEGER NOT NULL,
 held INTEGER NOT NULL, name TEXT NOT NULL, removed INTEGER NOT NULL) WITHOUT ROWID;
CREATE TABLE membership(msg_id TEXT NOT NULL,label_id TEXT NOT NULL,
 PRIMARY KEY(msg_id,label_id)) WITHOUT ROWID;
"""


def _s(value):
    return common.clean(value) if isinstance(value, str) else ""


class GmailApplier:
    def __init__(self, con, full):
        self.con, self.full, self.counts, self.dirty = con, full, Counter(), False

    def record(self, rec, seq):
        if not isinstance(rec, dict) or rec.get("origin") != ORIGIN \
                or rec.get("op") not in ("put", "absent", "purged") \
                or not common.is_int(rec.get("rev")) or not 0 < rec["rev"] < 2 ** 63:
            self.counts["bad_record"] += 1
            return
        key, kind, op, rev = rec.get("key"), rec.get("kind"), rec["op"], rec["rev"]
        match = re.fullmatch(r"gm:(msg|lbls|label):([^:]+)", key) if isinstance(key, str) else None
        if not match or kind != match[1] or (gm_id(match[2]) if kind != "label" else pk_text(match[2])) is None:
            self.counts["bad_key"] += 1
            return
        native = rec.get("native")
        if op == "put" and not isinstance(native, dict):
            self.counts["bad_native"] += 1
            return
        n, ident, held = native if isinstance(native, dict) else {}, match[2], int(op == "put")
        table, col = {"msg": ("msgs", "msg_id"), "lbls": ("states", "msg_id"), "label": ("labels", "label_id")}[kind]
        old = self.con.execute(f"SELECT rev FROM {table} WHERE {col}=?", (ident,)).fetchone()
        if old and rev <= old[0]:
            self.counts["stale_rev"] += 1
            return
        if op != "put":
            self.con.execute(f"UPDATE {table} SET rev=?,seq=?,held=0 WHERE {col}=?", (rev, seq, ident))
            if kind == "msg" and op == "purged":
                self.con.execute("UPDATE msgs SET subject='',sender='',recipients='',body_text='',attachments='',message='',ng='',tng='' WHERE msg_id=?", (ident,))
        elif kind == "msg":
            ts = n.get("internal_date_ms")
            if type(ts) is not int or not 0 <= ts <= MAX_MS or gm_id(n.get("thread_id")) is None:
                self.counts["bad_native"] += 1
                return
            subject, sender, body = (_s(n.get(k)) for k in ("subject", "from", "body_text"))
            recipients = "\n".join(_s(n.get(k)) for k in ("to", "cc"))
            files = n.get("attachments", [])
            attachments = "\n".join(_s(v.get("filename")) for v in files if isinstance(v, dict)) if isinstance(files, list) else ""
            text = "\n".join((subject, sender, recipients, body, attachments))
            ng = ti.ng_text(text)
            self.con.execute("""INSERT INTO msgs(key,msg_id,thread_id,rev,seq,held,ts,ts_ms,sender,subject,
             recipients,body_text,attachments,message,ng,tng) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
             ON CONFLICT(msg_id) DO UPDATE SET rev=excluded.rev,seq=excluded.seq,held=excluded.held,
             thread_id=excluded.thread_id,ts=excluded.ts,ts_ms=excluded.ts_ms,sender=excluded.sender,
             subject=excluded.subject,recipients=excluded.recipients,body_text=excluded.body_text,
             attachments=excluded.attachments,message=excluded.message,ng=excluded.ng,tng=excluded.tng""",
             (key, ident, n["thread_id"], rev, seq, held, ts // 1000, ts, sender, subject,
              recipients, body, attachments, text, ng, ti.title_ng(subject, ng)))
        elif kind == "lbls":
            labels = n.get("label_ids")
            if not isinstance(labels, list) or not all(isinstance(v, str) and pk_text(v) for v in labels):
                self.counts["bad_native"] += 1
                return
            self.con.execute("INSERT OR REPLACE INTO states VALUES(?,?,?,?,?,?)",
                             (ident, rev, seq, held, int(bool(n.get("_removedAt"))), json.dumps(sorted(set(labels)))))
        else:
            self.con.execute("INSERT OR REPLACE INTO labels VALUES(?,?,?,?,?,?)",
                             (ident, rev, seq, held, _s(n.get("name")), int(bool(n.get("_removedAt")))))
        self.dirty = True

    def end_manifest(self, seq):
        common.bump_counts(self.con, self.counts)
        self.counts.clear()
        if not self.full and self.dirty:
            self.finish()
        self.dirty = False

    def finish(self):
        self.con.execute("DELETE FROM membership")
        for ident, labels in self.con.execute("SELECT msg_id,labels FROM states WHERE held=1"):
            self.con.executemany("INSERT INTO membership VALUES(?,?)", [(ident, v) for v in json.loads(labels)])
        self.con.execute("""UPDATE msgs SET deleted=CASE WHEN held=0 OR
          coalesce((SELECT removed OR held=0 FROM states s WHERE s.msg_id=msgs.msg_id),0) OR
          EXISTS(SELECT 1 FROM membership m WHERE m.msg_id=msgs.msg_id AND label_id IN ('SPAM','TRASH'))
          THEN 1 ELSE 0 END""")


class GmailIndex(common.SourceIndex):
    name, origins = NAME, frozenset({ORIGIN})
    schema_version = extractor_version = "gmail-2"
    fts_tables = ("msgs_fts",)

    def create_tables(self, con):
        con.executescript(SCHEMA)
        con.execute(ti.fts_ddl("msgs", title=True))
        con.execute(ti.title_rank("msgs_fts"))

    def create_triggers(self, con):
        common.create_triggers(con, "msgs", "msgs_fts", title=True)

    def applier(self, con, full):
        return GmailApplier(con, full)

    def stats(self, con):
        return {"rows": con.execute("SELECT count(*) FROM msgs").fetchone()[0],
                "deleted": con.execute("SELECT coalesce(sum(deleted),0) FROM msgs").fetchone()[0],
                **common.read_counts(con)}


SPEC = GmailIndex()


def add_arguments(p):
    common.add_search_arguments(p, ["hits", "count", "timeline", "senders", "labels", "threads"])
    p.add_argument("--label", action="append", default=[])
    p.add_argument("--from", dest="from_address")
    p.add_argument("--thread")
    p.add_argument("--include-removed", action="store_true")
    p.add_argument("--before")
    p.add_argument("--after")


class Filters(drv.Filters):
    range_col, range_scale, until_pad = "ts_ms", 1000, 999

    def __init__(self, a, tz):
        a.include_deleted = a.include_deleted or a.include_removed
        super().__init__(a, tz)
        self.selector |= bool(a.label or a.from_address or a.thread)
        for value in (a.sender, a.from_address):
            if value:
                sql, params = common.like("t.sender", value)
                self.where.append(sql)
                self.params += params
        if a.thread:
            self.where.append("t.thread_id=?")
            self.params.append(a.thread)
        for value in a.label:
            sql, params = common.like("l.name", value)
            self.where.append(f"EXISTS(SELECT 1 FROM membership m LEFT JOIN labels l ON l.label_id=m.label_id WHERE m.msg_id=t.msg_id AND (m.label_id=? OR {sql}))")
            self.params += [value] + params

    def summary(self, a):
        return {**super().summary(a), "label": a.label, "from": a.from_address,
                "thread": a.thread, "include_removed": a.include_removed}


# Where a reply's quoted history or the signature starts: the rest of the body is not this mail's own text.
_CUT = re.compile(r"(-- |On .{1,300} wrote:|.{1,300}님이 작성:|-{2,} ?(Original Message|원본 메시지) ?-{2,})\s*", re.I)


def mail_paragraphs(body):
    """A mail body's own paragraphs: no quoted (>) lines, nothing from a reply header or the signature on."""
    kept = []
    for line in body.splitlines():
        if _CUT.fullmatch(line):
            break
        if not line.lstrip().startswith(">"):
            kept.append(line)
    return [p.strip() for p in re.split(r"\n\s*\n", "\n".join(kept)) if p.strip()]


class GmailSearch(drv.SearchDriver):
    name, table, fts_table = NAME, "msgs", "msgs_fts"
    sort_cols = ("ts_ms", "msg_id")
    group_modes = {"senders": "t.sender", "threads": "t.thread_id", "labels": "m.label_id"}
    in_page_key, page_group = "threads_in_page", "thread_id"
    hits_noun, browse_hint, Filters = "messages", "give a query or --label/--from/--thread/--since/--until", Filters
    key_col, container_col, container_src = "key", "msg_id", "states"

    def chunk_units(self, con, container):
        r = con.execute("SELECT key, ts, subject, sender, body_text, attachments FROM msgs WHERE msg_id=? AND deleted=0",
                        (container,)).fetchone()
        if r is None:
            return "", []
        units = [(r["key"], r["ts"], p, False) for p in mail_paragraphs(r["body_text"])]
        if r["attachments"]:
            units.append((r["key"], r["ts"], "attachments: " + ", ".join(r["attachments"].splitlines()), False))
        return f"{r['subject']} / {r['sender']}", units or [(r["key"], r["ts"], r["subject"], False)]

    def check_args(self, a):
        if a.thread and gm_id(a.thread) is None:
            raise common.UsageError("--thread needs a Gmail hex id")

    def parse_cursor(self, value, flag, before=True):
        parts = value.split(".")
        if len(parts) != 2 or not parts[0].isascii() or not parts[0].isdigit() \
                or len(parts[0]) > 19 or int(parts[0]) >= 2 ** 63 or gm_id(parts[1]) is None:
            raise common.UsageError(f"{flag}: expected ts_ms.hex_id")
        return int(parts[0]), parts[1]

    def hit(self, r, tz, marks, width):
        return {k: r[k] for k in ("id", "key", "msg_id", "thread_id", "ts", "ts_ms", "sender", "subject", "recipients", "body_text", "attachments")} | {
            "deleted": bool(r["deleted"]), "text": r["message"], "snippet": ti.highlight(r["message"], marks, width),
            "date": common.fmt_ts(r["ts"], tz), "cursor": self.cursor(r)}

    def context(self, con, r, n, a, tz):
        hide = "" if a.include_deleted else " AND deleted=0"
        before = con.execute("SELECT * FROM msgs WHERE thread_id=? AND (ts_ms,msg_id)<(?,?)" + hide +
                             " ORDER BY ts_ms DESC,msg_id DESC LIMIT ?", (r["thread_id"], r["ts_ms"], r["msg_id"], n)).fetchall()
        after = con.execute("SELECT * FROM msgs WHERE thread_id=? AND (ts_ms,msg_id)>(?,?)" + hide +
                            " ORDER BY ts_ms,msg_id LIMIT ?", (r["thread_id"], r["ts_ms"], r["msg_id"], n)).fetchall()
        return [{**self.hit(v, tz, [], a.width), "hit": v["id"] == r["id"]} for v in [*reversed(before), r, *after]]

    def aggregate(self, con, a, q, flt, tz):
        if a.mode != "labels":
            return super().aggregate(con, a, q, flt, tz)
        body, params = self.body(q, flt)
        total = con.execute(f"SELECT count(*) {body}", params).fetchone()[0]
        body = body.replace(" WHERE ", " JOIN membership m ON m.msg_id=t.msg_id WHERE ", 1)
        rows = con.execute(f"SELECT m.label_id AS gid,count(*) AS n,min(t.ts) AS f,max(t.ts) AS l {body} GROUP BY gid ORDER BY n DESC,gid LIMIT ?", params + [a.limit]).fetchall()
        return {"mode": "labels", "total": total, "rows": [self.group_row(con, r, "labels", tz) for r in rows]}

    def group_row(self, con, r, mode, tz):
        label = r["gid"]
        if mode == "labels":
            row = con.execute("SELECT name FROM labels WHERE label_id=?", (label,)).fetchone()
            label = row[0] if row else label
        return {"id": r["gid"], "label": label, "n": r["n"],
                "first": common.fmt_ts(r["f"], tz, "%Y-%m-%d") if r["f"] else "",
                "last": common.fmt_ts(r["l"], tz, "%Y-%m-%d") if r["l"] else ""}

    def group_label(self, r, mode):
        return r["label"], "id=" + r["id"]

    def render_rest(self, res, a, tz):
        print(f"gmail: {res['total']} {res['mode']} result(s)")
        for row in res["rows"]:
            print(json.dumps(row, ensure_ascii=False))


DRIVER = GmailSearch(SPEC)


def run(a):
    return DRIVER.run(a)
