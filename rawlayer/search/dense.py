# -*- coding: utf-8 -*-
"""Dense (semantic) retrieval beside an app's FTS5 index: `manage.py embed <app>` and `search <app> --hybrid`.

THE SIDECAR
<data_root>/index/<app>.vec.sqlite holds one vector per chunk of the app's rows and which records each chunk
covers -- never their text. The main index stays a pure function of the ledger; the sidecar is derived from the main
index, has its own writer lock (<app>.vec.lock) and may be deleted at any time (--hybrid then falls back to keyword
hits and says why). Its meta stamps the model, dims, query prefix and sqlite-vec version: embed refuses a different
one without --rebuild, search treats a mismatch as unavailable. It also stamps the chunker: another one (new code)
makes embed re-chunk every container, keeping the vectors of the chunks that come out the same, and search reports
the sidecar as stale until then.

CHUNKS
The app's driver yields each container's units in reading order (SearchDriver.chunk_units: a room, channel, page or
mail -> (label, [(rec_key, ts_s, text, brk)])). pack() joins them into chunks of about TARGET characters (OVERLAP
characters of trailing units carried over, at most CAP; a longer unit is cut into overlapping windows); a silence of
chunk_gap_s seconds or brk=True (a heading) starts a new chunk without overlap. A chunk is embedded as
"<label> | <date>\\n<units>" (a label past LABEL_CAP characters flattened and cut: Ollama embeds only the first 2048
tokens of a text, and drops the rest silently), and its sha covers container, first key and that text, so an
unchanged chunk is never embedded twice. embed re-chunks only the containers with rows folded after the sidecar's
src_seq (changed_containers), deletes the chunks they no longer produce (a purge removes its text's vectors on the
next run), embeds the new ones newest first in batches, and moves src_seq only when nothing is left (--limit runs
resume).

EMBEDDINGS
An Ollama server on a loopback IP literal (check_url: http only, no host names, no user info; proxies are ignored
and redirects refused, so the texts never leave this machine). Every vector is checked: DIMS finite numbers, not all
zero. Errors never carry the texts, only the server's own message (Ollama's does not quote the input). embed retries
a failed batch after each of RETRY_WAITS (transient failures are retried).

VECTORS
sqlite-vec's vec0 (VEC_VERSION; the windows-x86_64 build's DLL under <DATA_PARENT>/sqlite-vec/<version>/ must match
VEC_SHA256), cosine distance, chunk time span as metadata so --since/--until filter inside the KNN.

HYBRID
Reciprocal rank fusion (RRF_K) of two lists, per chunk: the keyword ranking (top KW_N, exact-first as hits()) mapped
to the chunk of each row (of the chunks holding it, the best-ranked semantic one), and the KNN of the query vector
(SEM_K candidates; every other filter and the query's exclusions applied through the main index, top SEM_N kept).
When those filters leave at most PREFILTER rows, the KNN runs over their chunks only, so a channel or a page keeps
its own best chunks. A relaxed keyword list (no row held every term) is weighted RELAXED_KW: OR-ed common words
otherwise push the semantic hits down. A hit is the unit's anchor row (its best keyword row, else the chunk's row
sharing the most query bigrams that no hit above shows) with "sources" (rank in each list), "rrf" and "context"
(the chunk's rows).
"""
import argparse
import array
import hashlib
import http.client
import ipaddress
import json
import math
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import common, textindex as ti
from .common import Busy, UsageError

VEC_VERSION = "v0.1.9"
VEC_SHA256 = "fcf98662a7ad9dce394b96a88f91032047823831b951c76636787c312a6476e6"   # vec0.dll, windows-x86_64
DEFAULT_URL = "http://127.0.0.1:11434"
# bge-m3: as good as qwen3-embedding:4b in local evals, and several times faster to embed
DEFAULT_MODEL, DEFAULT_DIMS = "bge-m3", 1024
# model name prefix -> what goes before a query (documents are embedded as they are)
QUERY_PREFIX = {"qwen3-embedding": "Instruct: Given a search query, retrieve relevant passages that answer the query\nQuery:"}
CHUNKER = "pack-2"
TARGET, OVERLAP, CAP, LABEL_CAP = 900, 150, 1600, 200
BATCH = 16
RETRY_WAITS = (2, 10)
RRF_K, KW_N, SEM_N, SEM_K = 60, 30, 30, 300
RELAXED_KW = 0.1                     # a relaxed OR-ed keyword list fused at full weight drowns the semantic hits
PREFILTER = 20000
EMBED_TIMEOUT, QUERY_TIMEOUT = 300, 30
_PART = 400                          # keys per IN (...) list: below SQLite's bound-parameter limit

SIDECAR_DDL = """
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS chunks(id INTEGER PRIMARY KEY, sha TEXT NOT NULL UNIQUE, container TEXT NOT NULL,
 ts_lo INTEGER NOT NULL, ts_hi INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS chunks_container ON chunks(container);
CREATE TABLE IF NOT EXISTS chunk_rec(chunk_id INTEGER NOT NULL, ord INTEGER NOT NULL, rec_key TEXT NOT NULL,
 PRIMARY KEY(chunk_id, ord)) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS chunk_rec_key ON chunk_rec(rec_key, chunk_id);
"""


class Unavailable(Exception):
    """Dense retrieval cannot run here (no sidecar, no sqlite-vec, another version): the reason is the message."""


class EmbedError(Exception):
    """The embedding server failed or returned a bad vector (the message never holds the texts); retry: the failure
    may pass (the server unreachable, or an HTTP error other than 404)."""

    def __init__(self, msg, retry=False):
        super().__init__(msg)
        self.retry = retry


# ------------------------------------------------------------------------------------------------ embeddings

def check_url(url):
    """'http://<loopback IP>:<port>' or UsageError: only this machine may see the texts."""
    p = urllib.parse.urlsplit(url or "")
    try:
        ip, port = ipaddress.ip_address(p.hostname or ""), p.port
    except ValueError:
        ip = port = None
    if p.scheme != "http" or ip is None or not ip.is_loopback or p.username or p.password \
            or p.path not in ("", "/") or p.query or p.fragment:
        raise UsageError("--url must be http://<loopback IP>[:port], e.g. " + DEFAULT_URL)
    return f"http://{f'[{ip}]' if ip.version == 6 else ip}:{port or 80}"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None                  # a 3xx surfaces as HTTPError


class Embedder:
    """texts -> vectors through Ollama's POST /api/embed."""

    def __init__(self, url, model, dims, timeout=EMBED_TIMEOUT):
        self.endpoint = check_url(url) + "/api/embed"
        self.model, self.dims, self.timeout = model, dims, timeout
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)

    def __call__(self, texts):
        body = json.dumps({"model": self.model, "input": list(texts), "keep_alive": "30m",
                           "dimensions": self.dims}).encode()
        req = urllib.request.Request(self.endpoint, body, {"Content-Type": "application/json"}, method="POST")
        try:
            with self.opener.open(req, timeout=self.timeout) as r:
                out = json.loads(r.read())
        except urllib.error.HTTPError as e:
            msg = _server_message(e)
            e.close()
            hint = f" (is {self.model} pulled?)" if e.code == 404 else ""
            raise EmbedError(f"embedding server: HTTP {e.code}{msg}{hint}", retry=e.code != 404) from None
        except (OSError, ValueError, http.client.HTTPException) as e:     # HTTPException: a broken or cut reply
            raise EmbedError(f"embedding server: {type(e).__name__} (is Ollama running at {self.endpoint}?)",
                             retry=not isinstance(e, ValueError)) from None
        vecs = out.get("embeddings") if isinstance(out, dict) else None
        if not isinstance(vecs, list) or len(vecs) != len(texts):
            raise EmbedError("embedding server: wrong number of vectors")
        for v in vecs:
            if not isinstance(v, list) or len(v) != self.dims or not any(v) \
                    or not all(isinstance(x, (int, float)) and math.isfinite(x) for x in v):
                raise EmbedError(f"embedding server: a vector is not {self.dims} finite numbers "
                                 f"(does {self.model} support dimensions={self.dims}?)")
        return vecs


def _server_message(e):
    """': <Ollama's error message>' of an HTTP error, flattened and clipped, or ''."""
    try:
        msg = json.loads(e.read(2048)).get("error")
    except (OSError, ValueError, AttributeError, http.client.HTTPException):
        return ""
    return f": {common.flat(msg)[:160]}" if isinstance(msg, str) and msg.strip() else ""


def embed_retrying(emb, texts):
    """emb(texts), retried after each of RETRY_WAITS seconds while the failure may pass."""
    for wait in RETRY_WAITS + (None,):
        try:
            return emb(texts)
        except EmbedError as e:
            if not e.retry or wait is None:
                raise
        time.sleep(wait)


def query_prefix(model):
    return next((v for k, v in QUERY_PREFIX.items() if model.startswith(k)), "")


# ------------------------------------------------------------------------------------------------ vectors

def vec_dll():
    from ..capture import paths  # noqa: PLC0415
    return Path(paths.DATA_PARENT) / "sqlite-vec" / VEC_VERSION / "vec0.dll"


def _blob(vec):
    return array.array("f", vec).tobytes()


class Vec0:
    """The vector table vec_chunks (rowid = chunks.id). The tests swap in a brute-force stand-in (STORE)."""

    @staticmethod
    def load(con):
        p = vec_dll()
        try:
            data = p.read_bytes()
        except OSError:
            raise Unavailable(f"sqlite-vec {VEC_VERSION} is not installed (llm.md 12)") from None
        if hashlib.sha256(data).hexdigest() != VEC_SHA256:
            raise Unavailable("the sqlite-vec DLL does not match its pinned SHA-256 (llm.md 12)")
        try:
            con.enable_load_extension(True)
            try:
                con.load_extension(str(p))
            finally:
                con.enable_load_extension(False)
            version = con.execute("SELECT vec_version()").fetchone()[0]
        except (AttributeError, sqlite3.Error) as e:
            raise Unavailable(f"sqlite-vec cannot be loaded: {type(e).__name__}") from None
        if version != VEC_VERSION:
            raise Unavailable(f"sqlite-vec {version} loaded, {VEC_VERSION} expected")

    @staticmethod
    def create(con, dims):
        con.execute(f"CREATE VIRTUAL TABLE IF NOT EXISTS vec_chunks USING vec0(embedding float[{int(dims)}] distance_metric=cosine, "
                    f"ts_lo integer, ts_hi integer)")

    @staticmethod
    def insert(con, rows):
        con.executemany("INSERT INTO vec_chunks(rowid, embedding, ts_lo, ts_hi) VALUES(?,?,?,?)",
                        [(i, _blob(v), lo, hi) for i, v, lo, hi in rows])

    @staticmethod
    def delete(con, ids):
        con.executemany("DELETE FROM vec_chunks WHERE rowid=?", [(i,) for i in ids])

    @staticmethod
    def knn(con, vec, k, since=None, until=None, ids=None):
        """Chunk ids nearest first; a time window keeps the chunks whose span overlaps it, ids (a list) these only."""
        where, params = ["embedding MATCH ?", "k = ?"], [_blob(vec), k]
        if ids is not None:
            where.append("rowid IN (SELECT value FROM json_each(?))")
            params.append(json.dumps(ids))
        if since is not None:
            where.append("ts_hi >= ?")
            params.append(since)
        if until is not None:
            where.append("ts_lo <= ?")
            params.append(until)
        return [r[0] for r in con.execute("SELECT rowid FROM vec_chunks WHERE " + " AND ".join(where) +
                                          " ORDER BY distance", params)]


STORE = Vec0


def sidecar_path(data_root, name):
    return common.index_dir(data_root) / (name + ".vec.sqlite")


def chunker_id(drv):
    return f"{CHUNKER}/{drv.chunk_version}"


def _parts(seq):
    seq = list(seq)
    return [seq[i:i + _PART] for i in range(0, len(seq), _PART)]


def _in(n):
    return ",".join("?" * n)


# ------------------------------------------------------------------------------------------------ chunks

def pack(label, units, gap_s=None, tz=None):
    """One container's chunks: [(rec_keys, ts_lo, ts_hi, text)] (see the module docstring)."""
    tz = tz or common.parse_tz(common.DEFAULT_TZ)
    label = label if len(label) <= LABEL_CAP else common.flat(label)[:LABEL_CAP]
    out, cur, prev = [], [], None

    def emit():
        keys = list(dict.fromkeys(k for k, _, _ in cur))
        tss = [t for _, t, _ in cur if t > 0]
        lo, hi = (min(tss), max(tss)) if tss else (0, 0)
        head = f"{label} | {common.fmt_ts(lo, tz, '%Y-%m-%d')}" if lo else label
        out.append((keys, lo, hi, head + "\n" + "\n".join(p for _, _, p in cur)))

    for key, ts, text, brk in units:
        text = common.clean(text or "").strip()
        if not text:
            continue
        pieces = [text[i:i + CAP] for i in range(0, len(text) - OVERLAP, CAP - OVERLAP)] if len(text) > CAP else [text]
        for piece in pieces:
            hard = brk or (gap_s is not None and prev is not None and ts - prev > gap_s)
            if cur and (hard or sum(len(p) + 1 for _, _, p in cur) + len(piece) > TARGET):
                emit()
                tail, n = [], 0
                for u in reversed(cur if not hard else []):
                    if n + len(u[2]) > OVERLAP:
                        break
                    tail.insert(0, u)
                    n += len(u[2]) + 1
                cur = tail if n + len(piece) <= CAP else []
            cur.append((key, ts, piece))
            prev, brk = ts, False    # only a heading's first piece breaks
    if cur:
        emit()
    return out


def chunk_sha(container, keys, text):
    return hashlib.sha256("\0".join((container, keys[0], text)).encode("utf-8")).hexdigest()


def plan(con, drv, since_seq, tz=None):
    """{sha: (container, keys, ts_lo, ts_hi, text)} of the containers changed after since_seq, and those containers."""
    containers = drv.changed_containers(con, since_seq)
    want = {}
    for c in containers:
        label, units = drv.chunk_units(con, c)
        for keys, lo, hi, text in pack(label, units, drv.chunk_gap_s, tz):
            want.setdefault(chunk_sha(c, keys, text), (c, keys, lo, hi, text))
    return want, containers


# ------------------------------------------------------------------------------------------------ embed

def _meta(con):
    return dict(con.execute("SELECT k, v FROM meta").fetchall())


def _set(con, **kv):
    con.executemany("INSERT OR REPLACE INTO meta(k, v) VALUES(?, ?)", [(k, str(v)) for k, v in kv.items()])


def embed(data_root, drv, a, clock=time.time, progress=None):
    """Bring the sidecar up to the main index (see the module docstring); the report holds counts only."""
    if not drv.container_col:
        raise UsageError(f"{drv.name} has no chunk hooks (SearchDriver.container_col / chunk_units)")
    t0 = clock()
    path = sidecar_path(data_root, drv.name)
    with common.IndexLock(common.index_dir(data_root), drv.name + ".vec"):
        if a.rebuild and not a.dry_run:
            for p in (path, path.with_name(path.name + "-journal")):
                p.unlink(missing_ok=True)
        side = common.open_ro(path) if path.exists() and a.dry_run else common._writer(path) if not a.dry_run else None
        try:
            if side is not None:
                STORE.load(side)
            old = _meta(side) if side is not None and side.execute(
                "SELECT 1 FROM sqlite_master WHERE name='meta'").fetchone() else {}
            model = a.model or old.get("model") or DEFAULT_MODEL
            dims = a.dims or int(old.get("dims") or DEFAULT_DIMS)
            url = check_url(a.url or old.get("url") or DEFAULT_URL)
            want_stamp = {"model": model, "dims": str(dims), "prefix": query_prefix(model), "vec": VEC_VERSION}
            if old and any(old.get(k) != v for k, v in want_stamp.items()):
                raise UsageError(f"the {drv.name} embeddings were made with model={old.get('model')} dims={old.get('dims')} "
                                 f"vec={old.get('vec')}: pass --rebuild to replace them")
            con, mmeta, _fresh = common.open_search(data_root, drv.spec)
            try:
                stamp = json.dumps(drv.spec.stamp(), sort_keys=True)
                # another chunker re-chunks every container; the chunks it still makes keep their vectors
                since = int(old.get("src_seq") or 0) \
                    if old.get("src_stamp") == stamp and old.get("chunker") == chunker_id(drv) else 0
                head = int(mmeta.get("head_seq") or 0)
                want, containers = plan(con, drv, since)
            finally:
                con.close()
            have = {}
            for part in _parts(containers):
                if side is not None and old:
                    have.update(side.execute(f"SELECT sha, id FROM chunks WHERE container IN ({_in(len(part))})", part))
            stale = [i for sha, i in have.items() if sha not in want]
            pending = sorted((s for s in want if s not in have), key=lambda s: -want[s][3])
            todo = pending[:a.limit] if a.limit else pending
            rep = {"source": drv.name, "model": model, "dims": dims, "containers": len(containers), "chunks": len(want),
                   "embedded": 0, "removed": len(stale), "pending": len(pending), "seq": head, "src_seq": since}
            if a.dry_run:
                return {**rep, "removed": 0, "would_remove": len(stale), "dry_run": True,
                        "seconds": round(clock() - t0, 1)}
            if not old:
                side.executescript(SIDECAR_DDL)
                STORE.create(side, dims)
                _set(side, **want_stamp, chunker=chunker_id(drv))
            _set(side, url=url)
            side.execute("BEGIN")
            for part in _parts(stale):
                STORE.delete(side, part)
                side.execute(f"DELETE FROM chunk_rec WHERE chunk_id IN ({_in(len(part))})", part)
                side.execute(f"DELETE FROM chunks WHERE id IN ({_in(len(part))})", part)
            side.execute("COMMIT")
            emb = Embedder(url, model, dims)
            for i in range(0, len(todo), BATCH):
                batch = todo[i:i + BATCH]
                vecs = embed_retrying(emb, [want[s][4] for s in batch])
                side.execute("BEGIN")
                rows = []
                for sha, vec in zip(batch, vecs):
                    c, keys, lo, hi, _text = want[sha]
                    cid = side.execute("INSERT INTO chunks(sha, container, ts_lo, ts_hi) VALUES(?,?,?,?)",
                                       (sha, c, lo, hi)).lastrowid
                    side.executemany("INSERT INTO chunk_rec VALUES(?,?,?)", [(cid, n, k) for n, k in enumerate(keys)])
                    rows.append((cid, vec, lo, hi))
                STORE.insert(side, rows)
                side.execute("COMMIT")
                rep["embedded"] += len(batch)
                if progress:
                    progress(rep["embedded"], len(todo))
            rep["pending"] = len(pending) - rep["embedded"]
            if not rep["pending"]:
                _set(side, src_seq=head, src_stamp=stamp, src_built_at=mmeta.get("built_at"), chunker=chunker_id(drv))
                rep["src_seq"] = head
            _set(side, updated_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(clock())))
            rep["seconds"] = round(clock() - t0, 1)
            return rep
        finally:
            if side is not None:
                side.close()


def _parser():
    ap = common.Parser(prog="manage.py embed", description="Embed an app's index into its dense sidecar "
                       "(<data_root>/index/<app>.vec.sqlite) for search --hybrid. Needs Ollama on this machine and "
                       "sqlite-vec (llm.md 12).")
    ap.add_argument("app")
    ap.add_argument("--config", required=True, help="capture config file (only data_root is read)")
    ap.add_argument("--model", help=f"Ollama embedding model (default: the sidecar's, else {DEFAULT_MODEL})")
    ap.add_argument("--dims", type=int, help=f"vector size (default: the sidecar's, else {DEFAULT_DIMS})")
    ap.add_argument("--url", help=f"the Ollama server, a loopback IP only (default: the sidecar's, else {DEFAULT_URL})")
    ap.add_argument("--limit", type=int, default=0, metavar="N",
                    help="embed at most N chunks this run, newest first (the run can be resumed); 0 = all")
    ap.add_argument("--dry-run", action="store_true", help="count containers and chunks, write nothing")
    ap.add_argument("--rebuild", action="store_true", help="drop the sidecar first (another model or dims)")
    ap.add_argument("--json", action="store_true")
    return ap


def main(argv):
    """manage.py embed <app> ...: 0 ok (also with chunks left by --limit) | 2 usage or no sqlite-vec | 75 busy or
    the embedding server failed (retry later; what was embedded is kept) | 1 error."""
    from .. import registry  # noqa: PLC0415 -- the app modules import this module
    common.setup_stdio()
    try:
        a = _parser().parse_args(argv)
        if a.limit < 0 or (a.dims is not None and not 0 < a.dims <= 8192):
            raise UsageError("--limit must not be negative; --dims must be 1..8192")
        mod = dict(registry.search_modules()).get(a.app)
        if mod is None:
            raise UsageError(f"{a.app} has no search index")
        last = [time.time()]

        def progress(n, total):
            if time.time() - last[0] >= 60 or n == total:
                last[0] = time.time()
                print(f"embed: {n}/{total} chunks", file=sys.stderr, flush=True)

        rep = embed(common.load_data_root(a.config), mod.DRIVER, a, progress=progress)
    except UsageError as e:
        print(f"embed: {e}", file=sys.stderr)
        return common.EXIT_USAGE
    except Unavailable as e:
        print(f"embed: {e}", file=sys.stderr)
        return common.EXIT_USAGE
    except Busy:
        print("embed: another embed run holds the lock (or the index is being rebuilt)", file=sys.stderr)
        return common.EXIT_BUSY
    except EmbedError as e:
        print(f"embed: {e}; what was embedded is kept, run again to resume", file=sys.stderr)
        return common.EXIT_BUSY
    except KeyboardInterrupt:
        return 130
    except Exception as e:  # noqa: BLE001
        print(f"embed: error {type(e).__name__}: {e}", file=sys.stderr)
        return common.EXIT_ERR
    print(common.dumps(rep) if a.json else "embed: " + " ".join(f"{k}={v}" for k, v in rep.items()))
    return common.EXIT_OK


# ------------------------------------------------------------------------------------------------ hybrid search

def _query_text(query):
    """The query as a sentence for the embedder: no exclusions, no OR, no quotes."""
    words = [w for w in query.split() if w != "OR" and not w.startswith("-")]
    return " ".join(words).replace('"', "") or query


def hybrid(drv, con, a, query, q, exact, flt, tz, data_root, fresh, relaxed=False):
    """(res, info): fused hits, or (None, info) with info["reason"] when dense retrieval cannot run. relaxed: the
    keyword list is q OR-ed (weighted RELAXED_KW)."""
    info = {"available": False, "rrf_k": RRF_K}
    path = sidecar_path(data_root, drv.name)
    # keyword list: exact-first, as hits() orders
    kw = drv.ranked(con, exact, flt, KW_N) if exact is not None else []
    seen = set(kw)
    kw_rows = drv.rows_by_id(con, (kw + [i for i in drv.ranked(con, q, flt, KW_N + len(kw)) if i not in seen])[:KW_N])
    kw_keys = [drv.rec_key(r) for r in kw_rows]
    # a selective filter: the KNN runs over the chunks of the rows it keeps, not over the filtered top SEM_K
    cand = drv.filtered_keys(con, q, flt, PREFILTER) if flt.selector and path.exists() else None
    try:
        if not path.exists():
            raise Unavailable(f"no embeddings yet -- run: python -s -B manage.py embed {drv.name}")
        side = common.open_ro(path)
        try:
            STORE.load(side)
            meta = _meta(side)
            if meta.get("vec") != VEC_VERSION:
                raise Unavailable(f"the embeddings were made by another version -- run: python -s -B manage.py "
                                  f"embed {drv.name} --rebuild")
            dims = int(meta["dims"])
            info.update(model=meta["model"], dims=dims, vec_seq=int(meta.get("src_seq") or 0))
            current = meta.get("chunker") == chunker_id(drv) and \
                meta.get("src_stamp") == json.dumps(drv.spec.stamp(), sort_keys=True)
            vec = Embedder(meta["url"], meta["model"], dims, QUERY_TIMEOUT)([meta.get("prefix", "") + _query_text(query)])[0]
            chunks_of = {}
            for part in _parts(set(kw_keys)):
                for key, cid in side.execute(f"SELECT rec_key, chunk_id FROM chunk_rec WHERE rec_key IN "
                                             f"({_in(len(part))})", part):
                    chunks_of.setdefault(key, []).append(cid)
            ids = None if cand is None else [r[0] for r in side.execute(
                "SELECT DISTINCT chunk_id FROM chunk_rec WHERE rec_key IN (SELECT value FROM json_each(?))",
                (json.dumps(cand),))]
            sem_ids = STORE.knn(side, vec, SEM_K, flt.since, flt.until, ids) if ids != [] else []
            members = {}
            for part in _parts(set(sem_ids) | {c for cs in chunks_of.values() for c in cs}):
                for cid, key in side.execute(f"SELECT chunk_id, rec_key FROM chunk_rec WHERE chunk_id IN "
                                             f"({_in(len(part))}) ORDER BY chunk_id, ord", part):
                    members.setdefault(cid, []).append(key)
        finally:
            side.close()
    except (Unavailable, EmbedError, UsageError, sqlite3.Error, KeyError, ValueError) as e:
        info["reason"] = str(e) if isinstance(e, (Unavailable, EmbedError)) else f"sidecar unreadable: {type(e).__name__}"
        return None, info
    # behind: rows folded after the embeddings (a seq that folded none does not count), or chunks of an older chunker
    # or index version (embed then re-chunks every container)
    info["stale"] = not current or (info["vec_seq"] < (fresh.get("seq") or 0)
                                    and bool(drv.changed_containers(con, info["vec_seq"])))
    rows = drv.rows_by_key(con, {k for ks in members.values() for k in ks}, q, flt)
    sem = [cid for cid in sem_ids if any(k in rows for k in members.get(cid, ()))][:SEM_N]
    sem_rank = {cid: n for n, cid in enumerate(sem, 1)}
    units = {}
    for rank, (key, r) in enumerate(zip(kw_keys, kw_rows), 1):
        # of the chunks holding the row (overlap, a long row's windows), the best semantic one no better keyword hit
        # holds: one unit with both ranks; when a better hit holds them all, the row is in that hit's context
        cs = sorted(chunks_of.get(key, ()), key=lambda c: (sem_rank.get(c, SEM_N + 1), c))
        cid = next((c for c in cs if ("c", c) not in units), cs[0] if cs else None)
        units.setdefault(("r", key) if cid is None else ("c", cid), {"kw": rank, "sem": None, "anchor": r})
    for rank, cid in enumerate(sem, 1):
        units.setdefault(("c", cid), {"kw": None, "sem": None, "anchor": None})["sem"] = rank
    w = RELAXED_KW if relaxed else 1.0

    def score(d):
        return (w / (RRF_K + d["kw"]) if d["kw"] else 0) + (1 / (RRF_K + d["sem"]) if d["sem"] else 0)

    order = sorted(units.items(), key=lambda u: (-score(u[1]), u[1]["kw"] or KW_N + 1, u[1]["sem"] or SEM_N + 1))
    qtok = set(ti.ng_tokens(_query_text(query))) - {ti.SEP}
    out, shown, kw_set = [], set(), set(kw_keys)
    for (kind, ident), d in order:
        if len(out) >= a.limit:
            break
        r = d["anchor"]
        if r is None:
            # a semantic-only chunk shows its row sharing the most query bigrams (the first on a tie) that is no
            # keyword hit and not shown above; with none left the hits above already show this chunk's rows
            free = [rows[k] for k in members[ident] if k in rows and k not in kw_set and k not in shown]
            if not free:
                continue
            r = max(free, key=lambda x: len(qtok.intersection(x["ng"].split())))
        akey = drv.rec_key(r)
        shown.add(akey)
        h = drv.hit(r, tz, q.marks, a.width) | {"sources": {"kw": d["kw"], "sem": d["sem"]}, "rrf": round(score(d), 6)}
        if kind == "c":
            h["context"] = [{**drv.hit(rows[k], tz, q.marks, a.width), "hit": k == akey}
                            for k in members.get(ident, ()) if k in rows] or [{**drv.hit(r, tz, q.marks, a.width), "hit": True}]
        elif a.context:
            h["context"] = drv.context(con, r, a.context, a, tz)
        out.append(h)
    body, params = drv.body(q, flt)
    info.update(available=True, retrievers={"kw": len(kw_rows), "sem": len(sem)})
    return {"mode": "hits", "total": con.execute(f"SELECT count(*) {body}", params).fetchone()[0], "rows": out}, info
