# -*- coding: utf-8 -*-
"""Mattermost raw search: the index <data_root>/index/mattermost.sqlite and the `manage.py search mattermost` command.

  python -s -B manage.py search mattermost --config CFG "배포 일정"
  python -s -B manage.py search mattermost --config CFG 재연결 --channel 개발 --since 2024-10 --context 3
  python -s -B manage.py search mattermost --config CFG 면접 --mode timeline     (also count / senders / channels)
  python -s -B manage.py search mattermost --config CFG --thread <post_id>        (the root and every reply, in order)
  python -s -B manage.py search mattermost --config CFG --channel 개발 --since 2024-10-03 --until 2024-10-03   (browse)
  python -s -B manage.py search mattermost --config CFG --list-channels

WHAT IS INDEXED (extractor "matter-1")
--------------------------------------
Ledger records of origin mm_archive (SPEC 4, 7.4). One row per post KEY mm:post:<id>, folded in ledger order;
a post can have several versions (SPEC 8.2 / Q3: an edit, a new reply_count, a move to another channel file),
and the latest put (highest rev) is the post as it stands:

* Text (kb build_corpus post_text rule): `message` stripped; else the props.attachments texts (pretext, title,
  text, fallback -- each once -- then every field as "title value"), joined by newlines; else the file names of
  metadata.files (mattertok's slim `_files` too), joined by spaces. body_from says which: message / attachment
  / files ('' when the post has no text). File names are indexed like any text. When the latest version has
  no text (a deleted post whose message the server emptied), the last text any version had is kept, so a
  deleted post still shows what was captured before -- with --include-deleted.
* edited: some version has edit_at > 0, or a version's text differed from the text before it. A new version
  with the same text (reply_count went up, the channel file moved) is not an edit.
* deleted: the latest version has delete_at > 0. system: its type starts with "system_" (joins, leaves,
  header changes). Both are hidden unless --include-deleted / --include-system.
* gone: the latest record is not a put (`absent` / `purged`, which mm_archive does not write today; a purge
  also drops the text). The row stays, flagged [gone].
* channel_id, user_id, root_id, type, reply_count, file_ids, create_at: from the latest version as they are.
  The channel is the post's own channel_id (Q11), not the file it was read from.
* sender: the username of the user record mm:user:<user_id> ('' when there is none; the user id is shown
  instead). The users table also keeps first/last name and nickname, which --sender matches and the senders
  mode prints. channel: the `display` of the channel record mm:ch:<channel_id> (else its channel_name), team:
  its team_name. Renames relabel rows already indexed.
* Facets: channels(channel_id, label, name, team, ch_type, n, senders, first_ts, last_ts, n_deleted, n_system,
  n_gone), n = rows that are neither deleted nor system posts.
* Not indexed, only counted (counts table): file records (the post carries the same metadata), bad keys, odd
  values.

Names, nicknames, channel labels and file names are printed as they are -- this tool does no masking or
pseudonymisation.
"""
import json
import re
import sys
from collections import Counter

from rawlayer.capture.canon import as_int
from rawlayer.search import common
from rawlayer.search import search as drv
from rawlayer.search import textindex as ti
from rawlayer.search.common import UsageError

NAME = "mattermost"
HELP = "search Mattermost posts (index: <data_root>/index/mattermost.sqlite)"
ORIGIN = "mm_archive"
OPS = ("put", "absent", "purged")
_I64 = 2 ** 63
_KEY = re.compile(r"^mm:(post|file|ch|user):(.+)$", re.S)
_CURSOR = re.compile(r"^([0-9]{1,15})\.(\S{1,128})$")

SCHEMA = """
CREATE TABLE matter(
  id          INTEGER PRIMARY KEY,         -- internal; not chronological (rows are added as the ledger grows)
  post_id     TEXT NOT NULL UNIQUE,
  rev         INTEGER NOT NULL DEFAULT 0,  -- rev of the record folded last
  seq         INTEGER NOT NULL DEFAULT 0,  -- ledger seq of that record
  held        INTEGER NOT NULL DEFAULT 0,  -- the latest record is a put
  channel_id  TEXT NOT NULL DEFAULT '',
  channel     TEXT NOT NULL DEFAULT '',    -- channel label ('' unknown; show channel_id)
  team        TEXT NOT NULL DEFAULT '',
  ts          INTEGER NOT NULL DEFAULT 0,  -- create_at // 1000; 0 = unknown
  ts_ms       INTEGER NOT NULL DEFAULT 0,  -- create_at (ms): the ordering key, with post_id as the tie
  sender      TEXT NOT NULL DEFAULT '',    -- username ('' unknown; show user_id)
  user_id     TEXT NOT NULL DEFAULT '',
  type        TEXT NOT NULL DEFAULT '',    -- '' for an ordinary post, else the post type (system_*, ...)
  body_from   TEXT NOT NULL DEFAULT '',    -- message | attachment | files | '' (no text)
  root_id     TEXT NOT NULL DEFAULT '',    -- '' for a top-level post, else the thread root's post_id
  reply_count INTEGER NOT NULL DEFAULT 0,
  file_ids    TEXT NOT NULL DEFAULT '',    -- JSON array; '' when none
  files       TEXT NOT NULL DEFAULT '',    -- JSON array of file names; '' when none
  edited      INTEGER NOT NULL DEFAULT 0,
  deleted     INTEGER NOT NULL DEFAULT 0,
  system      INTEGER NOT NULL DEFAULT 0,
  gone        INTEGER NOT NULL DEFAULT 0,
  message     TEXT NOT NULL DEFAULT '',
  ng          TEXT NOT NULL DEFAULT ''     -- n-gram shadow text (textindex.ng_text), indexed by matter_fts
);
CREATE INDEX matter_ctx ON matter(channel_id, ts_ms, post_id);
CREATE INDEX matter_ts ON matter(ts_ms, post_id);
CREATE INDEX matter_user ON matter(user_id);
CREATE INDEX matter_root ON matter(root_id);
CREATE TABLE ch_src(
  channel_id TEXT PRIMARY KEY, rev INTEGER NOT NULL, seq INTEGER NOT NULL, held INTEGER NOT NULL,
  label TEXT NOT NULL, name TEXT NOT NULL, team TEXT NOT NULL, ch_type TEXT NOT NULL) WITHOUT ROWID;
CREATE TABLE users(
  user_id TEXT PRIMARY KEY, rev INTEGER NOT NULL, seq INTEGER NOT NULL, held INTEGER NOT NULL,
  username TEXT NOT NULL, name TEXT NOT NULL, nickname TEXT NOT NULL) WITHOUT ROWID;
CREATE TABLE channels(
  channel_id TEXT PRIMARY KEY, label TEXT NOT NULL, name TEXT NOT NULL, team TEXT NOT NULL, ch_type TEXT NOT NULL,
  n INTEGER NOT NULL, senders INTEGER NOT NULL, first_ts INTEGER, last_ts INTEGER, n_deleted INTEGER NOT NULL,
  n_system INTEGER NOT NULL, n_gone INTEGER NOT NULL) WITHOUT ROWID;
"""

TRIGGERS = common.fts_triggers("matter", "matter_fts")

# labels of a row from the user / channel records as they are now (both build modes use exactly this)
_LABELS = ("sender = coalesce((SELECT u.username FROM users u WHERE u.user_id = matter.user_id), ''), "
           "channel = coalesce((SELECT c.label FROM ch_src c WHERE c.channel_id = matter.channel_id), ''), "
           "team = coalesce((SELECT c.team FROM ch_src c WHERE c.channel_id = matter.channel_id), '')")
_POST_COLS = ("rev", "seq", "held", "channel_id", "ts", "ts_ms", "user_id", "type", "body_from", "root_id",
              "reply_count", "file_ids", "files", "edited", "deleted", "system", "gone")

_is_int = common.is_int
_clean = common.clean


def _s(v):
    """A stripped str, or '' for anything that is not a str."""
    return _clean(v.strip()) if isinstance(v, str) else ""


def file_names(native):
    """File names of a post: metadata.files[].name, else (mattertok's slim format) _files[].name."""
    md = native.get("metadata")
    files = md.get("files") if isinstance(md, dict) else None
    if not isinstance(files, list) or not files:
        files = native.get("_files")
    if not isinstance(files, list):
        return []
    return [n for n in (_s(f.get("name")) for f in files if isinstance(f, dict)) if n]


def post_text(native, counts=None):
    """(text, body_from) of a post -- the knowledge-base corpus rule (build_corpus.post_text), made total:
    values of the wrong type are skipped (a non-str message is counted as body_other)."""
    msg = native.get("message")
    if isinstance(msg, str):
        if msg.strip():
            return _clean(msg.strip()), "message"
    elif msg is not None and counts is not None:
        counts["body_other"] += 1
    parts = []
    props = native.get("props")
    atts = props.get("attachments") if isinstance(props, dict) else None
    for att in atts if isinstance(atts, list) else ():
        if not isinstance(att, dict):
            continue
        for k in ("pretext", "title", "text", "fallback"):
            v = _s(att.get(k))
            if v and v not in parts:
                parts.append(v)
        fields = att.get("fields")
        for fld in fields if isinstance(fields, list) else ():
            if isinstance(fld, dict):
                v = " ".join(str(fld.get(k) or "").strip() for k in ("title", "value")).strip()
                if v:
                    parts.append(_clean(v))
    if parts:
        return "\n".join(parts), "attachment"
    names = file_names(native)
    if names:
        return " ".join(names), "files"
    return "", ""


def _ts_ms(ts_ms, create_at):
    """create_at in ms when it is a plausible epoch (2001..2286), else 0."""
    for v in (ts_ms, as_int(create_at)):
        if _is_int(v) and 1_000_000_000_000 <= v < 10_000_000_000_000:
            return v
    return 0


def _ident(v):
    """An id field (channel_id, user_id, root_id): a str, else ''."""
    return _clean(v) if isinstance(v, str) else ""


def _jlist(items):
    return json.dumps(items, ensure_ascii=False) if items else ""


class MatterApplier:
    def __init__(self, con, full):
        self.con, self.full = con, full
        self.counts = Counter()
        self.touched = set()            # matter.id of posts a record changed (incremental mode)
        self.channels = set()           # channel ids whose facet may have changed
        self.relabel_users = set()
        self.relabel_channels = set()

    # ---- fold
    def record(self, rec, seq):
        if not isinstance(rec, dict):
            self.counts["bad_record"] += 1
            return
        origin, op, rev, key = rec.get("origin"), rec.get("op"), rec.get("rev"), rec.get("key")
        if origin != ORIGIN or op not in OPS or not _is_int(rev) or not isinstance(key, str):
            self.counts["bad_record"] += 1
            return
        m = _KEY.fullmatch(key)
        if m is None or rec.get("kind") != m.group(1):
            self.counts["bad_key"] += 1
            return
        kind, ident = m.group(1), _clean(m.group(2))
        if kind == "post":
            self._post(rec, seq, op, rev, ident)
        elif kind == "ch":
            self._ch(rec, seq, op, rev, ident)
        elif kind == "user":
            self._user(rec, seq, op, rev, ident)
        else:
            self.counts["skipped_file"] += 1

    @staticmethod
    def _native(rec, counts):
        native = rec.get("native")
        if not isinstance(native, dict):
            counts["bad_native"] += 1
            return {}
        return native

    def _post(self, rec, seq, op, rev, pid):
        con = self.con
        cur = con.execute("SELECT id, rev, channel_id, message, edited, ts_ms, user_id, type, body_from, root_id, "
                          "reply_count, file_ids, files, deleted, system FROM matter WHERE post_id=?",
                          (pid,)).fetchone()
        if cur is not None and rev <= cur[1]:
            self.counts["stale_rev"] += 1
            return
        if cur is None:
            mid = con.execute("INSERT INTO matter(post_id) VALUES(?)", (pid,)).lastrowid
            old_ch, old_msg, edited = "", "", 0
            keep = (0, "", "", "", "", 0, "", "", 0, 0)
        else:
            mid, _rev, old_ch, old_msg, edited = cur[:5]
            keep = cur[5:]
        msg = old_msg
        if op == "put":
            native = self._native(rec, self.counts)
            text, body_from = post_text(native, self.counts)
            ts_ms = _ts_ms(rec.get("ts_ms"), native.get("create_at"))
            if ts_ms == 0:
                self.counts["ts_unknown"] += 1
            channel_id = _ident(native.get("channel_id"))
            if not channel_id:
                self.counts["no_channel"] += 1
            typ = _s(native.get("type"))
            rc = as_int(native.get("reply_count"))
            rc = rc if rc is not None and 0 <= rc < _I64 else 0
            fids = native.get("file_ids")
            fids = [_clean(f) for f in fids if isinstance(f, str)] if isinstance(fids, list) else []
            names = file_names(native)
            edit_at, delete_at = as_int(native.get("edit_at")), as_int(native.get("delete_at"))
            if edit_at is not None and edit_at > 0:
                edited = 1
            if text:
                if old_msg and old_msg != text:
                    edited = 1
                msg = text
            else:
                body_from = keep[3]          # the kept text's origin
            vals = (rev, seq, 1, channel_id, ts_ms // 1000, ts_ms, _ident(native.get("user_id")), typ, body_from,
                    _ident(native.get("root_id")), rc, _jlist(fids), _jlist(names), edited,
                    int(delete_at is not None and delete_at > 0), int(typ.startswith("system_")), 0)
        else:
            ts_ms, user_id, typ, body_from, root_id, rc, fids, names, deleted, system = keep
            if op == "purged":
                msg, body_from, names = "", "", ""
            channel_id = old_ch
            vals = (rev, seq, 0, channel_id, ts_ms // 1000, ts_ms, user_id, typ, body_from, root_id, rc, fids, names,
                    edited, deleted, system, 1)
        sets = ", ".join(c + "=?" for c in _POST_COLS)
        if msg != old_msg:
            con.execute(f"UPDATE matter SET {sets}, message=?, ng=? WHERE id=?", vals + (msg, ti.ng_text(msg), mid))
        else:
            con.execute(f"UPDATE matter SET {sets} WHERE id=?", vals + (mid,))
        if not self.full:
            self.touched.add(mid)
            self.channels.update((old_ch, channel_id) if cur is not None else (channel_id,))

    def _ch(self, rec, seq, op, rev, cid):
        con = self.con
        cur = con.execute("SELECT rev, label, name, team, ch_type FROM ch_src WHERE channel_id=?", (cid,)).fetchone()
        if cur is not None and rev <= cur[0]:
            self.counts["stale_rev"] += 1
            return
        label, name, team, ch_type = cur[1:] if cur else ("", "", "", "")
        if op == "put":
            native = self._native(rec, self.counts)
            name, team, ch_type = _s(native.get("channel_name")), _s(native.get("team_name")), _s(native.get("type"))
            label = _s(native.get("display")) or name
        elif op == "purged":
            label = name = team = ch_type = ""
        con.execute("INSERT OR REPLACE INTO ch_src(channel_id, rev, seq, held, label, name, team, ch_type) "
                    "VALUES(?,?,?,?,?,?,?,?)", (cid, rev, seq, 1 if op == "put" else 0, label, name, team, ch_type))
        if not self.full:
            self.channels.add(cid)
            if cur is None or (cur[1], cur[3]) != (label, team):
                self.relabel_channels.add(cid)

    def _user(self, rec, seq, op, rev, uid):
        con = self.con
        cur = con.execute("SELECT rev, username, name, nickname FROM users WHERE user_id=?", (uid,)).fetchone()
        if cur is not None and rev <= cur[0]:
            self.counts["stale_rev"] += 1
            return
        username, name, nickname = cur[1:] if cur else ("", "", "")
        if op == "put":
            native = self._native(rec, self.counts)
            username, nickname = _s(native.get("username")), _s(native.get("nickname"))
            name = " ".join(p for p in (_s(native.get("first_name")), _s(native.get("last_name"))) if p)
        elif op == "purged":
            username = name = nickname = ""
        con.execute("INSERT OR REPLACE INTO users(user_id, rev, seq, held, username, name, nickname) "
                    "VALUES(?,?,?,?,?,?,?)", (uid, rev, seq, 1 if op == "put" else 0, username, name, nickname))
        if not self.full and (cur is None or cur[1] != username):
            self.relabel_users.add(uid)

    # ---- derive
    def end_manifest(self, seq):
        common.bump_counts(self.con, self.counts)
        self.counts.clear()
        if self.full:
            return
        con = self.con
        for mid in sorted(self.touched):
            con.execute(f"UPDATE matter SET {_LABELS} WHERE id=?", (mid,))
        for uid in sorted(self.relabel_users):
            r = con.execute("SELECT username FROM users WHERE user_id=?", (uid,)).fetchone()
            name = r[0] if r else ""
            con.execute("UPDATE matter SET sender=? WHERE user_id=? AND sender<>?", (name, uid, name))
        for cid in sorted(self.relabel_channels):
            r = con.execute("SELECT label, team FROM ch_src WHERE channel_id=?", (cid,)).fetchone()
            label, team = r if r else ("", "")
            con.execute("UPDATE matter SET channel=?, team=? WHERE channel_id=? AND (channel<>? OR team<>?)",
                        (label, team, cid, label, team))
        for cid in sorted(self.channels):
            self._facet(cid)
        self.touched.clear()
        self.channels.clear()
        self.relabel_users.clear()
        self.relabel_channels.clear()

    def finish(self):
        con = self.con
        con.execute(f"UPDATE matter SET {_LABELS}")
        chans = {r[0] for r in con.execute("SELECT DISTINCT channel_id FROM matter")}
        chans |= {r[0] for r in con.execute("SELECT channel_id FROM ch_src")}
        for cid in sorted(chans):
            self._facet(cid)

    def _facet(self, cid):
        con = self.con
        rows, nd, nsys, ngone = con.execute(
            "SELECT count(*), coalesce(sum(deleted), 0), coalesce(sum(system), 0), coalesce(sum(gone), 0) "
            "FROM matter WHERE channel_id=?", (cid,)).fetchone()
        src = con.execute("SELECT label, name, team, ch_type FROM ch_src WHERE channel_id=?", (cid,)).fetchone()
        if rows == 0 and src is None:          # every post moved away and no channel record: as a rebuild has it
            con.execute("DELETE FROM channels WHERE channel_id=?", (cid,))
            return
        n, senders, first, last = con.execute(
            "SELECT count(*), count(DISTINCT user_id), MIN(CASE WHEN ts > 0 THEN ts END), "
            "MAX(CASE WHEN ts > 0 THEN ts END) FROM matter WHERE channel_id=? AND deleted=0 AND system=0",
            (cid,)).fetchone()
        label, name, team, ch_type = src if src else ("", "", "", "")
        con.execute("INSERT OR REPLACE INTO channels(channel_id, label, name, team, ch_type, n, senders, first_ts, "
                    "last_ts, n_deleted, n_system, n_gone) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (cid, label, name, team, ch_type, n, senders, first, last, nd, nsys, ngone))


class MatterIndex(common.SourceIndex):
    name = NAME
    origins = frozenset((ORIGIN,))
    schema_version = "matter-1"
    extractor_version = "matter-1"
    fts_tables = ("matter_fts",)

    def create_tables(self, con):
        con.executescript(SCHEMA)
        con.execute(ti.fts_ddl("matter"))

    def create_triggers(self, con):
        common.create_triggers(con, "matter", "matter_fts")

    def applier(self, con, full):
        return MatterApplier(con, full)

    def stats(self, con):
        rows, deleted, system, gone = con.execute(
            "SELECT count(*), coalesce(sum(deleted), 0), coalesce(sum(system), 0), coalesce(sum(gone), 0) "
            "FROM matter").fetchone()
        out = {"rows": rows, "deleted": deleted, "system": system, "gone": gone,
               "channels": con.execute("SELECT count(*) FROM channels").fetchone()[0],
               "users": con.execute("SELECT count(*) FROM users").fetchone()[0]}
        out.update(common.read_counts(con))
        return out


SPEC = MatterIndex()


# ================================================================================================ search

def _jl(s):
    try:
        v = json.loads(s) if s else []
    except ValueError:
        return []
    return v if isinstance(v, list) else []


def _hide_sql(a):
    return ("" if a.include_deleted else " AND deleted = 0") + ("" if a.include_system else " AND system = 0")


def _place(h):
    return h.get("channel") or h.get("channel_id") or "?"



def add_arguments(p):
    common.add_search_arguments(p, ["hits", "count", "timeline", "senders", "channels"])
    p.add_argument("--channel", action="append", default=[], metavar="C",
                   help="substring of the channel label or URL name, or an exact channel id (repeat for several)")
    p.add_argument("--team", help="substring of the team name")
    p.add_argument("--include-system", action="store_true",
                   help="also show system posts (joins, leaves, header changes; hidden by default)")
    p.add_argument("--thread", metavar="POST_ID",
                   help="the whole thread containing this post: the root, then every reply in time order "
                        "(query and selection filters do not apply)")
    p.add_argument("--before", metavar="CUR", help="browse: the page older than this cursor (<ts_ms>.<post_id>)")
    p.add_argument("--after", metavar="CUR", help="browse: the page newer than this cursor")
    p.add_argument("--list-channels", action="store_true",
                   help="channels by post count (with --channel / --team: matching ones)")


class _Filters(drv.Filters):
    range_col = "ts_ms"
    range_scale = 1000
    until_pad = 999

    def __init__(self, a, tz):
        super().__init__(a, tz)
        self.selector = self.selector or bool(a.channel or a.team)
        if a.channel:
            ors = []
            for c in a.channel:
                lab, p1 = common.like("t.channel", c)
                nam, p2 = common.like("name", c)
                ors.append(f"({lab} OR t.channel_id = ? OR "
                           f"t.channel_id IN (SELECT channel_id FROM channels WHERE {nam}))")
                self.params += p1 + [c.strip()] + p2
            self.where.append("(" + " OR ".join(ors) + ")")
        if a.team:
            sql, ps = common.like("t.team", a.team)
            self.where.append(sql)
            self.params += ps
        if a.sender:
            snd, p1 = common.like("t.sender", a.sender)
            nam, p2 = common.like("name", a.sender)
            nick, p3 = common.like("nickname", a.sender)
            self.where.append(f"({snd} OR t.user_id = ? OR "
                              f"t.user_id IN (SELECT user_id FROM users WHERE {nam} OR {nick}))")
            self.params += p1 + [a.sender.strip()] + p2 + p3
        if not a.include_system:
            self.where.append("t.system = 0")

    def summary(self, a):
        out = {"channel": list(a.channel), "team": a.team}
        out.update(super().summary(a))
        out["include_system"] = bool(a.include_system)
        return out


_THREAD_INAPPLICABLE = (("query", "QUERY"), ("sender", "--sender"), ("since", "--since"), ("until", "--until"),
                        ("channel", "--channel"), ("team", "--team"), ("before", "--before"), ("after", "--after"))


class MatterSearch(drv.SearchDriver):
    name = NAME
    table = "matter"
    fts_table = "matter_fts"
    sort_cols = ("ts_ms", "post_id")
    group_modes = {"senders": "t.user_id", "channels": "t.channel_id"}
    list_flag = "list_channels"
    in_page_key = "channels_in_page"
    page_group = "channel_id"
    hits_noun = "posts"
    browse_hint = ("give a query, or --channel / --team / --sender / --since / --until to browse, "
                   "or --thread / --list-channels")
    Filters = _Filters
    key_col, container_col, container_src, chunk_gap_s = "post_id", "channel_id", "ch_src", 1800

    def chunk_units(self, con, container):
        rows = con.execute("SELECT post_id, ts, team, channel, sender, user_id, message FROM matter WHERE channel_id=? "
                           "AND deleted=0 AND system=0 AND message!='' ORDER BY ts_ms, post_id", (container,)).fetchall()
        label = " › ".join(p for p in (rows[-1]["team"], rows[-1]["channel"] or container) if p) if rows else ""
        return label, [(r["post_id"], r["ts"], f"{r['sender'] or r['user_id']}: {r['message']}", False) for r in rows]

    def check_args(self, a):
        if a.thread is not None and a.list_channels:
            raise UsageError("give --thread or --list-channels, not both")
        if a.thread is not None and not a.thread.strip():
            raise UsageError("--thread needs a post id")
        return "thread" if a.thread is not None else None

    def parse_cursor(self, s, flag, before=True):
        """'<ts_ms>.<post_id>' keyset cursor -> (ts_ms, post_id)."""
        m = _CURSOR.fullmatch((s or "").strip())
        if not m:
            raise UsageError(f"{flag} {s!r}: expected a cursor like 1727758980123.abc123 (printed with each page)")
        return int(m.group(1)), m.group(2)

    def hit(self, r, tz, marks, width):
        msg = r["message"]
        return {"id": r["id"], "key": f"mm:post:{r['post_id']}", "post_id": r["post_id"],
                "channel_id": r["channel_id"],
                "channel": r["channel"], "team": r["team"], "ts": r["ts"], "ts_ms": r["ts_ms"],
                "date": common.fmt_ts(r["ts"], tz), "sender": r["sender"], "user_id": r["user_id"],
                "type": r["type"],
                "body_from": r["body_from"], "root_id": r["root_id"], "reply_count": r["reply_count"],
                "files": _jl(r["files"]), "file_ids": _jl(r["file_ids"]), "edited": bool(r["edited"]),
                "deleted": bool(r["deleted"]), "system": bool(r["system"]), "gone": bool(r["gone"]),
                "cursor": self.cursor(r), "snippet": ti.highlight(msg, marks, width) if msg else self.placeholder(r),
                "text": msg}

    def context(self, con, r, n, a, tz):
        """n posts before and after a hit in its channel, by (ts_ms, post_id) keyset; the hit itself marked."""
        hide = _hide_sql(a)
        key = (r["channel_id"], r["ts_ms"], r["ts_ms"], r["post_id"])
        before = con.execute(
            "SELECT * FROM matter WHERE channel_id = ? AND (ts_ms < ? OR (ts_ms = ? AND post_id < ?))"
            f"{hide} ORDER BY ts_ms DESC, post_id DESC LIMIT ?", key + (n,)).fetchall()
        after = con.execute(
            "SELECT * FROM matter WHERE channel_id = ? AND (ts_ms > ? OR (ts_ms = ? AND post_id > ?))"
            f"{hide} ORDER BY ts_ms, post_id LIMIT ?", key + (n,)).fetchall()
        out = []
        for c in list(reversed(before)) + [r] + list(after):
            text = common.flat(c["message"]) or self.placeholder(c)
            out.append({"id": c["id"], "post_id": c["post_id"], "ts": c["ts"], "date": common.fmt_ts(c["ts"], tz),
                        "sender": c["sender"], "user_id": c["user_id"], "root_id": c["root_id"],
                        "text": common.clip(text, a.width), "deleted": bool(c["deleted"]),
                        "system": bool(c["system"]),
                        "gone": bool(c["gone"]), "edited": bool(c["edited"]), "hit": c["id"] == r["id"]})
        return out

    def placeholder(self, r):
        return "(" + (r["type"] + ", " if r["type"] else "") + "no text)"

    def who(self, row):
        return row.get("sender") or row.get("user_id") or "?"

    def flags(self, h):
        out = "".join(f" [{k}]" for k in ("deleted", "system", "gone", "edited") if h.get(k))
        if h.get("root_id"):
            out = " [reply]" + out
        files = h.get("files") or []
        if files and h.get("body_from") != "files":
            out += f" [+{len(files)} file{'s' if len(files) > 1 else ''}]"
        return out

    def group_row(self, con, r, mode, tz):
        span = {"first": common.fmt_ts(r["f"], tz, "%Y-%m-%d") if r["f"] else "",
                "last": common.fmt_ts(r["l"], tz, "%Y-%m-%d") if r["l"] else ""}
        if mode == "senders":
            u = con.execute("SELECT username, name, nickname FROM users WHERE user_id=?", (r["gid"],)).fetchone()
            return {"user_id": r["gid"], "label": (u["username"] if u else "") or r["gid"] or "?",
                    "name": u["name"] if u else "", "nickname": u["nickname"] if u else "", "n": r["n"], **span}
        c = con.execute("SELECT label, team FROM channels WHERE channel_id=?", (r["gid"],)).fetchone()
        return {"channel_id": r["gid"], "label": (c["label"] if c else "") or r["gid"] or "?",
                "team": c["team"] if c else "", "n": r["n"], **span}

    def group_label(self, r, mode):
        if mode == "senders":
            extra = " / ".join(x for x in (r["name"], r["nickname"]) if x)
            return r["label"] + (f" ({extra})" if extra else ""), f"user={r['user_id']}"
        return r["label"] + (f" [{r['team']}]" if r["team"] else ""), f"channel={r['channel_id']}"

    def list_all(self, con, a, tz):
        where, params = [], []
        if a.channel:
            ors = []
            for c in a.channel:
                lab, p1 = common.like("label", c)
                nam, p2 = common.like("name", c)
                ors.append(f"({lab} OR {nam} OR channel_id = ?)")
                params += p1 + p2 + [c.strip()]
            where.append("(" + " OR ".join(ors) + ")")
        if a.team:
            sql, ps = common.like("team", a.team)
            where.append(sql)
            params += ps
        w = (" WHERE " + " AND ".join(where)) if where else ""
        total = con.execute(f"SELECT count(*) FROM channels{w}", params).fetchone()[0]
        rows = con.execute(f"SELECT * FROM channels{w} ORDER BY n DESC, channel_id LIMIT ?",
                           params + [a.limit]).fetchall()
        return {"mode": "list", "total": total, "rows": [
            {"channel_id": r["channel_id"], "label": r["label"] or r["channel_id"] or "?", "name": r["name"],
             "team": r["team"], "type": r["ch_type"], "n": r["n"], "senders": r["senders"],
             "n_deleted": r["n_deleted"],
             "n_system": r["n_system"], "n_gone": r["n_gone"],
             "first": common.fmt_ts(r["first_ts"], tz, "%Y-%m-%d") if r["first_ts"] else "",
             "last": common.fmt_ts(r["last_ts"], tz, "%Y-%m-%d") if r["last_ts"] else ""} for r in rows]}

    def extra_result(self, con, a, extra, query, tz):
        """The thread holding post a.thread: its root first, then every reply by (create_at, post_id). Selection
        filters do not apply (a thread cut down to one sender or one window reads like another conversation); they
        are reported on stderr. Deleted and system posts follow the usual hide convention, with a count."""
        given = {"query": query}
        skipped = [flag for attr, flag in _THREAD_INAPPLICABLE
                   if (given[attr] if attr in given else getattr(a, attr, None))]
        if a.mode != "hits":
            skipped.append("--mode " + a.mode)
        if a.context:
            skipped.append("--context")
        if skipped:
            common.note(f"{', '.join(skipped)} not applied -- --thread retrieves a whole thread as one unit; search "
                        f"with those instead")
        pid = a.thread.strip()
        seed = con.execute("SELECT * FROM matter WHERE post_id = ?", (pid,)).fetchone()   # deleted or not: it was named
        if seed is None:
            raise UsageError(f"no post {pid!r} in the {NAME} index")
        root = seed["root_id"] or seed["post_id"]
        rows = con.execute("SELECT * FROM matter WHERE post_id = ? OR root_id = ? "
                           "ORDER BY (post_id = ?) DESC, ts_ms, post_id", (root, root, root)).fetchall()
        root_found = any(r["post_id"] == root for r in rows)
        kept, hid_d, hid_s = [], 0, 0
        for r in rows:
            if r["deleted"] and not a.include_deleted:
                hid_d += 1
            elif r["system"] and not a.include_system:
                hid_s += 1
            else:
                kept.append(r)
        if hid_d:
            common.note(f"{hid_d} deleted post(s) hidden from this thread; --include-deleted shows them")
        if hid_s:
            common.note(f"{hid_s} system post(s) hidden from this thread; --include-system shows them")
        if not root_found:
            common.note("the thread's root post is not in the index (never archived); showing the replies")
        out = []
        for r in kept:
            h = self.hit(r, tz, [], a.width)
            h["snippet"] = common.clip(common.flat(r["message"]) or self.placeholder(r), a.width)
            h["root"] = r["post_id"] == root
            out.append(h)
        return {"mode": "thread", "root_id": root, "root_found": root_found, "channel_id": seed["channel_id"],
                "channel": seed["channel"], "total": len(out), "hidden_deleted": hid_d, "hidden_system": hid_s,
                "rows": out}

    def render_rest(self, res, a, tz):
        mode, rows, total = res["mode"], res["rows"], res["total"]
        out = sys.stdout.write
        if mode == "list":
            out(f"=== {NAME}: {total} channel(s) by post count ===\n")
            for r in rows:
                span = f"{r['first']}..{r['last']}" if r["first"] else ""
                team = f"[{r['team']}]" if r["team"] else ""
                out(f"  {r['n']:7,}  {common.pad(r['label'], 32)} {common.pad(team, 14)} {span:22} "
                    f"id={r['channel_id']}\n")
            return
        if mode == "thread":
            where = res["channel"] or res["channel_id"] or "?"
            out(f"=== {NAME}: thread {res['root_id']} in {where} ({total} post(s)) ===\n")
            if not rows:
                out("  (no posts)\n")
            for h in rows:
                mark = "" if h["root"] else "  ↳ reply"
                out(f"{h['date']:16}  @{self.who(h)}{mark}  #{h['post_id']}"
                    f"{''.join(f' [{k}]' for k in ('deleted', 'system', 'gone', 'edited') if h.get(k))}\n")
                for line in (h["text"].splitlines() or [self.placeholder(h)]):
                    out(f"    {line}\n")
                if h["files"] and h["body_from"] != "files":
                    out(f"    [files: {', '.join(h['files'])}]\n")
                out("\n")
            return
        if mode == "browse":
            page = res["page"]
            multi = res["channels_in_page"] > 1
            out(f"=== {NAME}: {total} post(s); this page {len(rows)} ===\n")
            if rows:
                out(f"  older: --before {page['before']}    newer: --after {page['after']}\n")
            else:
                out("  (no posts)\n")
            for h in rows:
                where = f" | {common.pad(_place(h), 20)}" if multi else ""
                text = common.flat(h["text"]) if h["text"] else h["snippet"]
                out(f"{h['date']:16} | {common.pad(self.who(h), 14)}{where} :: {text}{self.flags(h)}  #{h['post_id']}\n")
            return
        shown = len(rows)
        head = f"=== {NAME}: {total} match(es)"
        if total > shown:
            head += f" (showing {shown} best)"
        out(head + " ===\n")
        if not rows:
            out("  (no matches)\n")
        prefix = f"[{NAME}] "
        for h in rows:
            out(f"{prefix}{h['date']:16} | {common.pad(self.who(h), 18)} | {common.pad(_place(h), 26)} "
                f":: {h['snippet']}{self.flags(h)}\n")
            out(f"{' ' * len(prefix)}post={h['post_id']} root={h['root_id'] or '-'} channel={h['channel_id'] or '-'}"
                f"{' team=' + h['team'] if h['team'] else ''} cursor={h['cursor']}\n")
            for c in h.get("context", []):
                arrow = ">>" if c["hit"] else "  "
                out(f"     {arrow} {c['date'][5:]:11} {common.pad(self.who(c), 14)} {c['text']}"
                    f"{''.join(f' [{k}]' for k in ('deleted', 'system', 'gone') if c.get(k))}\n")


DRIVER = MatterSearch(SPEC)


def run(a):
    return DRIVER.run(a)
