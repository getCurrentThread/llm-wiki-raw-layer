# -*- coding: utf-8 -*-
"""Read-only Mattermost REST API v4 collector, ported from mattertok's archiver (archive.py, client.py, auth.py).

It writes mattertok's archive format -- manifest.json (one cursor state per channel), posts/**.jsonl (one post per
line, oldest first) and users.json -- so mm_archive reads it unchanged and an existing mattertok archive continues
where mattertok stopped.

Each channel rolls two cursors, as in mattertok:

  backfill  `?before=<cursor>` from the newest page back until a page comes back short
  forward   `?after=<newest post id>` once the backfill is done, new posts only

`?page=` paging is not used: it drifts on deep histories. The server compares create_at strictly, so a page's
cursor (the one requested next and the one recorded) overlaps the posts that share its edge timestamp, and none of
them is skipped at a page boundary unless a whole page shares one timestamp. A recorded cursor post that was
deleted for good empties its pages: a backfill then pages back again from the newest page, a forward pass (when
the channel's last_post_at moved on) pages back to the known posts. Fetched posts are merged into the channel file
by post id, ordered by create_at, and the file is replaced atomically before the manifest records the cursors that
cover them (every FLUSH_POSTS posts and at the end of each channel), so an interrupted run fetches again at most
what it had not published. Known posts and users are never removed.

With a file cap, the attached files of at most that many bytes are kept too, as files/<id[:2]>/<id> (no extension,
so a kept .exe never runs on a click), each written before files.json records its post id, sha256, size and path;
mm_archive turns files.json into the ledger's pointers. files.json never shrinks."""
import getpass
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import shutil
import sys
import time
from urllib import error, parse, request
import uuid

from tools.archiveio import CollectorError, atomic, bind, inside, load, locked

PER_PAGE = 200
FILE_FLUSH = 100            # files.json is rewritten every 100 kept files, so an interrupted run loses little
FLUSH_POSTS = 5000          # publish a long backfill in parts, so memory and lost work stay bounded
RATE = 4.0                  # requests per second; Mattermost rate-limits each user
TIME_BUDGET = 2400          # seconds of collection inside the one-hour task, leaving capture and index the rest
TYPES = ("O", "P", "D", "G")
TYPE_DIR = {"O": "public", "P": "private", "D": "dm", "G": "group"}
STATE_FIELDS = ("channel_id", "display", "channel_name", "team_name", "type", "path", "oldest_cursor",
                "newest_post_id", "newest_create_at", "backfill_done", "post_count", "expected",
                "inaccessible_before", "error")
TRANSPORT = (error.URLError, OSError, http.client.HTTPException)   # HTTPException: a truncated or garbled reply
_ID = re.compile(r"[a-z0-9]{26}")
_INVALID = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


class _NoRedirect(request.HTTPRedirectHandler):
    """A 3xx is an error, never followed: urllib would resend the Authorization header to the new host, even over
    plain http (mattertok's httpx client did not follow redirects either)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _opener(*handlers):
    return request.build_opener(_NoRedirect, *handlers).open


_OPEN = _opener()


def _id(value):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise CollectorError("bad_response")
    return value


def _int(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def credential_path(value):
    from rawlayer.capture import paths, precheck
    path = Path(value)
    if not value or not path.is_absolute() or not path.resolve().is_relative_to(Path(paths.DATA_PARENT).resolve()):
        raise CollectorError("unsafe_token_path")
    precheck.check(path.parent)
    return path


def token(configured=""):
    """A session token or personal access token: MM_TOKEN, else the private token file."""
    value = os.environ.get("MM_TOKEN")
    if not value and configured:
        try:
            value = credential_path(configured).read_text(encoding="utf-8").strip()
        except OSError:
            raise CollectorError("auth_required") from None
    if not value or any(c.isspace() or c == "\x00" for c in value):
        raise CollectorError("auth_required")
    return value


def base_url(value):
    parts = parse.urlsplit(value or "")
    if parts.scheme != "https" or not parts.netloc or parts.query or parts.fragment:
        raise CollectorError("bad_url")
    return value.rstrip("/")


class MattermostClient:
    def __init__(self, url, credential, opener=_OPEN, sleep=time.sleep, rate=RATE):
        self.api = base_url(url) + "/api/v4"
        self.credential, self.opener, self.sleep = credential, opener, sleep
        self.interval = 1.0 / rate if rate > 0 else 0.0
        self.next_at, self.requests, self.user_id = 0.0, 0, None
        self.users = {}

    def _pace(self):
        wait = self.next_at - time.monotonic()
        if wait > 0:
            self.sleep(wait)
        self.next_at = time.monotonic() + self.interval

    def request(self, method, path, query=None, body=None, binary=False):
        """429 honours Retry-After and 5xx / transport failures back off exponentially (four attempts). `binary`
        returns the reply's bytes instead of its JSON."""
        if not re.fullmatch(r"/[A-Za-z0-9_/-]+", path):
            raise CollectorError("bad_endpoint")
        url = self.api + path + ("?" + parse.urlencode(query) if query else "")
        headers = {"Authorization": "Bearer " + self.credential, "Accept": "*/*" if binary else "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = request.Request(url, data=data, headers=headers, method=method)
        delay = 1.0
        for attempt in range(4):
            self._pace()
            try:
                self.requests += 1
                with self.opener(req, timeout=60) as response:
                    raw = response.read()
                break
            except error.HTTPError as e:
                status, retry = e.code, (e.headers or {}).get("Retry-After")
                e.close()
                if status == 429 and attempt < 3:
                    try:
                        self.sleep(min(30.0, float(retry or delay)))
                    except ValueError:
                        self.sleep(delay)
                    delay *= 2
                    continue
                if 500 <= status < 600 and attempt < 3:
                    self.sleep(delay)
                    delay *= 2
                    continue
                # 401: the session expired or was revoked; `--login` issues a new one
                raise CollectorError("auth_required" if status == 401 else "api_error", status) from None
            except TRANSPORT:
                if attempt == 3:
                    raise CollectorError("transport_error") from None
                self.sleep(delay)
                delay *= 2
        if binary:
            return raw
        try:
            return json.loads(raw) if raw else None
        except ValueError:
            raise CollectorError("bad_response") from None

    def authenticate(self):
        me = self.request("GET", "/users/me")
        if not isinstance(me, dict):
            raise CollectorError("bad_response")
        self.user_id = _id(me.get("id"))
        return me

    def my_teams(self):
        rows = self.request("GET", "/users/me/teams")
        if not isinstance(rows, list):
            raise CollectorError("bad_response")
        return rows

    def my_channels(self, team_id):
        # archived channels too: one archived since the last run may still hold posts the archive lacks
        rows = self.request("GET", f"/users/me/teams/{_id(team_id)}/channels", {"include_deleted": "true"})
        if not isinstance(rows, list):
            raise CollectorError("bad_response")
        return rows

    def posts(self, channel_id, query):
        data = self.request("GET", f"/channels/{_id(channel_id)}/posts", query)
        if not isinstance(data, dict) or not isinstance(data.get("order") or [], list) \
                or not isinstance(data.get("posts") or {}, dict):
            raise CollectorError("bad_response")
        return data

    def fetch_users(self, user_ids):
        """POST /users/ids in chunks of 100 (no request per user); fills self.users."""
        missing = sorted({u for u in user_ids if u and u not in self.users})
        for i in range(0, len(missing), 100):
            rows = self.request("POST", "/users/ids", body=missing[i:i + 100])
            if not isinstance(rows, list):
                raise CollectorError("bad_response")
            for user in rows:
                if isinstance(user, dict) and isinstance(user.get("id"), str) and _ID.fullmatch(user["id"]):
                    self.users[user["id"]] = user
        return self.users

    def file(self, file_id):
        """An attached file's bytes."""
        return self.request("GET", f"/files/{_id(file_id)}", binary=True)

    def user(self, user_id):
        if user_id not in self.users:
            try:
                obj = self.request("GET", f"/users/{_id(user_id)}")
                if isinstance(obj, dict) and obj.get("id") == user_id:
                    self.users[user_id] = obj
            except CollectorError:
                pass
        return self.users.get(user_id)


def login(url, token_file, opener=_OPEN, ask=input, secret=getpass.getpass):
    """Sign in with a login id and password once and keep only the session token (mattertok's login). The
    password is never stored; Mattermost returns the token in the `Token` response header."""
    path = credential_path(token_file)
    endpoint = base_url(url) + "/api/v4/users/login"
    body = {"login_id": ask("Mattermost login id: ").strip(), "password": secret("Password (not shown): ")}
    if not body["login_id"] or not body["password"]:
        raise CollectorError("auth_required")
    for attempt in range(2):
        req = request.Request(endpoint, data=json.dumps(body).encode("utf-8"),
                              headers={"Content-Type": "application/json"}, method="POST")
        try:
            with opener(req, timeout=60) as response:
                value = response.headers.get("Token")
                me = json.loads(response.read())
            break
        except error.HTTPError as e:
            try:
                detail = json.loads(e.read())
                detail = str(detail.get("id", "")) if isinstance(detail, dict) else ""
            except (OSError, ValueError, http.client.HTTPException):
                detail = ""
            finally:
                e.close()
            if "mfa" in detail.lower() and "token" not in body and attempt == 0:
                body["token"] = ask("MFA code: ").strip()
                continue
            # a redirect is not followed: it means mm_url is not the server's address, not wrong credentials
            raise CollectorError("api_error" if 300 <= e.code < 400 else "auth_failed", e.code) from None
        except (*TRANSPORT, ValueError):
            raise CollectorError("transport_error") from None
    else:
        raise CollectorError("auth_failed")
    if not isinstance(value, str) or not value or any(c.isspace() for c in value) \
            or not isinstance(me, dict) or not _ID.fullmatch(str(me.get("id", ""))):
        raise CollectorError("auth_failed")
    _replace(path.parent, path, value.encode("ascii") + b"\n")
    return {"status": "auth_ready"}


# ---------------------------------------------------------------------------------------------------- the archive

def safe_name(name, fallback="unnamed", limit=60):
    """A Windows-safe file name stem: no reserved characters or device names, bounded length."""
    cleaned = _INVALID.sub("_", str(name or "")).strip().strip(". ")
    cleaned = re.sub(r"\s+", " ", cleaned)[:limit].strip()
    if not cleaned:
        return fallback
    if cleaned.upper().split(".")[0] in _RESERVED:
        cleaned = "_" + cleaned
    return cleaned


def path_for(state):
    """mattertok's layout. DM names are 'userid__userid' (53 characters), so DMs use the display name."""
    bucket = TYPE_DIR.get(state["type"], "other")
    cid8 = state["channel_id"][:8]
    if state["type"] in ("D", "G"):
        return f"{bucket}/{safe_name(state['display'], cid8, 40)}__{cid8}.jsonl"
    team = safe_name(state["team_name"] or "no-team", "no-team", 40)
    return f"{bucket}/{team}/{safe_name(state['channel_name'] or state['display'], cid8)}__{cid8}.jsonl"


def _safe_rel(path):
    if not isinstance(path, str) or not path.endswith(".jsonl") or "\\" in path or ":" in path or "\x00" in path:
        return False
    return all(part not in ("", ".", "..") for part in path.split("/"))


def excluded(patterns, *names):
    """mattertok's is_excluded_channel: any name (display, URL name, known display) contains a pattern, casefolded."""
    haystack = "\n".join(n for n in names if isinstance(n, str) and n).casefold()
    return any(p.casefold() in haystack for p in patterns)


def _label(client, ch):
    if ch.get("type") == "D":
        others = [u for u in str(ch.get("name", "")).split("__") if u and u != client.user_id]
        if not others:
            return "@DM"
        user = client.user(others[0]) if _ID.fullmatch(others[0]) else None
        return "@" + ((user or {}).get("username") or others[0][:8])
    return ch.get("display_name") or ch.get("name") or ch["id"][:8]


def discover(client):
    """Every channel I belong to, once: DMs and group DMs appear in every team's list."""
    seen, errors = {}, 0
    for team in client.my_teams():
        try:
            rows = client.my_channels(team.get("id") if isinstance(team, dict) else None)
        except CollectorError:
            errors += 1
            continue
        for ch in rows:
            if not isinstance(ch, dict) or not isinstance(ch.get("id"), str) or not _ID.fullmatch(ch["id"]) \
                    or ch["id"] in seen:
                continue
            seen[ch["id"]] = dict(ch, _team_name=team.get("name", "") if ch.get("team_id") else "")
    # one batch for every DM counterpart's name instead of one request per DM
    client.fetch_users([u for ch in seen.values() if ch.get("type") == "D"
                        for u in str(ch.get("name", "")).split("__") if u != client.user_id and _ID.fullmatch(u)])
    return list(seen.values()), errors


def plan(client, known, channels, patterns):
    """The channels to sync, as manifest states. The exclusion is decided here only, so no path (backfill,
    forward, resume) fetches an excluded channel. New channels without messages are skipped, and so are archived
    channels the archive never held (mattertok never listed archived channels)."""
    states, skipped = [], 0
    for ch in channels:
        ctype = ch.get("type", "O")
        if ctype not in TYPES:
            continue
        old = known.get(ch["id"])
        if excluded(patterns, ch.get("display_name"), ch.get("name"), old.get("display") if old else None):
            skipped += 1
            continue
        expected = _int(ch.get("total_msg_count"))
        if old is None:
            if not expected or _int(ch.get("delete_at")):
                continue
            state = {"channel_id": ch["id"], "display": _label(client, ch), "channel_name": ch.get("name", ""),
                     "team_name": ch.get("_team_name", ""), "type": ctype, "path": "", "oldest_cursor": "",
                     "newest_post_id": "", "newest_create_at": 0, "backfill_done": False, "post_count": 0,
                     "expected": expected, "inaccessible_before": 0, "error": ""}
            state["path"] = path_for(state)
        else:
            state = dict(old, expected=expected or old.get("expected", 0))
        states.append((state, _int(ch.get("last_post_at"))))
    return states, skipped


def _batch(data):
    posts = data.get("posts") or {}
    batch = []
    for pid in data.get("order") or []:
        post = posts.get(pid) if isinstance(pid, str) else None
        if not isinstance(post, dict) or post.get("id") != pid or not _ID.fullmatch(pid):
            raise CollectorError("bad_response")
        batch.append(post)
    return batch


def _key(post):
    return (_int(post.get("create_at")), post["id"])


def _advance(st, batch):
    newest = max(batch, key=_key)
    if _key(newest) >= (st["newest_create_at"], st["newest_post_id"]):
        st["newest_create_at"], st["newest_post_id"] = _key(newest)


def _back_cursor(batch):
    """The next `before=` cursor: the oldest post newer than the page's oldest timestamp, so the next page reads
    again every post sharing that timestamp (the server compares create_at strictly). A page of one timestamp
    has no such post and moves on from its oldest post."""
    edge = min(_int(p.get("create_at")) for p in batch)
    later = [p for p in batch if _int(p.get("create_at")) > edge]
    return (min(later, key=_key) if later else min(batch, key=_key))["id"]


def _forward_cursor(batch):
    edge = max(_int(p.get("create_at")) for p in batch)
    earlier = [p for p in batch if _int(p.get("create_at")) < edge]
    return (max(earlier, key=_key) if earlier else max(batch, key=_key))["id"]


def _backward(client, st, got, deadline, flush, stop_at=None):
    """Page back from st's oldest_cursor (the newest page when it is empty) until the channel's first post, then
    mark the backfill done. With `stop_at` (a create_at), page back from the newest page only until the posts
    reach it, and move the forward cursor only once they do: the catch-up of a channel whose forward cursor post
    was purged. Returns False when the time budget ran out first."""
    catchup = stop_at is not None
    cursor, seen, top = "" if catchup else st["oldest_cursor"], set(), []
    resumed = bool(cursor)
    while True:
        if time.monotonic() > deadline:
            return False
        query = {"per_page": PER_PAGE}
        if cursor:
            query["before"] = cursor
        data = client.posts(st["channel_id"], query)
        cut = _int(data.get("first_inaccessible_post_time"))
        if cut:
            st["inaccessible_before"] = cut        # the server's retention limit: older posts cannot be read
        batch = _batch(data)
        if resumed and not batch and not cut:
            # nothing before the saved cursor and no retention cut: the cursor post was deleted for good, which
            # empties every `before=` page. Page back again from the newest page; known posts merge by id.
            resumed, cursor = False, ""
            continue
        resumed = False
        fresh = [p for p in batch if p["id"] not in seen]
        seen.update(p["id"] for p in batch)
        got.extend(p for p in fresh if not catchup or _int(p.get("create_at")) > stop_at)
        # only a short page ends the pass: prev_post_id compares create_at strictly, so it is empty while the rest
        # of a timestamp split by this page's edge is still to come
        done = len(batch) < PER_PAGE
        if batch:
            # nothing new on a page means its edge group was all read already: move past the page instead
            cursor = _back_cursor(batch) if fresh else min(batch, key=_key)["id"]
            if catchup:
                top = top or batch
                done = done or min(_int(p.get("create_at")) for p in batch) <= stop_at
            else:
                _advance(st, batch)
                st["oldest_cursor"] = cursor
        if done:
            if catchup and top:
                _advance(st, top)
            elif not catchup:
                st["backfill_done"] = True
            flush()
            return True
        if len(got) >= FLUSH_POSTS:
            flush()                                 # a catch-up's cursor stays put until it is done


def fetch(client, st, got, deadline, flush, last_post_at=0):
    """Advance one channel's cursors in `st`, collecting the posts into `got`; `flush()` publishes `got` and
    records `st`. A channel still backfilling pages back first, then forward from its newest post. Returns False
    when the time budget ran out first."""
    if not st["backfill_done"] or not st["newest_post_id"]:
        if not _backward(client, st, got, deadline, flush):
            return False
        if not st["newest_post_id"]:
            return True                             # a channel without posts
    cursor, seen, first = st["newest_post_id"], set(), True
    while True:
        if time.monotonic() > deadline:
            return False
        data = client.posts(st["channel_id"], {"per_page": PER_PAGE, "after": cursor})
        batch = _batch(data)
        if not batch:
            if first and last_post_at > st["newest_create_at"]:
                # the channel has newer posts, but nothing comes after the cursor: the cursor post was purged
                # (retention, permanent delete), which empties every `after=` page. Page back to known posts.
                return _backward(client, st, got, deadline, flush, stop_at=st["newest_create_at"])
            flush()
            return True
        first = False
        fresh = [p for p in batch if p["id"] not in seen]
        seen.update(p["id"] for p in batch)
        got.extend(fresh)
        if len(batch) < PER_PAGE:                   # as in _backward, next_post_id is not trusted
            _advance(st, batch)
            flush()
            return True
        cursor = _forward_cursor(batch) if fresh else max(batch, key=_key)["id"]
        # the recorded cursor moves only to the next request's cursor: a run that stops before that request
        # resumes there and reads again the posts that share the page's newest timestamp
        _advance(st, [p for p in batch if p["id"] == cursor])
        if len(got) >= FLUSH_POSTS:
            flush()


def _replace(root, path, data):
    """archiveio.atomic for bytes: a private temporary file, fsync, then replace (retried for shared readers)."""
    path = inside(root, path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with tmp.open("xb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        for attempt in range(10):
            try:
                os.replace(tmp, path)
                break
            except PermissionError:
                if attempt == 9:
                    raise CollectorError("archive_busy") from None
                time.sleep(0.2)
    finally:
        tmp.unlink(missing_ok=True)


def _rows(data):
    """{post id: (create_at, line bytes)} of a channel file's bytes; a torn or unreadable line is dropped."""
    rows = {}
    for line in data.split(b"\n"):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            continue
        if isinstance(obj, dict) and isinstance(obj.get("id"), str):
            rows[obj["id"]] = (_int(obj.get("create_at")), line)
    return rows


def publish(root, rel, posts):
    """Merge `posts` into the channel file by id (the newest fetch wins), oldest first, and replace the file when
    that changes it. Returns the number of posts in it and the number of `posts` that were new or different."""
    target = inside(root, root.joinpath("posts", *rel.split("/")))
    try:
        old = target.read_bytes()
    except FileNotFoundError:
        old = None
    rows, changed = _rows(old or b""), 0
    for post in posts:
        row = (_int(post.get("create_at")), json.dumps(post, ensure_ascii=False).encode("utf-8"))
        changed += rows.get(post["id"]) != row
        rows[post["id"]] = row
    ordered = sorted(rows.items(), key=lambda kv: (kv[1][0], kv[0]))
    data = b"".join(line + b"\n" for _pid, (_ts, line) in ordered)
    if data != old:
        _replace(root, target, data)
    return len(ordered), changed


def _archived(root, man):
    """(channel state, post) of every post in the archive's channel files; unreadable files and lines are skipped."""
    for st in man["channels"].values():
        if not isinstance(st, dict) or not _safe_rel(st.get("path")):
            continue
        try:
            data = root.joinpath("posts", *st["path"].split("/")).read_bytes()
        except OSError:
            continue
        for line in data.split(b"\n"):
            try:
                post = json.loads(line)
            except ValueError:
                continue
            if isinstance(post, dict):
                yield st, post


def _authors(root, man):
    """Every post author in the archive's channel files."""
    return {uid for _st, p in _archived(root, man)
            if isinstance(uid := p.get("user_id"), str) and _ID.fullmatch(uid)}


def store_files(root, client, man, patterns, cap, deadline, stats):
    """Keep every archived file of at most `cap` bytes not kept yet (or gone from disk), newest first, until the
    deadline or the disk floor. A failed or short download counts in files_failed, not errors: a file must not
    hold back the capture of posts."""
    from rawlayer.capture.run import HARD_FLOOR
    stored = load(root / "files.json", {})
    if not isinstance(stored, dict):
        raise CollectorError("bad_archive")
    todo = {}
    for st, post in _archived(root, man):
        if st.get("error") == "unavailable" or excluded(patterns, st.get("display"), st.get("channel_name")):
            continue
        md = post.get("metadata")
        for f in (md.get("files") if isinstance(md, dict) else None) or ():
            fid = f.get("id") if isinstance(f, dict) else None
            if not isinstance(fid, str) or not _ID.fullmatch(fid) or _int(f.get("delete_at")) \
                    or not 0 < _int(f.get("size")) <= cap or not isinstance(post.get("id"), str):
                continue
            old = stored.get(fid)
            if isinstance(old, dict) and root.joinpath(*str(old.get("path", "")).split("/")).is_file():
                continue
            todo[fid] = (_int(f.get("create_at")), fid, post["id"], f["size"])
    kept = 0
    for _ts, fid, pid, size in sorted(todo.values(), reverse=True):
        if time.monotonic() > deadline or shutil.disk_usage(root).free - size < HARD_FLOOR + 1024 ** 3:
            stats["limited"] = True
            break
        try:
            data = client.file(fid)
        except CollectorError as e:
            if e.category == "auth_required":
                raise
            stats["files_failed"] += 1
            continue
        if len(data) != size:
            stats["files_failed"] += 1
            continue
        rel = f"files/{fid[:2]}/{fid}"
        _replace(root, root.joinpath(*rel.split("/")), data)
        stored[fid] = {"post_id": pid, "sha256": hashlib.sha256(data).hexdigest(), "size": size, "path": rel}
        kept += 1
        if kept % FILE_FLUSH == 0:
            atomic(root, root / "files.json", stored)
    if kept:
        atomic(root, root / "files.json", stored)
    stats["files_stored"] += kept


def _manifest(root):
    man = load(root / "manifest.json")
    if man is None:
        return {"version": 1, "base_url": "", "user_id": "", "username": "", "created_at": int(time.time()),
                "updated_at": 0, "channels": {}}
    if not isinstance(man, dict) or man.get("version", 1) != 1 or not isinstance(man.get("channels"), dict):
        raise CollectorError("bad_archive")
    return man


def _known(man):
    """The manifest's channel states that are usable; the rest stay in the manifest untouched."""
    out = {}
    for cid, st in man["channels"].items():
        if isinstance(st, dict) and st.get("channel_id") == cid and _ID.fullmatch(cid) and _safe_rel(st.get("path")) \
                and all(f in st for f in STATE_FIELDS):
            out[cid] = st
    return out


def sync(root, client, patterns=(), max_items=0, time_budget=TIME_BUDGET, files_cap=0):
    root = Path(root)
    deadline = time.monotonic() + time_budget
    stats = {"status": "ok", "channels": 0, "posts_written": 0, "files_written": 0, "excluded": 0,
             "unavailable": 0, "errors": 0, "limited": False, "users": 0, "files_stored": 0, "files_failed": 0}
    with locked(root):
        me = client.authenticate()
        man = _manifest(root)
        if (man.get("user_id") and man["user_id"] != me["id"]) or \
                (man.get("base_url") and man["base_url"].rstrip("/") + "/api/v4" != client.api):
            raise CollectorError("account_changed")         # an archive belongs to one account on one server
        bind(root, me["id"])
        users = load(root / "users.json", {})
        if not isinstance(users, dict):
            raise CollectorError("bad_archive")
        man.update(base_url=client.api[:-len("/api/v4")], user_id=me["id"], username=me.get("username", ""))

        def save():
            man["updated_at"] = int(time.time())
            atomic(root, root / "manifest.json", man)

        known = _known(man)
        stats["errors"] += len(man["channels"]) - len(known)
        channels, errors = discover(client)
        stats["errors"] += errors
        states, stats["excluded"] = plan(client, known, channels, patterns)
        attempted, authors = 0, set()
        for st, last_post_at in states:
            if max_items and stats["channels"] >= max_items or time.monotonic() > deadline:
                stats["limited"] = True
                break
            cid, got, wrote = st["channel_id"], [], [0]
            attempted += 1

            def flush(st=st, got=got, wrote=wrote, cid=cid):
                # the posts reach the file before the manifest records the cursors that cover them
                if got:
                    st["post_count"], changed = publish(root, st["path"], got)
                    authors.update(p["user_id"] for p in got if isinstance(p.get("user_id"), str))
                    wrote[0] += changed
                    got.clear()
                if man["channels"].get(cid) != st:
                    man["channels"][cid] = dict(st)
                    save()
            failed = ""
            try:
                if not fetch(client, st, got, deadline, flush, last_post_at):
                    stats["limited"] = True
            except (CollectorError, OSError) as e:
                # 403/404: the channel's history is no longer readable (left, archived, permission removed)
                failed = "unavailable" if getattr(e, "status", None) in (403, 404) else "collection_failed"
            st["error"] = failed
            try:
                flush()                                     # what was fetched before a failure is kept too
            except (CollectorError, OSError):
                # the posts are not on disk: the manifest keeps the cursors of what was published
                failed = "collection_failed"
                if cid in man["channels"]:
                    man["channels"][cid] = dict(man["channels"][cid], error=failed)
                    save()
            if failed:
                stats["unavailable" if failed == "unavailable" else "errors"] += 1
            if wrote[0]:
                stats["files_written"] += 1
                stats["posts_written"] += wrote[0]
            stats["channels"] += 1
        if attempted and stats["unavailable"] == attempted:
            stats["errors"] += 1                            # every channel refused: the token, not the channels
        # users.json: refresh this run's authors and resolve every author the archive still lacks; never shrink
        wanted = {u for u in authors if _ID.fullmatch(u)} | (_authors(root, man) - set(users))
        try:
            client.fetch_users(wanted)
        except CollectorError:
            stats["errors"] += 1
        for uid in sorted(wanted):
            u = client.users.get(uid)
            if u:
                users[uid] = {k: u.get(k, "") for k in ("username", "nickname", "first_name", "last_name")}
        stats["users"] = len(users)
        atomic(root, root / "users.json", users)
        save()
        if files_cap:
            store_files(root, client, man, patterns, files_cap, deadline, stats)
    stats["requests"] = client.requests
    return stats


def sync_config(cfg, auth_only=False, full=False, max_items=0):
    client = MattermostClient(cfg.mm_url, token(cfg.mm_token_file))
    if auth_only:
        client.authenticate()
        return {"status": "authenticated"}
    return sync(cfg.sources["mm_archive"], client, cfg.mm_exclude, max_items,
                files_cap=cfg.mm_files_max_mb * 1024 ** 2)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--login" not in argv:
        from tools.archiveio import main as archive_main
        if "--help" in argv or "-h" in argv:
            print("One-time sign-in: python -s -B -m mattermost.collector --login [--config CONFIG]")
        return archive_main("mattermost", sync_config, argv)
    import argparse
    from rawlayer import registry
    from rawlayer.capture.config import Config
    from rawlayer.management.base import default_config
    p = argparse.ArgumentParser(description="Sign in to Mattermost once and keep only the session token")
    p.add_argument("--login", action="store_true")
    p.add_argument("--config")
    a = p.parse_args(argv)
    registry.select("mattermost")
    try:
        cfg = Config(a.config or default_config("mattermost"))
        report = login(cfg.mm_url, cfg.mm_token_file)
        print(json.dumps({"app": "mattermost", **report}))
        return 0
    except CollectorError as e:
        print(json.dumps({"app": "mattermost", "status": e.category, "http_status": e.status}))
        return 2
    except Exception as e:
        print(json.dumps({"app": "mattermost", "status": "error", "error_type": type(e).__name__}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
