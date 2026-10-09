# -*- coding: utf-8 -*-
"""The app's Mattermost collector (mattertok's archiver, ported) against an in-process fake server: backfill and
forward cursors, timestamp ties at page edges, retention, purged cursors, archived channels, resuming a mattertok
archive, exclusion, unavailable channels, partial progress and interruption, torn files, users, account binding,
bounds, the HTTP client (retries, redirects), sign-in, the command line, options and the origin hook."""
import contextlib
import http.client
import io
import json
import os
import shutil
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import BaseHandler
from urllib.response import addinfourl

from rawlayer.capture.config import Config
from tools.archiveio import CollectorError, load
from mattermost import collector
from mattermost.apps import MatterConfig, _url
from mattermost.collector import MattermostClient, login, sync, token
from mattermost.readers import archive as reader
from mattermost.tests.fixtures import Fixture
from mattermost.tests.helpers import _force, by_key, capture, invariants, records, replay_diff

URL = "https://mm.example"


def ident(prefix, n):
    return f"{prefix}{n:0{26 - len(prefix)}d}"


ME, OTHER, TEAM, TEAM2 = ident("u", 0), ident("u", 1), ident("t", 1), ident("t", 2)


class Server:
    """Just enough of Mattermost's API v4: teams, channels (archived ones on request), cursor paging of posts as
    the server does it -- `before`/`after` compare the cursor post's create_at strictly, a cursor post that no
    longer exists matches nothing, posts older than the retention cut are not served -- and users."""

    def __init__(self):
        self.users = {ME: {"id": ME, "username": "me", "nickname": "", "first_name": "", "last_name": ""},
                      OTHER: {"id": OTHER, "username": "other", "nickname": "", "first_name": "", "last_name": ""}}
        self.teams = [{"id": TEAM, "name": "team", "display_name": "Team"}]
        self.channels, self.posts, self.fail, self.calls = {}, {}, {}, []
        self.clock, self.seq, self.cut = 1_700_000_000_000, 0, 0

    def channel(self, n, name="general", ctype="O", display=None, posts=0, author=ME, same=1):
        cid = ident("c", n)
        if ctype == "D":
            name = f"{ME}__{OTHER}"
        self.channels[cid] = {"id": cid, "type": ctype, "name": name, "display_name": display or name,
                              "team_id": TEAM if ctype in ("O", "P") else "", "delete_at": 0, "last_post_at": 0}
        self.posts[cid] = []
        self.add(cid, posts, author, same)
        return cid

    def add(self, cid, count, author=ME, same=1):
        """`count` posts, `same` of them sharing each create_at."""
        for i in range(count):
            if i % same == 0:
                self.clock += 1000
            self.seq += 1
            pid = ident("p", self.seq)
            self.posts[cid].append({"id": pid, "channel_id": cid, "create_at": self.clock, "update_at": self.clock,
                                    "edit_at": 0, "delete_at": 0, "user_id": author, "root_id": "",
                                    "message": "synthetic " + pid[-4:], "type": "", "props": {}, "metadata": {}})
            self.channels[cid]["last_post_at"] = self.clock

    def request(self, method, path, query=None, body=None):
        self.calls.append((path, dict(query or {})))
        parts = path.strip("/").split("/")
        if path == "/users/me":
            return {"id": ME, "username": "me"}
        if path == "/users/me/teams":
            return list(self.teams)
        if parts[:3] == ["users", "me", "teams"] and parts[4] == "channels":
            archived = (query or {}).get("include_deleted") == "true"
            return [dict(c, total_msg_count=len(self.posts[c["id"]])) for c in self.channels.values()
                    if c["team_id"] in ("", parts[3]) and (archived or not c["delete_at"])]
        if path == "/users/ids":
            return [self.users[u] for u in body if u in self.users]
        if parts[0] == "users":
            if parts[1] not in self.users:
                raise CollectorError("api_error", 404)
            return self.users[parts[1]]
        if parts[0] == "channels" and parts[2] == "posts":
            return self.page(parts[1], query)
        raise AssertionError("unexpected endpoint")

    def page(self, cid, query):
        status, skip = self.fail.get(cid, (None, 0))
        seen = sum(1 for p, _q in self.calls if p == f"/channels/{cid}/posts")
        if status is not None and seen > skip:
            if isinstance(status, BaseException):
                raise status
            raise CollectorError("api_error", status)
        rows = sorted(self.posts[cid], key=lambda p: (p["create_at"], p["id"]))
        visible = [p for p in rows if p["create_at"] >= self.cut]
        stamp = {p["id"]: p["create_at"] for p in rows}
        per = query["per_page"]
        if "after" in query:
            page = [p for p in visible if query["after"] in stamp and p["create_at"] > stamp[query["after"]]][:per]
        else:
            older = visible if "before" not in query else \
                [p for p in visible if query["before"] in stamp and p["create_at"] < stamp[query["before"]]]
            page = older[-per:]
        # like the server's, the neighbours of a page compare create_at strictly too
        prev = [p["id"] for p in visible if page and p["create_at"] < page[0]["create_at"]][-1:]
        nxt = [p["id"] for p in visible if page and p["create_at"] > page[-1]["create_at"]][:1]
        return {"order": [p["id"] for p in reversed(page)], "posts": {p["id"]: dict(p) for p in page},
                "next_post_id": "".join(nxt), "prev_post_id": "".join(prev),
                "first_inaccessible_post_time": self.cut if len(visible) < len(rows) else 0}

    def client(self):
        c = MattermostClient(URL, "synthetic-token", sleep=lambda s: None, rate=0)
        c.request = self.request
        return c

    def post_calls(self, cid):
        return [q for p, q in self.calls if p == f"/channels/{cid}/posts"]


class Base(unittest.TestCase):
    def setUp(self):
        self.fx, self.srv = Fixture(), Server()
        self.addCleanup(shutil.rmtree, self.fx.base, onerror=_force)

    def sync(self, **kw):
        return sync(self.fx.mm, self.srv.client(), **kw)

    def manifest(self):
        return load(self.fx.mm / "manifest.json")

    def lines(self, cid):
        rel = self.manifest()["channels"][cid]["path"]
        return [json.loads(x) for x in self.fx.mm.joinpath("posts", *rel.split("/")).read_bytes().splitlines() if x]

    def ids(self, cid):
        return [p["id"] for p in self.lines(cid)]

    def served(self, cid):
        return [p["id"] for p in self.srv.posts[cid]]


class TestSync(Base):
    def test_backfill_then_forward_and_capture(self):
        cid = self.srv.channel(1, posts=450)
        stats = self.sync()
        self.assertEqual((stats["channels"], stats["posts_written"], stats["errors"]), (1, 450, 0))
        # three pages back, then forward from the newest post in the same run
        self.assertEqual([tuple(k for k in ("before", "after") if k in q) for q in self.srv.post_calls(cid)],
                         [(), ("before",), ("before",), ("after",)])
        rows = self.lines(cid)
        self.assertEqual([p["id"] for p in rows], [p["id"] for p in self.srv.posts[cid]])
        st = self.manifest()["channels"][cid]
        self.assertTrue(st["backfill_done"])
        self.assertEqual((st["newest_post_id"], st["post_count"]), (rows[-1]["id"], 450))
        self.assertEqual(st["path"], f"public/team/general__{cid[:8]}.jsonl")
        self.assertEqual(load(self.fx.mm / "users.json")[ME]["username"], "me")
        self.assertEqual(capture(self.fx), 0)

        self.srv.add(cid, 3, author=OTHER)
        self.srv.calls.clear()
        self.assertEqual(self.sync()["posts_written"], 3)
        self.assertEqual(self.srv.post_calls(cid), [{"per_page": 200, "after": rows[-1]["id"]}])
        self.assertEqual(len(self.lines(cid)), 453)
        self.assertEqual(load(self.fx.mm / "users.json")[OTHER]["username"], "other")
        self.assertEqual(capture(self.fx), 0)
        recs = by_key(records(self.fx))
        self.assertEqual(sum(1 for k in recs if k.startswith("mm:post:")), 453)
        self.assertFalse(invariants(self.fx)[0])
        self.assertEqual(replay_diff(self.fx), 0)

    def test_resume_mattertok_archive(self):
        cid = self.srv.channel(1, posts=5)
        old = self.srv.posts[cid]
        rel = f"public/team/general__{cid[:8]}.jsonl"
        state = {"channel_id": cid, "display": "general", "channel_name": "general", "team_name": "team",
                 "type": "O", "path": rel, "oldest_cursor": old[0]["id"], "newest_post_id": old[-1]["id"],
                 "newest_create_at": old[-1]["create_at"], "backfill_done": True, "post_count": 5, "expected": 5,
                 "inaccessible_before": 0, "error": ""}
        self.fx.mm.joinpath("manifest.json").write_text(json.dumps(
            {"version": 1, "base_url": URL, "user_id": ME, "username": "me", "created_at": 1, "updated_at": 1,
             "channels": {cid: state}}, ensure_ascii=False, indent=1), encoding="utf-8")
        target = self.fx.mm.joinpath("posts", *rel.split("/"))
        target.parent.mkdir(parents=True)
        target.write_bytes(b"".join(json.dumps(p, ensure_ascii=False).encode() + b"\r\n" for p in old))
        before = target.read_bytes().splitlines()
        self.srv.add(cid, 2)
        stats = self.sync()
        self.assertEqual((stats["posts_written"], stats["errors"]), (2, 0))
        self.assertEqual(self.srv.post_calls(cid), [{"per_page": 200, "after": old[4]["id"]}])
        after = target.read_bytes().splitlines()
        self.assertEqual([x.rstrip(b"\r") for x in before], after[:5])
        self.assertEqual(len(after), 7)

    def test_exclusion_is_decided_before_any_fetch(self):
        keep = self.srv.channel(1, posts=2)
        skip = self.srv.channel(2, name="room-x", display="Room (Skip-Me)", posts=2)
        stats = self.sync(patterns=["skip-me"])
        self.assertEqual(stats["excluded"], 1)
        self.assertNotIn(skip, self.manifest()["channels"])
        self.assertEqual(self.srv.post_calls(skip), [])
        self.assertIn(keep, self.manifest()["channels"])
        # a known channel whose recorded display matches stays excluded after a server-side rename
        man = self.manifest()
        man["channels"][keep]["display"] = "old skip-me room"
        self.fx.mm.joinpath("manifest.json").write_text(json.dumps(man), encoding="utf-8")
        self.srv.calls.clear()
        self.assertEqual(self.sync(patterns=["skip-me"])["excluded"], 2)
        self.assertEqual(self.srv.post_calls(keep), [])

    def test_new_empty_channel_and_dm_labels(self):
        self.srv.channel(1, posts=0)
        dm = self.srv.channel(2, ctype="D", posts=1)
        self.sync()
        man = self.manifest()["channels"]
        self.assertEqual(list(man), [dm])
        self.assertEqual(man[dm]["display"], "@other")
        self.assertEqual(man[dm]["path"], f"dm/@other__{dm[:8]}.jsonl")
        self.assertEqual(man[dm]["team_name"], "")

    def test_unavailable_channel_and_every_channel_refused(self):
        ok, gone = self.srv.channel(1, posts=1), self.srv.channel(2, name="gone", posts=1)
        self.srv.fail[gone] = (403, 0)
        stats = self.sync()
        self.assertEqual((stats["unavailable"], stats["errors"]), (1, 0))
        self.assertEqual(self.manifest()["channels"][gone]["error"], "unavailable")
        self.assertEqual(len(self.lines(ok)), 1)
        self.srv.fail[ok] = (403, 0)
        self.assertEqual(self.sync()["errors"], 1)

    def test_failure_mid_backfill_keeps_the_published_page(self):
        cid = self.srv.channel(1, posts=450)
        self.srv.fail[cid] = (400, 1)
        stats = self.sync()
        self.assertEqual((stats["errors"], stats["posts_written"]), (1, 200))
        st = self.manifest()["channels"][cid]
        self.assertFalse(st["backfill_done"])
        self.assertEqual(st["error"], "collection_failed")
        # the next page starts above the oldest timestamp read, so it reads that timestamp's posts again
        self.assertEqual(st["oldest_cursor"], self.ids(cid)[1])
        del self.srv.fail[cid]
        self.srv.calls.clear()
        self.assertEqual(self.sync()["errors"], 0)
        self.assertEqual(self.srv.post_calls(cid)[0]["before"], st["oldest_cursor"])
        self.assertEqual(self.ids(cid), self.served(cid))
        self.assertEqual(self.manifest()["channels"][cid]["post_count"], 450)

    def test_timestamp_ties_across_page_edges_backward_and_forward(self):
        cid = self.srv.channel(1, posts=450, same=7)        # 200 = 28 * 7 + 4: every page edge splits a tie
        self.assertEqual(self.sync()["posts_written"], 450)
        self.assertEqual(self.ids(cid), self.served(cid))
        self.srv.add(cid, 450, same=7)
        self.srv.calls.clear()
        stats = self.sync()
        self.assertEqual((stats["posts_written"], stats["errors"]), (450, 0))
        self.assertEqual(self.ids(cid), self.served(cid))
        self.assertEqual([("after" in q) for q in self.srv.post_calls(cid)], [True, True, True])
        st = self.manifest()["channels"][cid]
        self.assertEqual((st["newest_post_id"], st["post_count"]), (self.served(cid)[-1], 900))

    def test_full_page_at_the_end_reads_the_rest_of_its_timestamp(self):
        # the server's prev/next_post_id are empty when only the rest of the page's edge timestamp remains
        cid = self.srv.channel(1)
        self.srv.add(cid, 15, same=15)
        self.srv.add(cid, 195)
        self.assertEqual(self.sync()["posts_written"], 210)
        self.assertEqual(self.ids(cid), self.served(cid))
        self.srv.add(cid, 190)
        self.srv.add(cid, 15, same=15)
        self.assertEqual(self.sync()["posts_written"], 205)
        self.assertEqual(self.ids(cid), self.served(cid))

    def test_forward_stop_between_pages_resumes_inside_the_split_timestamp(self):
        cid = self.srv.channel(1, posts=5)
        self.sync()
        self.srv.add(cid, 195)
        self.srv.add(cid, 10, same=10)              # the first forward page ends inside this timestamp
        self.srv.add(cid, 50)
        self.srv.fail[cid] = (500, 3)               # this run's second forward page fails
        self.assertEqual(self.sync()["posts_written"], 200)
        del self.srv.fail[cid]
        self.assertEqual(self.sync()["errors"], 0)
        self.assertEqual(self.ids(cid), self.served(cid))

    def test_deleted_backfill_cursor_pages_back_again(self):
        cid = self.srv.channel(1, posts=600)
        self.srv.fail[cid] = (500, 1)
        self.sync()
        cursor = self.manifest()["channels"][cid]["oldest_cursor"]
        self.srv.posts[cid] = [p for p in self.srv.posts[cid] if p["id"] != cursor]   # deleted for good
        del self.srv.fail[cid]
        self.srv.calls.clear()
        stats = self.sync()
        self.assertEqual((stats["errors"], stats["posts_written"]), (0, 400))    # known posts read again don't count
        self.assertEqual([q.get("before") for q in self.srv.post_calls(cid)][:2], [cursor, None])
        self.assertTrue(self.manifest()["channels"][cid]["backfill_done"])
        self.assertEqual(len(self.lines(cid)), 600)  # the deleted post stays in the archive
        self.assertLessEqual(set(self.served(cid)), set(self.ids(cid)))

    def test_retention_cut_ends_the_backfill(self):
        cid = self.srv.channel(1, posts=6)
        self.srv.cut = self.srv.posts[cid][2]["create_at"]   # the two oldest posts are past the retention limit
        self.assertEqual(self.sync()["posts_written"], 4)
        st = self.manifest()["channels"][cid]
        self.assertEqual((st["inaccessible_before"], st["backfill_done"]), (self.srv.cut, True))
        self.assertEqual(self.ids(cid), self.served(cid)[2:])

    def test_dm_listed_in_every_team_is_fetched_once(self):
        self.srv.teams.append({"id": TEAM2, "name": "second", "display_name": "Second"})
        dm = self.srv.channel(1, ctype="D", posts=3)
        stats = self.sync()
        self.assertEqual((stats["channels"], stats["posts_written"]), (1, 3))
        self.assertEqual(sum(1 for p, _q in self.srv.calls if p.endswith("/channels")), 2)
        self.assertEqual([("after" in q) for q in self.srv.post_calls(dm)], [False, True])

    def test_purged_forward_cursor_catches_up(self):
        cid = self.srv.channel(1, posts=5)
        self.sync()
        self.srv.posts[cid].pop()                   # the cursor post is purged; `after=` it matches nothing
        self.srv.calls.clear()
        self.assertEqual(self.sync()["posts_written"], 0)
        self.assertEqual(len(self.srv.post_calls(cid)), 1)  # last_post_at did not move: nothing to catch up
        self.srv.add(cid, 3)
        self.srv.calls.clear()
        self.assertEqual(self.sync()["posts_written"], 3)
        self.assertEqual([tuple(k for k in ("before", "after") if k in q) for q in self.srv.post_calls(cid)],
                         [("after",), ()])
        self.assertEqual(self.manifest()["channels"][cid]["newest_post_id"], self.served(cid)[-1])
        self.assertEqual(len(self.lines(cid)), 8)   # the purged post stays in the archive
        self.srv.add(cid, 1)
        self.srv.calls.clear()
        self.assertEqual(self.sync()["posts_written"], 1)
        self.assertEqual(len(self.srv.post_calls(cid)), 1)

    def test_catch_up_publishes_in_parts_and_moves_the_cursor_last(self):
        cid = self.srv.channel(1, posts=5)
        self.sync()                                 # two post requests
        purged = self.srv.posts[cid].pop()["id"]
        self.srv.add(cid, 450)
        self.srv.fail[cid] = (RuntimeError("killed"), 5)    # the third catch-up page
        with patch.object(collector, "FLUSH_POSTS", 100), self.assertRaises(RuntimeError):
            self.sync()
        self.assertEqual(self.manifest()["channels"][cid]["newest_post_id"], purged)
        self.assertEqual(len(self.lines(cid)), 5 + 399)
        del self.srv.fail[cid]
        self.assertEqual(self.sync()["posts_written"], 51)
        self.assertEqual(self.manifest()["channels"][cid]["newest_post_id"], self.served(cid)[-1])
        self.assertEqual(len(self.lines(cid)), 5 + 450)

    def test_archived_channels(self):
        kept = self.srv.channel(1, posts=2)
        self.sync()
        self.srv.add(kept, 1)
        self.srv.channels[kept]["delete_at"] = self.srv.clock
        new = self.srv.channel(2, name="old-room", posts=2)
        self.srv.channels[new]["delete_at"] = self.srv.clock
        self.srv.calls.clear()
        self.assertEqual(self.sync()["posts_written"], 1)   # a known channel archived since keeps its last posts
        self.assertEqual(len(self.lines(kept)), 3)
        self.assertNotIn(new, self.manifest()["channels"])  # one the archive never held stays out, as in mattertok
        self.assertEqual(self.srv.post_calls(new), [])
        self.assertEqual([q for p, q in self.srv.calls if p.endswith("/channels")], [{"include_deleted": "true"}])

    def test_publish_failure_keeps_the_committed_cursor(self):
        cid = self.srv.channel(1, posts=5)
        self.sync()
        committed = self.manifest()["channels"][cid]
        self.srv.add(cid, 2)
        fresh = self.srv.channel(2, name="second", posts=2)
        with patch.object(collector, "publish", side_effect=CollectorError("archive_busy")):
            stats = self.sync()
        self.assertEqual((stats["errors"], stats["posts_written"]), (2, 0))
        man = self.manifest()["channels"]
        self.assertEqual(man[cid], dict(committed, error="collection_failed"))
        self.assertNotIn(fresh, man)
        stats = self.sync()
        self.assertEqual((stats["errors"], stats["posts_written"]), (0, 4))
        self.assertEqual(self.ids(cid), self.served(cid))
        self.assertEqual(self.manifest()["channels"][cid]["error"], "")

    def test_interruption_keeps_finished_channels_and_flushed_pages(self):
        done = self.srv.channel(1, posts=3)
        cid = self.srv.channel(2, name="long", posts=450)
        self.srv.fail[cid] = (RuntimeError("killed"), 2)
        with patch.object(collector, "FLUSH_POSTS", 100), self.assertRaises(RuntimeError):
            self.sync()
        man = self.manifest()["channels"]
        self.assertTrue(man[done]["backfill_done"])
        self.assertEqual(self.ids(done), self.served(done))
        self.assertEqual((man[cid]["backfill_done"], man[cid]["post_count"]), (False, 399))
        self.assertEqual(len(self.lines(cid)), 399)
        del self.srv.fail[cid]
        self.srv.calls.clear()
        self.assertEqual(self.sync()["errors"], 0)
        self.assertEqual(self.srv.post_calls(cid)[0]["before"], man[cid]["oldest_cursor"])
        self.assertEqual(self.ids(cid), self.served(cid))

    def test_collector_lock_is_exclusive(self):
        self.srv.channel(1, posts=1)
        with collector.locked(self.fx.mm), self.assertRaisesRegex(CollectorError, "collector_busy"):
            self.sync()
        self.assertEqual(self.sync()["errors"], 0)

    def test_torn_tail_and_duplicates_are_repaired_on_publish(self):
        cid = self.srv.channel(1, posts=3)
        self.sync()
        rel = self.manifest()["channels"][cid]["path"]
        target = self.fx.mm.joinpath("posts", *rel.split("/"))
        data = target.read_bytes()
        target.write_bytes(data + data.splitlines(keepends=True)[0] + b'{"id": "half')
        rd = reader.read_once(self.fx.mm, [], set(), "mm_archive")
        self.assertEqual(rd.notes["partial_tail"], 1)
        self.srv.add(cid, 1)
        self.sync()
        rows = self.lines(cid)
        self.assertEqual(len(rows), 4)
        self.assertEqual(len({p["id"] for p in rows}), 4)
        rd = reader.read_once(self.fx.mm, [], set(), "mm_archive")
        self.assertEqual((rd.notes["partial_tail"], rd.notes["bad_line"]), (0, 0))

    def test_users_never_shrink_and_missing_authors_resolve(self):
        cid = self.srv.channel(1, posts=1, author=OTHER)
        kept = ident("u", 9)
        self.fx.mm.joinpath("users.json").write_text(json.dumps({kept: {"username": "kept"}}), encoding="utf-8")
        self.sync()
        users = load(self.fx.mm / "users.json")
        self.assertEqual(users[kept], {"username": "kept"})
        self.assertEqual(users[OTHER]["username"], "other")
        del users[OTHER]
        self.fx.mm.joinpath("users.json").write_text(json.dumps(users), encoding="utf-8")
        self.srv.calls.clear()
        self.sync()                                 # no new post: the missing author is still resolved
        self.assertEqual(load(self.fx.mm / "users.json")[OTHER]["username"], "other")
        self.assertEqual(self.srv.post_calls(cid)[0]["after"], self.srv.posts[cid][0]["id"])

    def test_archive_is_bound_to_one_account_and_server(self):
        self.srv.channel(1, posts=1)
        self.sync()
        for key, value in (("user_id", ident("u", 7)), ("base_url", "https://other.example")):
            man = self.manifest()
            original = man[key]
            man[key] = value
            self.fx.mm.joinpath("manifest.json").write_text(json.dumps(man), encoding="utf-8")
            with self.assertRaisesRegex(CollectorError, "account_changed"):
                self.sync()
            man[key] = original
            self.fx.mm.joinpath("manifest.json").write_text(json.dumps(man), encoding="utf-8")
        self.assertEqual(self.sync()["errors"], 0)

    def test_bounds(self):
        a, b = self.srv.channel(1, posts=1), self.srv.channel(2, name="second", posts=1)
        stats = self.sync(max_items=1)
        self.assertTrue(stats["limited"])
        self.assertEqual(len(self.manifest()["channels"]), 1)
        self.srv.calls.clear()
        self.assertTrue(self.sync(time_budget=-1)["limited"])
        self.assertEqual(self.srv.post_calls(a) + self.srv.post_calls(b), [])


class Response:
    def __init__(self, value, headers=None):
        self.data, self.headers = json.dumps(value).encode(), headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def read(self):
        return self.data


def http_error(status, body=b"{}", headers=None):
    return HTTPError(URL, status, "synthetic", headers or {}, io.BytesIO(body))


class TestHttp(unittest.TestCase):
    def client(self, responses, seen=None):
        def opener(req, timeout=None):
            if seen is not None:
                seen.append(req)
            value = responses.pop(0)
            if isinstance(value, Exception):
                raise value
            return value
        return MattermostClient(URL, "private-secret", opener=opener, sleep=lambda s: None, rate=0)

    def test_retry_header_and_redaction(self):
        seen = []
        client = self.client([http_error(429, headers={"Retry-After": "0"}), http_error(503), Response({"id": ME})], seen)
        self.assertEqual(client.authenticate()["id"], ME)
        self.assertEqual(client.requests, 3)
        self.assertEqual(seen[0].get_header("Authorization"), "Bearer private-secret")
        self.assertEqual(seen[0].full_url, URL + "/api/v4/users/me")
        for status, category in ((401, "auth_required"), (403, "api_error")):
            with self.assertRaises(CollectorError) as raised:
                self.client([http_error(status, b"private-secret")]).authenticate()
            self.assertEqual((str(raised.exception), raised.exception.status), (category, status))
            self.assertNotIn("private-secret", repr(raised.exception.args))
        with self.assertRaisesRegex(CollectorError, "bad_url"):
            MattermostClient("http://mm.example", "t")

    def test_truncated_reply_is_retried(self):
        client = self.client([http.client.IncompleteRead(b"{"), Response({"id": ME})])
        self.assertEqual(client.authenticate()["id"], ME)
        self.assertEqual(client.requests, 2)
        with self.assertRaisesRegex(CollectorError, "transport_error"):
            self.client([http.client.RemoteDisconnected("synthetic")] * 4).authenticate()

    def test_redirect_is_refused_not_followed(self):
        class Redirect(BaseHandler):
            handler_order = 50                      # answers before the real HTTPS handler would connect

            def __init__(self):
                self.seen = []

            def https_open(self, req):
                self.seen.append((req.full_url, req.get_header("Authorization")))
                headers = http.client.parse_headers(io.BytesIO(b"Location: http://elsewhere.example/x\r\n\r\n"))
                response = addinfourl(io.BytesIO(b""), headers, req.full_url, 302)
                response.msg = "Found"
                return response
        transport = Redirect()
        client = MattermostClient(URL, "private-secret", opener=collector._opener(transport),
                                  sleep=lambda s: None, rate=0)
        with self.assertRaises(CollectorError) as raised:
            client.authenticate()
        self.assertEqual((str(raised.exception), raised.exception.status), ("api_error", 302))
        self.assertEqual(transport.seen, [(URL + "/api/v4/users/me", "Bearer private-secret")])

    def test_login_keeps_only_the_token(self):
        base = Fixture().base
        self.addCleanup(shutil.rmtree, base, onerror=_force)
        path = base / "token.txt"
        answers = iter(["someone", "123456"])
        responses = [http_error(401, b'{"id": "mfa.validate_token.authenticate.app_error"}'),
                     Response({"id": ME}, {"Token": "session-token"})]
        bodies = []

        def opener(req, timeout=None):
            bodies.append(json.loads(req.data))
            value = responses.pop(0)
            if isinstance(value, Exception):
                raise value
            return value
        with patch("rawlayer.capture.precheck.check"):
            report = login(URL, str(path), opener, ask=lambda prompt: next(answers), secret=lambda prompt: "pw-secret")
            self.assertEqual(report, {"status": "auth_ready"})
            self.assertEqual(path.read_text(encoding="ascii"), "session-token\n")
            self.assertEqual(bodies[1]["token"], "123456")
            self.assertNotIn("pw-secret", path.read_text(encoding="ascii"))
            with patch.dict(os.environ, {"MM_TOKEN": ""}):
                self.assertEqual(token(str(path)), "session-token")
            with patch.dict(os.environ, {"MM_TOKEN": "env-token"}):
                self.assertEqual(token(str(path)), "env-token")
            path.unlink()
            with self.assertRaisesRegex(CollectorError, "auth_failed"):
                login(URL, str(path), lambda req, timeout=None: (_ for _ in ()).throw(http_error(401)),
                      ask=lambda prompt: "someone", secret=lambda prompt: "wrong")
            with self.assertRaises(CollectorError) as raised:      # a redirect: mm_url is wrong, not the password
                login(URL, str(path), lambda req, timeout=None: (_ for _ in ()).throw(http_error(302)),
                      ask=lambda prompt: "someone", secret=lambda prompt: "pw-secret")
            self.assertEqual((str(raised.exception), raised.exception.status), ("api_error", 302))
            self.assertFalse(path.exists())
            with patch.dict(os.environ, {"MM_TOKEN": ""}), self.assertRaisesRegex(CollectorError, "auth_required"):
                token(str(path))
        with patch.dict(os.environ, {"MM_TOKEN": ""}), self.assertRaisesRegex(CollectorError, "unsafe_token_path"):
            token("relative-token.txt")


class TestApp(Base):
    def config(self, **values):
        cfg = json.loads(self.fx.config_path.read_text(encoding="utf-8"))
        cfg.update(values)
        self.fx.config_path.write_text(json.dumps(cfg), encoding="utf-8")
        return Config(self.fx.config_path)

    def test_options_and_task_checks(self):
        app, cfg = MatterConfig("mattermost"), self.config()
        self.assertEqual((cfg.mm_sync, cfg.mm_url, cfg.mm_token_file), (False, "", ""))
        self.assertEqual(app.task_checks(cfg), [])
        cfg = self.config(mm_sync=True)
        self.assertEqual(app.task_checks(cfg), ["mm_sync requires mm_url"])
        path = self.fx.base / "token.txt"
        cfg = self.config(mm_sync=True, mm_url=URL, mm_token_file=str(path))
        with patch("rawlayer.capture.precheck.check"):
            self.assertEqual(app.task_checks(cfg), ["mm_sync requires a token file"])
            path.write_text("t", encoding="ascii")
            self.assertEqual(app.task_checks(cfg), [])
        self.assertEqual(app.task_checks(self.config(mm_token_file="relative.txt")),
                         ["mm_sync requires a protected token path"])
        with self.assertRaises(ValueError):
            _url("http://mm.example")

    def test_origin_collects_first_only_with_mm_sync(self):
        self.srv.channel(1, posts=2)
        calls = []

        def fake(cfg, **kw):
            calls.append(cfg)
            return sync(cfg.sources["mm_archive"], self.srv.client(), cfg.mm_exclude)
        with patch.object(collector, "sync_config", side_effect=fake), patch("rawlayer.capture.precheck.check"):
            self.assertEqual(capture(self.fx), 0)           # mm_sync off: the archive is only read
            self.assertEqual(calls, [])
            self.config(mm_sync=True, mm_url=URL)
            self.assertEqual(capture(self.fx), 0)
            self.assertEqual(len(calls), 1)
            self.assertEqual(sum(1 for k in by_key(records(self.fx)) if k.startswith("mm:post:")), 2)
            with patch.dict(os.environ, {"RAWLAYER_ARCHIVE_READY": "1"}):
                self.assertEqual(capture(self.fx), 0)
            self.assertEqual(len(calls), 1)
        with patch.object(collector, "sync_config", return_value={"errors": 1}), \
                patch("rawlayer.capture.precheck.check"), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(capture(self.fx), 75)

    def test_command_line_exit_codes(self):
        argv, out = ["--config", str(self.fx.config_path)], io.StringIO()
        with patch("rawlayer.capture.precheck.check"), contextlib.redirect_stdout(out):
            for result, code in (({"status": "ok", "errors": 0}, 0), ({"status": "ok", "errors": 2}, 75),
                                 (CollectorError("auth_required"), 2), (CollectorError("account_changed"), 2),
                                 (CollectorError("collector_busy"), 75), (RuntimeError("synthetic"), 1)):
                kw = {"side_effect": result} if isinstance(result, Exception) else {"return_value": result}
                with patch.object(collector, "sync_config", **kw):
                    self.assertEqual(collector.main(argv), code)
            with patch.object(collector, "login", return_value={"status": "auth_ready"}) as signin:
                self.assertEqual(collector.main(["--login", *argv]), 0)
            self.assertEqual(signin.call_count, 1)
            with patch.object(collector, "login", side_effect=CollectorError("auth_failed", 401)):
                self.assertEqual(collector.main(["--login", *argv]), 2)
        reports = [json.loads(line) for line in out.getvalue().splitlines()]
        self.assertEqual(reports[-2], {"app": "mattermost", "status": "auth_ready"})
        self.assertEqual(reports[-1], {"app": "mattermost", "status": "auth_failed", "http_status": 401})
        self.assertNotIn("synthetic", out.getvalue())


if __name__ == "__main__":
    unittest.main()
