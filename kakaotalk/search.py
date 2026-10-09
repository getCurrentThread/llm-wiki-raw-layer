# -*- coding: utf-8 -*-
"""KakaoTalk raw search: the index <data_root>/index/kakaotalk.sqlite and the `manage.py search kakaotalk` command.

  python -s -B manage.py search kakaotalk --config CFG "송금 계좌"
  python -s -B manage.py search kakaotalk --config CFG 회의 --room 스터디 --since 2024-10 --context 3
  python -s -B manage.py search kakaotalk --config CFG 면접 --mode timeline        (also count / senders / rooms)
  python -s -B manage.py search kakaotalk --config CFG --room 스터디                (browse: newest page first)
  python -s -B manage.py search kakaotalk --config CFG --room 스터디 --before 1727758980.12345.678
  python -s -B manage.py search kakaotalk --config CFG --list-rooms

WHAT IS INDEXED (extractor "kakao-4")
-------------------------------------
Ledger records of the three Kakao origins (backup_decrypted, edb, macdb -- kakao-4 dropped the retired
kakatok exports merged_out / live_out / decrypted_live: the app reads the client itself now).
One row per message KEY kakao:<chatId>:msg:<logId> across origins
(UNIQUE(chat_id, log_id), like the knowledge-base corpus), built from a per-origin fold `ksrc` that keeps the
latest record of every origin for every key:

* Text: from the highest-precedence origin whose LATEST put carries usable text, precedence
  edb > macdb > backup_decrypted (PRECEDENCE). Usable text = a str message with
  something besides whitespace, in a row that is not a delete-for-all tombstone. An origin whose latest put has
  no usable text (a tombstone, a row emptied by a local delete) does not outrank one that currently has text
  with the text it held before (ksrc.has_text). When no origin's latest put has usable text, the last usable text
  an origin put is kept, highest precedence first, holders before origins that dropped the key (so a message
  that was deleted for everyone, or deleted locally and emptied, still shows what was captured before -- with
  --include-deleted).
* origins: bitmask of the origins whose head is a put (bit i = PRECEDENCE[i]). `absent` (edb's two-phase
  absence) and `purged` clear the origin's bit. gone = no origin holds the key any more; the row stays, with
  its last text, flagged [gone] (shown by default).
* deleted: some origin's latest put has the 0x4000 bit in `type` (delete for everyone; the body is then a
  base64 blob, never indexed), a non-zero is_encrypt_delete_msg, or a non-zero `deleted` (local deletion).
  Deleted rows are hidden unless --include-deleted.
* edited: an origin put a usable text and later a different usable text for the same key.
* sender: author names come from the client's own user database -- TalkUserDB read beside the rooms (edb) and
  from the backup's decrypted copy (backup_decrypted), plus the macOS database's NTUser (derived.sender, when
  it is not just digits). The names are the accounts' display names (nickName; the owner's own alias for a
  friend only where the account has no nickname), the join the kakatok research documented; rows from room
  DBs without a named author carry only authorId. The authors table
  keeps, per author id, the name on the newest such message (sendAt, then ledger seq; the live client's names
  outrank the backup's at a tie). `sender` on each row is that name ('' when unknown; the author id is shown
  instead).
* room: title from room records (the client's chat-list database for edb rooms, the manifest titles for the
  backup's, macdb rooms): the newest put with a non-empty title (ledger seq; edb before macdb before
  backup_decrypted within one capture).
* Facets: rooms(chat_id, title, n, senders, first_ts, last_ts, n_deleted, n_gone), n = rows not deleted.
* Not indexed, only counted (counts table): hist, meta, attachment and schema records, bad keys, odd values.

The producer's type_name labels are known to be wrong (26 is a reply, not 'deleted'), so the integer type is
kept and labelled here (TYPE_NAMES) at display time only.

Names and room titles are printed as they are -- this tool does no masking or pseudonymisation.
"""
import hashlib
import re
import sys
from collections import Counter

from rawlayer.capture.canon import as_int, as_str
from rawlayer.search import common
from rawlayer.search import search as drv
from rawlayer.search import textindex as ti
from rawlayer.search.common import UsageError

NAME = "kakaotalk"
HELP = "search KakaoTalk messages (index: <data_root>/index/kakaotalk.sqlite)"
PRECEDENCE = ("edb", "macdb", "backup_decrypted")  # text, best first
RANK = {o: i for i, o in enumerate(PRECEDENCE)}
TITLE_ORDER = ("edb", "macdb", "backup_decrypted")  # ties within seq
TITLE_RANK = {RANK[o]: i for i, o in enumerate(TITLE_ORDER)}
NAME_RANK = {"edb": 1, "macdb": 1, "backup_decrypted": 0}   # name sources; the live client's names win a tie
DELETE_ALL = 0x4000
OPS = ("put", "absent", "purged")
SKIP_KINDS = ("hist", "meta", "attachment", "schema")
TYPE_NAMES = {0: "system", 1: "text", 2: "photo", 3: "video", 5: "audio", 6: "emoticon", 12: "schedule",
              14: "vote", 16: "map", 18: "file", 20: "emoticon", 26: "reply", 27: "photos", 51: "call",
              52: "join/leave", 71: "link", 72: "card"}
_MSG_KEY = re.compile(r"^kakao:([0-9]+):msg:(.+)$", re.S)
_ROOM_KEY = re.compile(r"^kakao:([0-9]+)$")
_LOG_NO = re.compile(r"^[0-9]{1,19}$")
_I64 = 2 ** 63

SCHEMA = """
CREATE TABLE kakao(
  id        INTEGER PRIMARY KEY,       -- internal; not chronological (rows are added as the ledger grows)
  chat_id   TEXT NOT NULL,
  log_id    TEXT NOT NULL,             -- exact (logIds exceed 2^53); emitted as a string
  log_no    INTEGER NOT NULL,          -- logId as int64 (0 if not representable): ordering tie
  ts        INTEGER NOT NULL DEFAULT 0, -- sendAt, unix seconds; 0 = unknown
  room      TEXT NOT NULL DEFAULT '',  -- room title ('' unknown)
  sender    TEXT NOT NULL DEFAULT '',  -- author name ('' unknown; show author_id)
  author_id TEXT,
  type      INTEGER,
  origins   INTEGER NOT NULL DEFAULT 0,-- bitmask over PRECEDENCE of origins holding a put
  source    TEXT NOT NULL DEFAULT '',  -- origin the text came from
  edited    INTEGER NOT NULL DEFAULT 0,
  deleted   INTEGER NOT NULL DEFAULT 0,
  gone      INTEGER NOT NULL DEFAULT 0,
  message   TEXT NOT NULL DEFAULT '',
  ng        TEXT NOT NULL DEFAULT '',  -- n-gram shadow text (textindex.ng_text), indexed by kakao_fts
  UNIQUE(chat_id, log_id)
);
CREATE INDEX kakao_ctx ON kakao(chat_id, ts, log_no);
CREATE INDEX kakao_ts ON kakao(ts, log_no);
CREATE INDEX kakao_author ON kakao(author_id);
CREATE TABLE ksrc(
  mid INTEGER NOT NULL, origin INTEGER NOT NULL, rev INTEGER NOT NULL, seq INTEGER NOT NULL,
  held INTEGER NOT NULL, th BLOB, deleted INTEGER NOT NULL, ts INTEGER NOT NULL, author_id TEXT, type INTEGER,
  edited INTEGER NOT NULL,
  has_text INTEGER NOT NULL,           -- the origin's latest put carries usable text (th is that text)
  PRIMARY KEY(mid, origin)) WITHOUT ROWID;
CREATE TABLE texts(h BLOB PRIMARY KEY, message TEXT NOT NULL) WITHOUT ROWID;
CREATE TABLE room_src(
  chat_id TEXT NOT NULL, origin INTEGER NOT NULL, rev INTEGER NOT NULL, seq INTEGER NOT NULL,
  held INTEGER NOT NULL, title TEXT NOT NULL, PRIMARY KEY(chat_id, origin)) WITHOUT ROWID;
CREATE TABLE authors(
  author_id TEXT PRIMARY KEY, name TEXT NOT NULL, c_ts INTEGER NOT NULL, c_seq INTEGER NOT NULL,
  c_rank INTEGER NOT NULL, c_key TEXT NOT NULL) WITHOUT ROWID;
CREATE TABLE rooms(
  chat_id TEXT PRIMARY KEY, title TEXT NOT NULL, n INTEGER NOT NULL, senders INTEGER NOT NULL,
  first_ts INTEGER, last_ts INTEGER, n_deleted INTEGER NOT NULL, n_gone INTEGER NOT NULL) WITHOUT ROWID;
"""

TRIGGERS = common.fts_triggers("kakao", "kakao_fts")

_ROW_COLS = "ts, room, sender, author_id, type, origins, source, edited, deleted, gone"

_is_int = common.is_int
_clean = common.clean


def _nonzero(v):
    i = as_int(v)
    return i is not None and i != 0


def _text_hash(text):
    return hashlib.blake2b(text.encode("utf-8"), digest_size=16).digest()


def _ts(ts_ms, send_at):
    if _is_int(ts_ms) and 1_000_000_000_000 <= ts_ms < 10_000_000_000_000:
        return ts_ms // 1000
    s = as_int(send_at)
    return s if s is not None and 1_000_000_000 <= s < 10_000_000_000 else 0


class _Src:
    __slots__ = ("rank", "held", "th", "deleted", "ts", "author_id", "type", "edited", "has_text")

    def __init__(self, rank, held, th, deleted, ts, author_id, typ, edited, has_text):
        self.rank, self.held, self.th, self.deleted = rank, held, th, deleted
        self.ts, self.author_id, self.type, self.edited, self.has_text = ts, author_id, typ, edited, has_text


def derive(srcs):
    """Row fields from the per-origin states of one key (the rule in the module docstring). Pure."""
    srcs = sorted(srcs, key=lambda s: s.rank)
    holders = [s for s in srcs if s.held]

    def pick(pred):
        for group in (holders, srcs):
            for s in group:
                if pred(s):
                    return s
        return None

    t = next((s for s in holders if s.has_text), None) or pick(lambda s: s.th is not None)
    w = pick(lambda s: s.ts > 0)
    a = pick(lambda s: s.author_id is not None)
    y = pick(lambda s: s.type is not None)
    return {"th": t.th if t else None, "source": PRECEDENCE[t.rank] if t else "",
            "ts": w.ts if w else 0, "author_id": a.author_id if a else None, "type": y.type if y else None,
            "origins": sum(1 << s.rank for s in holders), "edited": int(any(s.edited for s in srcs)),
            "deleted": int(any(s.deleted for s in srcs)), "gone": int(not holders)}


class _Labels:
    """Author names and room titles as the fold has them now (cached for one derive pass)."""

    def __init__(self, con, preload=False):
        self.con = con
        self._a, self._t = {}, {}
        self.all = preload
        if preload:
            self._a = dict(con.execute("SELECT author_id, name FROM authors"))
            best = {}
            for chat, origin, seq, title in con.execute(
                    "SELECT chat_id, origin, seq, title FROM room_src WHERE title <> ''"):
                k = (seq, -TITLE_RANK.get(origin, 99))
                if chat not in best or k > best[chat][0]:
                    best[chat] = (k, title)
            self._t = {c: v[1] for c, v in best.items()}

    def author(self, aid):
        if aid is None:
            return ""
        if aid not in self._a and not self.all:
            r = self.con.execute("SELECT name FROM authors WHERE author_id=?", (aid,)).fetchone()
            self._a[aid] = r[0] if r else ""
        return self._a.get(aid, "")

    def title(self, chat):
        if chat not in self._t and not self.all:
            best = None
            for origin, seq, title in self.con.execute(
                    "SELECT origin, seq, title FROM room_src WHERE chat_id=? AND title <> ''", (chat,)):
                k = (seq, -TITLE_RANK.get(origin, 99))
                if best is None or k > best[0]:
                    best = (k, title)
            self._t[chat] = best[1] if best else ""
        return self._t.get(chat, "")


class KakaoApplier:
    def __init__(self, con, full):
        self.con, self.full = con, full
        self.counts = Counter()
        self.touched = set()
        self.title_chats = set()
        self.authors_changed = set()

    # ---- fold
    def record(self, rec, seq):
        if not isinstance(rec, dict):
            self.counts["bad_record"] += 1
            return
        kind, origin, op, rev, key = rec.get("kind"), rec.get("origin"), rec.get("op"), rec.get("rev"), rec.get("key")
        if origin not in RANK or op not in OPS or not _is_int(rev) or not isinstance(key, str):
            self.counts["bad_record"] += 1
            return
        if kind == "msg":
            self._msg(rec, seq, origin, op, rev, key)
        elif kind == "room":
            self._room(rec, seq, origin, op, rev, key)
        elif kind in SKIP_KINDS:
            self.counts["skipped_" + kind] += 1
        else:
            self.counts["skipped_other"] += 1

    def _mid(self, chat_id, log_id):
        r = self.con.execute("SELECT id FROM kakao WHERE chat_id=? AND log_id=?", (chat_id, log_id)).fetchone()
        if r:
            return r[0]
        log_no = int(log_id) if _LOG_NO.fullmatch(log_id) and int(log_id) < _I64 else 0
        return self.con.execute("INSERT INTO kakao(chat_id, log_id, log_no) VALUES(?,?,?)",
                                (chat_id, log_id, log_no)).lastrowid

    def _msg(self, rec, seq, origin, op, rev, key):
        m = _MSG_KEY.fullmatch(key)
        if not m:
            self.counts["bad_key"] += 1
            return
        mid = self._mid(m.group(1), _clean(m.group(2)))
        rank = RANK[origin]
        cur = self.con.execute("SELECT rev, held, th, deleted, ts, author_id, type, edited FROM ksrc "
                               "WHERE mid=? AND origin=?", (mid, rank)).fetchone()
        if cur is not None and rev <= cur[0]:
            self.counts["stale_rev"] += 1
            return
        _r, _h, th, deleted, ts, author, typ, edited = cur if cur else (0, 0, None, 0, 0, None, None, 0)
        has_text = 0
        if op == "put":
            native, derived = rec.get("native"), rec.get("derived")
            if not isinstance(native, dict):
                self.counts["bad_native"] += 1
                native = {}
            if not isinstance(derived, dict):
                derived = {}
            typ = as_int(native.get("type"))
            if typ is not None and not -_I64 <= typ < _I64:
                typ = None
            tomb = (typ is not None and typ & DELETE_ALL != 0) or _nonzero(native.get("is_encrypt_delete_msg"))
            deleted = int(tomb or _nonzero(native.get("deleted")))
            body = native.get("message")
            text = None
            if isinstance(body, str):
                if not tomb and body.strip():
                    text = _clean(body)
            elif body is not None and not (isinstance(body, dict) and "$b64" in body):
                self.counts["body_other"] += 1
            ts = _ts(rec.get("ts_ms"), native.get("sendAt"))
            author = as_str(native.get("authorId"))
            author = _clean(author) if author else None
            if text is not None:
                h = _text_hash(text)
                self.con.execute("INSERT OR IGNORE INTO texts(h, message) VALUES(?, ?)", (h, text))
                if th is not None and th != h:
                    edited = 1
                th = h
                has_text = 1
            if origin in NAME_RANK and author is not None:
                s = derived.get("sender")
                if isinstance(s, str):
                    s = _clean(s.strip())
                    if s and not s.isdigit():
                        self._name(author, s, (ts, seq, NAME_RANK[origin], key))
            held = 1
        else:
            held = 0
            if op == "purged":
                th = None
        self.con.execute("INSERT OR REPLACE INTO ksrc(mid, origin, rev, seq, held, th, deleted, ts, author_id, type, "
                         "edited, has_text) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                         (mid, rank, rev, seq, held, th, deleted, ts, author, typ, edited, has_text))
        if not self.full:
            self.touched.add(mid)

    def _name(self, author, name, cand):
        r = self.con.execute("SELECT name, c_ts, c_seq, c_rank, c_key FROM authors WHERE author_id=?",
                             (author,)).fetchone()
        if r is None or cand > tuple(r[1:]):
            self.con.execute("INSERT OR REPLACE INTO authors(author_id, name, c_ts, c_seq, c_rank, c_key) "
                             "VALUES(?,?,?,?,?,?)", (author, name) + cand)
            if not self.full and (r is None or r[0] != name):
                self.authors_changed.add(author)

    def _room(self, rec, seq, origin, op, rev, key):
        m = _ROOM_KEY.fullmatch(key)
        if not m:
            self.counts["bad_key"] += 1
            return
        chat, rank = m.group(1), RANK[origin]
        cur = self.con.execute("SELECT rev, title FROM room_src WHERE chat_id=? AND origin=?", (chat, rank)).fetchone()
        if cur is not None and rev <= cur[0]:
            self.counts["stale_rev"] += 1
            return
        title = cur[1] if cur else ""
        if op == "put":
            d = rec.get("derived")
            t = d.get("title") if isinstance(d, dict) else None
            title = _clean(t.strip()) if isinstance(t, str) else ""
        elif op == "purged":
            title = ""
        self.con.execute("INSERT OR REPLACE INTO room_src(chat_id, origin, rev, seq, held, title) VALUES(?,?,?,?,?,?)",
                         (chat, rank, rev, seq, 1 if op == "put" else 0, title))
        if not self.full:
            self.title_chats.add(chat)

    # ---- derive
    def end_manifest(self, seq):
        common.bump_counts(self.con, self.counts)
        self.counts.clear()
        if self.full:
            return
        con = self.con
        labels = _Labels(con)
        chats = set()
        for mid in sorted(self.touched):
            srcs = [_Src(*r) for r in con.execute(
                "SELECT origin, held, th, deleted, ts, author_id, type, edited, has_text FROM ksrc WHERE mid=?",
                (mid,))]
            chat, old_msg = con.execute("SELECT chat_id, message FROM kakao WHERE id=?", (mid,)).fetchone()
            self._write(mid, chat, derive(srcs), labels, old_msg)
            chats.add(chat)
        for aid in sorted(self.authors_changed):
            name = labels.author(aid)
            con.execute("UPDATE kakao SET sender=? WHERE author_id=? AND sender<>?", (name, aid, name))
        for chat in sorted(self.title_chats):
            title = labels.title(chat)
            r = con.execute("SELECT title FROM rooms WHERE chat_id=?", (chat,)).fetchone()
            if r is None or r[0] != title:
                con.execute("UPDATE kakao SET room=? WHERE chat_id=? AND room<>?", (title, chat, title))
                chats.add(chat)
        for chat in sorted(chats):
            self._facet(chat, labels)
        self.touched.clear()
        self.title_chats.clear()
        self.authors_changed.clear()

    def finish(self):
        con = self.con
        labels = _Labels(con, preload=True)
        top = con.execute("SELECT coalesce(max(id), 0) FROM kakao").fetchone()[0]
        step = 20_000
        for lo in range(1, top + 1, step):        # bounded batches, read fully before the rows are rewritten
            batch = con.execute(
                "SELECT s.mid, k.chat_id, s.origin, s.held, s.th, s.deleted, s.ts, s.author_id, s.type, s.edited, "
                "s.has_text FROM ksrc s JOIN kakao k ON k.id = s.mid WHERE s.mid >= ? AND s.mid < ? "
                "ORDER BY s.mid, s.origin",
                (lo, lo + step)).fetchall()
            cur, chat, srcs = None, None, []
            for r in batch:
                if r[0] != cur:
                    if cur is not None:
                        self._write(cur, chat, derive(srcs), labels, None)
                    cur, chat, srcs = r[0], r[1], []
                srcs.append(_Src(*r[2:]))
            if cur is not None:
                self._write(cur, chat, derive(srcs), labels, None)
        chats = {r[0] for r in con.execute("SELECT DISTINCT chat_id FROM kakao")}
        chats |= {r[0] for r in con.execute("SELECT DISTINCT chat_id FROM room_src")}
        for chat in sorted(chats):
            self._facet(chat, labels)

    def _write(self, mid, chat, d, labels, old_msg):
        con = self.con
        msg = ""
        if d["th"] is not None:
            r = con.execute("SELECT message FROM texts WHERE h=?", (d["th"],)).fetchone()
            msg = r[0] if r else ""
        vals = (d["ts"], labels.title(chat), labels.author(d["author_id"]), d["author_id"], d["type"],
                d["origins"], d["source"], d["edited"], d["deleted"], d["gone"])
        sets = ", ".join(c + "=?" for c in _ROW_COLS.split(", "))
        if self.full or old_msg != msg:
            con.execute(f"UPDATE kakao SET {sets}, message=?, ng=? WHERE id=?", vals + (msg, ti.ng_text(msg), mid))
        else:
            con.execute(f"UPDATE kakao SET {sets} WHERE id=?", vals + (mid,))

    def _facet(self, chat, labels):
        con = self.con
        n, senders, first, last = con.execute(
            "SELECT count(*), count(DISTINCT author_id), MIN(CASE WHEN ts > 0 THEN ts END), "
            "MAX(CASE WHEN ts > 0 THEN ts END) FROM kakao WHERE chat_id=? AND deleted=0", (chat,)).fetchone()
        nd, ngone = con.execute("SELECT count(*) - count(NULLIF(deleted, 1)), count(*) - count(NULLIF(gone, 1)) "
                                "FROM kakao WHERE chat_id=?", (chat,)).fetchone()
        con.execute("INSERT OR REPLACE INTO rooms(chat_id, title, n, senders, first_ts, last_ts, n_deleted, n_gone) "
                    "VALUES(?,?,?,?,?,?,?,?)", (chat, labels.title(chat), n, senders, first, last, nd, ngone))


class KakaoIndex(common.SourceIndex):
    name = NAME
    origins = frozenset(PRECEDENCE)
    schema_version = "kakao-4"        # kakao-4: the kakatok exports (merged_out/live_out/decrypted_live) left
    extractor_version = "kakao-4"     # kakao-4: names from the client's TalkUserDB (edb, backup_decrypted)
    fts_tables = ("kakao_fts",)

    def create_tables(self, con):
        con.executescript(SCHEMA)
        con.execute(ti.fts_ddl("kakao"))

    def create_triggers(self, con):
        common.create_triggers(con, "kakao", "kakao_fts")

    def applier(self, con, full):
        return KakaoApplier(con, full)

    def stats(self, con):
        rows, deleted, gone = con.execute(
            "SELECT count(*), coalesce(sum(deleted), 0), coalesce(sum(gone), 0) FROM kakao").fetchone()
        out = {"rows": rows, "deleted": deleted, "gone": gone,
               "rooms": con.execute("SELECT count(*) FROM rooms").fetchone()[0],
               "authors": con.execute("SELECT count(*) FROM authors").fetchone()[0]}
        out.update(common.read_counts(con))
        return out


SPEC = KakaoIndex()


# ================================================================================================ search

def add_arguments(p):
    common.add_search_arguments(p, ["hits", "count", "timeline", "senders", "rooms"])
    p.add_argument("--room", action="append", default=[], metavar="R",
                   help="substring of the room title, or a chat id (repeat for several rooms)")
    p.add_argument("--before", metavar="CUR", help="browse: the page older than this cursor (<ts>.<logId>.<row>)")
    p.add_argument("--after", metavar="CUR", help="browse: the page newer than this cursor")
    p.add_argument("--list-rooms", action="store_true", help="rooms by message count (with --room: matching ones)")


def origin_names(mask):
    return [o for i, o in enumerate(PRECEDENCE) if mask & (1 << i)]


def type_name(t):
    if t is None:
        return ""
    base = TYPE_NAMES.get(t & ~DELETE_ALL, f"type {t & ~DELETE_ALL}")
    return base + (" (deleted for all)" if t & DELETE_ALL else "")


class _Filters(drv.Filters):
    def __init__(self, a, tz):
        super().__init__(a, tz)
        self.selector = self.selector or bool(a.room)
        if a.room:
            ors = []
            for r in a.room:
                sql, ps = common.like("t.room", r)
                if r.strip().isdigit():
                    ors.append(f"(t.chat_id = ? OR {sql})")
                    self.params += [r.strip()] + ps
                else:
                    ors.append(sql)
                    self.params += ps
            self.where.append("(" + " OR ".join(ors) + ")")
        if a.sender:
            sql, ps = common.like("t.sender", a.sender)
            self.where.append(f"({sql} OR t.author_id = ?)")
            self.params += ps + [a.sender.strip()]

    def summary(self, a):
        out = {"room": list(a.room)}
        out.update(super().summary(a))
        return out


class KakaoSearch(drv.SearchDriver):
    name = NAME
    table = "kakao"
    fts_table = "kakao_fts"
    sort_cols = ("ts", "log_no", "id")
    group_modes = {"senders": "t.author_id", "rooms": "t.chat_id"}
    list_flag = "list_rooms"
    in_page_key = "rooms_in_page"
    page_group = "chat_id"
    hits_noun = "messages"
    browse_hint = "give a query, or --room / --sender / --since / --until to browse, or --list-rooms"
    Filters = _Filters

    def hit(self, r, tz, marks, width):
        msg = r["message"]
        return {"id": r["id"], "key": f"kakao:{r['chat_id']}:msg:{r['log_id']}", "chat_id": r["chat_id"],
                "log_id": r["log_id"], "ts": r["ts"], "date": common.fmt_ts(r["ts"], tz), "room": r["room"],
                "sender": r["sender"], "author_id": r["author_id"], "type": r["type"],
                "type_name": type_name(r["type"]),
                "origins": origin_names(r["origins"]), "source": r["source"], "deleted": bool(r["deleted"]),
                "gone": bool(r["gone"]), "edited": bool(r["edited"]), "cursor": self.cursor(r),
                "snippet": ti.highlight(msg, marks, width) if msg else self.placeholder(r), "text": msg}

    def context(self, con, r, n, a, tz):
        """n rows before and after a hit in its room, by (ts, log_no, id) keyset; the hit itself marked."""
        hide = "" if a.include_deleted else " AND deleted = 0"
        key = (r["chat_id"], r["ts"], r["ts"], r["log_no"], r["log_no"], r["id"])
        before = con.execute(
            "SELECT * FROM kakao WHERE chat_id = ? AND (ts < ? OR (ts = ? AND (log_no < ? OR (log_no = ? AND id < ?))))"
            f"{hide} ORDER BY ts DESC, log_no DESC, id DESC LIMIT ?", key + (n,)).fetchall()
        after = con.execute(
            "SELECT * FROM kakao WHERE chat_id = ? AND (ts > ? OR (ts = ? AND (log_no > ? OR (log_no = ? AND id > ?))))"
            f"{hide} ORDER BY ts, log_no, id LIMIT ?", key + (n,)).fetchall()
        out = []
        for c in list(reversed(before)) + [r] + list(after):
            text = common.flat(c["message"]) or self.placeholder(c)
            out.append({"id": c["id"], "log_id": c["log_id"], "ts": c["ts"], "date": common.fmt_ts(c["ts"], tz),
                        "sender": c["sender"], "author_id": c["author_id"], "text": common.clip(text, a.width),
                        "deleted": bool(c["deleted"]), "gone": bool(c["gone"]), "hit": c["id"] == r["id"]})
        return out

    def placeholder(self, r):
        return "(" + (type_name(r["type"]) or "no type") + ", no text)"

    def who(self, row):
        return row.get("sender") or row.get("author_id") or "?"

    def flags(self, h):
        return "".join(f" [{k}]" for k in ("deleted", "gone", "edited") if h.get(k))

    def group_row(self, con, r, mode, tz):
        first = common.fmt_ts(r["f"], tz, "%Y-%m-%d") if r["f"] else ""
        last = common.fmt_ts(r["l"], tz, "%Y-%m-%d") if r["l"] else ""
        if mode == "senders":
            lab = con.execute("SELECT name FROM authors WHERE author_id=?", (r["gid"],)).fetchone()
            return {"author_id": r["gid"], "label": (lab[0] if lab else "") or r["gid"] or "?", "n": r["n"],
                    "first": first, "last": last}
        lab = con.execute("SELECT title FROM rooms WHERE chat_id=?", (r["gid"],)).fetchone()
        return {"chat_id": r["gid"], "label": (lab[0] if lab else "") or "(untitled)", "n": r["n"],
                "first": first, "last": last}

    def group_label(self, r, mode):
        return str(r["label"]), (f"author={r['author_id']}" if mode == "senders" else f"chat={r['chat_id']}")

    def list_all(self, con, a, tz):
        where, params = [], []
        for r in a.room:
            sql, ps = common.like("title", r)
            if r.strip().isdigit():
                where.append(f"(chat_id = ? OR {sql})")
                params += [r.strip()] + ps
            else:
                where.append(sql)
                params += ps
        w = (" WHERE " + " OR ".join(where)) if where else ""
        total = con.execute(f"SELECT count(*) FROM rooms{w}", params).fetchone()[0]
        rows = con.execute(f"SELECT * FROM rooms{w} ORDER BY n DESC, chat_id LIMIT ?", params + [a.limit]).fetchall()
        return {"mode": "list", "total": total, "rows": [
            {"chat_id": r["chat_id"], "label": r["title"] or "(untitled)", "title": r["title"], "n": r["n"],
             "senders": r["senders"], "n_deleted": r["n_deleted"], "n_gone": r["n_gone"],
             "first": common.fmt_ts(r["first_ts"], tz, "%Y-%m-%d") if r["first_ts"] else "",
             "last": common.fmt_ts(r["last_ts"], tz, "%Y-%m-%d") if r["last_ts"] else ""} for r in rows]}

    def render_rest(self, res, a, tz):
        mode, rows, total = res["mode"], res["rows"], res["total"]
        out = sys.stdout.write
        if mode == "list":
            out(f"=== {NAME}: {total} room(s) by message count ===\n")
            for r in rows:
                span = f"{r['first']}..{r['last']}" if r["first"] else ""
                out(f"  {r['n']:7,}  {common.pad(r['label'], 40)} {span:22} id={r['chat_id']}\n")
            return
        if mode == "browse":
            page = res["page"]
            multi = res["rooms_in_page"] > 1
            out(f"=== {NAME}: {total} message(s); this page {len(rows)} ===\n")
            if rows:
                out(f"  older: --before {page['before']}    newer: --after {page['after']}\n")
            else:
                out("  (no messages)\n")
            for h in rows:
                where = f" | {common.pad(h['room'] or h['chat_id'], 20)}" if multi else ""
                text = common.flat(h["text"]) if h["text"] else h["snippet"]
                out(f"{h['date']:16} | {common.pad(self.who(h), 14)}{where} :: {text}{self.flags(h)}  #{h['log_id']}\n")
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
            out(f"{prefix}{h['date']:16} | {common.pad(self.who(h), 18)} | {common.pad(h['room'] or '(untitled)', 26)} "
                f":: {h['snippet']}{self.flags(h)}\n")
            out(f"{' ' * len(prefix)}chat={h['chat_id']} log={h['log_id']} cursor={h['cursor']} "
                f"origins={','.join(h['origins']) or '-'}\n")
            for c in h.get("context", []):
                arrow = ">>" if c["hit"] else "  "
                out(f"     {arrow} {c['date'][5:]:11} {common.pad(self.who(c), 14)} {c['text']}{self.flags(c)}\n")
            if h.get("context"):
                out("\n")


DRIVER = KakaoSearch(SPEC)


def run(a):
    return DRIVER.run(a)
