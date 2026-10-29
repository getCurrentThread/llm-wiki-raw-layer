# -*- coding: utf-8 -*-
"""The Mattermost administration tool (mattermost/admin.py) against an in-process fake server that sits behind the
client's opener, so the real request code runs: spec validation, plan, apply (channel, members, locked hook, RESULT),
idempotent reruns, conflicts, refusals before any write, verify, single-attempt writes, at-most-once posts, webhook
posts without credentials, pinning, 401 and the secrecy of the output. All data is synthetic."""
import contextlib
import http.client
import io
import json
import os
from pathlib import Path
import shutil
import unittest
from unittest.mock import patch
from urllib import parse
from urllib.error import HTTPError

from mattermost import admin
from mattermost.tests.fixtures import Fixture
from mattermost.tests.helpers import _force

URL = "https://mm.example"
TOKEN = "synthetic-token"
MESSAGE = "공지 테스트 메시지"


def ident(prefix, n):
    return f"{prefix}{n:0{26 - len(prefix)}d}"


ME, ALICE, BOB, CAROL, TEAM = (ident("u", 0), ident("u", 1), ident("u", 2), ident("u", 3), ident("t", 1))


def refuse(status):
    return HTTPError(URL, status, "synthetic", {}, io.BytesIO(b"{}"))


class Reply:
    def __init__(self, raw):
        self.raw = raw

    def read(self):
        return self.raw

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class Fake:
    """Just enough of API v4 for the admin tool: team and channel lookups, users, members, hooks and posts."""

    def __init__(self):
        self.users = {"me": ME, "alice": ALICE, "bob": BOB, "carol": CAROL}
        self.team_members = {ME: 0, ALICE: 0, BOB: -1}              # an active member has delete_at <= 0
        self.channels, self.members, self.hooks, self.posts = {}, {}, {}, {}
        self.writes, self.seen, self.pinned, self.drop, self.n = [], [], set(), None, 100

    def new(self, prefix):
        self.n += 1
        return ident(prefix, self.n)

    def channel(self, name):
        return next((c for c in self.channels.values() if c["name"] == name), None)

    def add_post(self, cid, message, user=ME):
        pid = self.new("p")
        self.posts[cid].append({"id": pid, "channel_id": cid, "message": message, "user_id": user,
                                "delete_at": 0, "is_pinned": False})
        return self.posts[cid][-1]

    def opener(self, req, timeout=None):
        self.seen.append(req)
        method, parts = req.get_method(), parse.urlsplit(req.full_url)
        body = json.loads(req.data) if req.data else None
        if method != "GET" and not parts.path.endswith("/users/usernames"):     # that POST is a lookup
            self.writes.append((method, parts.path))
        if parts.path.startswith("/hooks/"):
            hook = self.hooks.get(parts.path.split("/")[2])
            if hook is None or self.drop == ("POST", "webhook"):
                raise refuse(404)
            self.add_post(hook["channel_id"], body["text"])
            return Reply(b"ok")
        if req.get_header("Authorization") != "Bearer " + TOKEN:
            raise refuse(401)
        path = parts.path[len("/api/v4"):]
        result = self.route(method, path, dict(parse.parse_qsl(parts.query)), body)
        if self.drop == (method, path):
            self.drop = None
            raise http.client.RemoteDisconnected("synthetic")      # the server acted, the reply was lost
        return Reply(json.dumps(result).encode("utf-8"))

    def route(self, method, path, query, body):
        p = path.strip("/").split("/")
        page, per = int(query.get("page", 0)), int(query.get("per_page", 60))
        if path == "/users/me":
            return {"id": ME, "username": "me"}
        if p[:2] == ["teams", "name"]:
            if p[2] != "team-a":
                raise refuse(404)
            return {"id": TEAM, "name": "team-a"}
        if path == "/users/usernames":
            return [{"id": self.users[u], "username": u} for u in body if u in self.users]
        if p[0] == "teams" and p[2] == "members":
            if p[3] not in self.team_members:
                raise refuse(404)
            return {"user_id": p[3], "delete_at": self.team_members[p[3]]}
        if p[0] == "teams" and p[2:4] == ["channels", "name"]:
            if self.channel(p[4]) is None:
                raise refuse(404)
            return dict(self.channel(p[4]))
        if path == "/channels":
            if self.channel(body["name"]):
                raise refuse(400)
            cid = self.new("c")
            self.channels[cid] = {"id": cid, "delete_at": 0, "header": "", "purpose": "", **body}
            self.members[cid], self.posts[cid] = {ME}, []
            return self.channels[cid]
        if p[0] == "channels":
            cid = p[1]
            if cid not in self.channels:
                raise refuse(404)
            if p[2] == "patch":
                self.channels[cid].update(body)
                return self.channels[cid]
            if p[2] == "members" and len(p) == 4:
                if p[3] not in self.members[cid]:
                    raise refuse(404)
                return {"user_id": p[3]}
            if p[2] == "members" and method == "POST":
                self.members[cid].add(body["user_id"])
                return {"user_id": body["user_id"]}
            if p[2] == "members":
                rows = [{"user_id": u} for u in sorted(self.members[cid])]
                return rows[page * per:(page + 1) * per]
            if p[2] == "posts":
                rows = list(reversed(self.posts[cid]))[:per]
                return {"order": [r["id"] for r in rows], "posts": {r["id"]: r for r in rows}}
        if path == "/hooks/incoming":
            if method == "POST":
                hid = self.new("h")
                self.hooks[hid] = {"id": hid, "team_id": TEAM, "delete_at": 0, **body}
                return self.hooks[hid]
            rows = [h for h in self.hooks.values() if h["team_id"] == query["team_id"]]
            return rows[page * per:(page + 1) * per]
        if path == "/posts":
            return self.add_post(body["channel_id"], body["message"])
        if p[0] == "posts" and p[2] == "pin":
            for rows in self.posts.values():
                for post in rows:
                    if post["id"] == p[1]:
                        post["is_pinned"] = True
                        self.pinned.add(p[1])
                        return {"status": "OK"}
        raise AssertionError(path)


class Base(unittest.TestCase):
    def setUp(self):
        self.fx, self.fake = Fixture(), Fake()
        self.addCleanup(shutil.rmtree, self.fx.base, onerror=_force)
        cfg = json.loads(self.fx.config_path.read_text(encoding="utf-8"))
        cfg["mm_url"] = URL
        self.fx.config_path.write_text(json.dumps(cfg), encoding="utf-8")
        self.result = self.fx.base / "result.json"

    def spec(self, count=2, members=("alice", "bob"), webhook=True, **extra):
        channels = {f"team-{i:02d}": {"name": f"team-{i:02d}", "display_name": f"Team {i:02d}", "type": "P",
                                      "members": list(members), **extra,
                                      **({"webhook": {"description": "synthetic"}} if webhook else {})}
                    for i in range(1, count + 1)}
        path = self.fx.base / "spec.json"
        path.write_text(json.dumps({"team": "team-a", "channels": channels}), encoding="utf-8")
        return str(path)

    def run_admin(self, *argv, token=TOKEN):
        real, out = admin.AdminClient, io.StringIO()
        factory = lambda url, tok: real(url, tok, opener=self.fake.opener, sleep=lambda s: None, rate=0)  # noqa: E731
        with patch.dict(os.environ, {"MM_TOKEN": token}), patch("rawlayer.capture.precheck.check"), \
                patch.object(admin, "AdminClient", factory), contextlib.redirect_stdout(out):
            code = admin.main([argv[0], "--config", str(self.fx.config_path), *argv[1:]])
        text = out.getvalue()
        for secret in (TOKEN, "/hooks/", "alice", "Team 01"):
            self.assertNotIn(secret, text)
        return code, json.loads(text)

    def apply(self, spec=None):
        return self.run_admin("apply", "--spec", spec or self.spec(), "--out", str(self.result))

    def stored(self):
        return json.loads(self.result.read_text(encoding="utf-8"))

    def message(self, text=MESSAGE):
        path = self.fx.base / "message.txt"
        path.write_text(text, encoding="utf-8")
        return str(path)


class TestSpec(Base):
    def test_refusals_happen_before_any_request(self):
        good = {"name": "team-01", "display_name": "Team 01", "members": ["alice"]}
        cases = {"bad_spec": "not json", "spec_team": {"team": "", "channels": {"a": good}},
                 "spec_channels": {"team": "team-a", "channels": {}},
                 "spec_key": {"team": "team-a", "channels": {"bad key": good}},
                 "spec_name": {"team": "team-a", "channels": {"a": dict(good, name="A")}},
                 "spec_name_duplicate": {"team": "team-a", "channels": {"a": good, "b": good}},
                 "spec_display_name": {"team": "team-a", "channels": {"a": dict(good, display_name="")}},
                 "spec_type": {"team": "team-a", "channels": {"a": dict(good, type="D")}},
                 "spec_members": {"team": "team-a", "channels": {"a": dict(good, members=["alice", "Alice"])}},
                 "spec_webhook": {"team": "team-a", "channels": {"a": dict(good, webhook=[])}}}
        for category, raw in cases.items():
            with self.subTest(category):
                path = self.fx.base / "bad.json"
                path.write_text(raw if isinstance(raw, str) else json.dumps(raw), encoding="utf-8")
                code, report = self.run_admin("plan", "--spec", str(path))
                self.assertEqual((code, report["status"]), (2, category))
        self.assertEqual(self.fake.seen, [])


class TestApply(Base):
    def test_plan_is_read_only_and_apply_builds_everything(self):
        code, report = self.run_admin("plan", "--spec", self.spec())
        self.assertEqual((code, report["new"], report["exists"], report["items"]["team-01"]), (0, 2, 0, "new"))
        self.assertEqual((self.fake.writes, self.fake.channels), ([], {}))

        code, report = self.apply()
        self.assertEqual((code, report["created"], report["members_added"], report["hooks_created"], report["errors"]),
                         (0, 2, 4, 2, 0))
        self.assertEqual(report["items"], {"team-01": "created", "team-02": "created"})
        res = self.stored()
        for key in ("team-01", "team-02"):
            row = res["channels"][key]
            ch, hook = self.fake.channel(key), self.fake.hooks[row["hook_id"]]
            self.assertEqual((row["channel_id"], row["name"]), (ch["id"], key))
            self.assertEqual(self.fake.members[ch["id"]], {ME, ALICE, BOB})
            self.assertIs(hook["channel_locked"], True)
            self.assertEqual((hook["channel_id"], hook["display_name"]), (ch["id"], "Team " + key[-2:]))
            self.assertEqual(row["webhook_url"], f"{URL}/hooks/{row['hook_id']}")
        self.assertEqual(res["team_id"], TEAM)
        self.assertEqual(self.run_admin("plan", "--spec", self.spec())[1]["exists"], 2)

    def test_second_apply_changes_nothing(self):
        self.apply()
        before, self.fake.writes = self.stored(), []
        code, report = self.apply()
        self.assertEqual((code, report["created"], report["members_added"], report["hooks_created"]), (0, 0, 0, 0))
        self.assertEqual(set(report["items"].values()), {"unchanged"})
        self.assertEqual(self.fake.writes, [])
        self.assertEqual((len(self.fake.channels), len(self.fake.hooks)), (2, 2))
        self.assertEqual(self.stored()["channels"], before["channels"])

    def test_header_and_missing_member_are_repaired(self):
        self.apply()
        cid = self.fake.channel("team-01")["id"]
        self.fake.members[cid].discard(BOB)
        code, report = self.apply(self.spec(header="synthetic header"))
        self.assertEqual((code, report["members_added"], report["items"]["team-01"]), (0, 1, "updated"))
        self.assertEqual(self.fake.channel("team-01")["header"], "synthetic header")
        self.assertIn(BOB, self.fake.members[cid])

    def test_same_name_with_another_display_name_is_never_adopted(self):
        cid = self.fake.new("c")
        self.fake.channels[cid] = {"id": cid, "team_id": TEAM, "name": "team-01", "display_name": "Other",
                                   "type": "P", "delete_at": 0}
        self.fake.members[cid], self.fake.posts[cid] = {ME}, []
        code, report = self.apply()
        self.assertEqual((code, report["items"]["team-01"], report["items"]["team-02"]),
                         (1, "failed:conflict", "created"))
        self.assertEqual(self.fake.members[cid], {ME})
        self.assertNotIn("team-01", self.stored()["channels"])
        self.assertEqual(self.run_admin("plan", "--spec", self.spec())[1]["items"]["team-01"], "conflict")

    def test_refusals_write_nothing(self):
        for members, category in (("alice", "zed"), "unknown_users"), (("alice", "carol"), "not_in_team"):
            code, report = self.run_admin("apply", "--spec", self.spec(members=members), "--out", str(self.result))
            self.assertEqual((code, report["status"]), (2, category))
        for out in (str(Path(__file__).resolve()), "result.json"):
            self.fake.seen.clear()
            code, report = self.run_admin("apply", "--spec", self.spec(), "--out", out)
            self.assertEqual((code, report["status"]), (2, "unsafe_result_path"))
            self.assertEqual(self.fake.seen, [])
        self.assertEqual((self.fake.writes, self.result.exists()), ([], False))

    def test_a_result_of_another_team_is_never_overwritten(self):
        self.result.write_text(json.dumps({"team_id": ident("t", 9), "channels": {"x": {"hook_id": "kept"}}}),
                               encoding="utf-8")
        before = self.result.read_text(encoding="utf-8")
        code, report = self.apply()
        self.assertEqual((code, report["status"], report["writes"]), (2, "result_other_team", 0))
        self.assertEqual((self.fake.writes, self.result.read_text(encoding="utf-8")), ([], before))

    def test_a_write_is_sent_once_and_a_rerun_adopts_the_result(self):
        self.fake.drop = ("POST", "/channels")
        spec = self.spec(count=1, members=(), webhook=False)
        code, report = self.apply(spec)
        self.assertEqual((code, report["items"]["team-01"]), (1, "failed:transport_error"))
        self.assertEqual(self.fake.writes, [("POST", "/api/v4/channels")])
        self.assertEqual(len(self.fake.channels), 1)                # the server did create it
        code, report = self.apply(spec)
        self.assertEqual((code, report["items"]["team-01"]), (0, "unchanged"))
        self.assertEqual((len(self.fake.channels), self.fake.writes.count(("POST", "/api/v4/channels"))), (1, 1))
        self.assertEqual(self.stored()["channels"]["team-01"]["channel_id"], next(iter(self.fake.channels)))

    def test_unauthorized(self):
        code, report = self.run_admin("plan", "--spec", self.spec(), token="wrong-token")
        self.assertEqual((code, report["status"], report["http_status"]), (2, "auth_required", 401))


class TestVerify(Base):
    def setUp(self):
        super().setUp()
        self.apply()
        self.cid = self.fake.channel("team-01")["id"]

    def verify(self):
        return self.run_admin("verify", "--spec", self.spec(), "--out", str(self.result))

    def test_passes_then_fails_on_each_difference(self):
        code, report = self.verify()
        self.assertEqual((code, report["status"], report["problems"]), (0, "ok", {}))
        hook = next(h for h in self.fake.hooks.values() if h["channel_id"] == self.cid)
        for label, mutate, undo, category in (
                ("extra member", lambda: self.fake.members[self.cid].add(CAROL),
                 lambda: self.fake.members[self.cid].discard(CAROL), "members"),
                ("unlocked hook", lambda: hook.update(channel_locked=False),
                 lambda: hook.update(channel_locked=True), "hook_unlocked"),
                ("second hook", lambda: self.fake.hooks.update(h999={**hook, "id": ident("h", 999)}),
                 lambda: self.fake.hooks.pop("h999"), "hook_count"),
                ("deleted channel", lambda: self.fake.channels[self.cid].update(delete_at=5),
                 lambda: self.fake.channels[self.cid].update(delete_at=0), "archived")):
            with self.subTest(label):
                mutate()
                code, report = self.verify()
                self.assertEqual(code, 1)
                self.assertIn(category, report["items"]["team-01"])
                self.assertEqual(report["items"]["team-02"], "ok")
                undo()
        self.assertEqual(self.verify()[0], 0)

    def test_a_result_that_no_longer_matches_fails(self):
        res = self.stored()
        res["channels"]["team-01"]["webhook_url"] = "https://other.example/hooks/x"
        self.result.write_text(json.dumps(res), encoding="utf-8")
        code, report = self.verify()
        self.assertEqual((code, report["problems"]), (1, {"result_url": 1}))


class TestPost(Base):
    def setUp(self):
        super().setUp()
        self.apply(self.spec(count=1, members=("alice",)))
        self.cid = self.fake.channel("team-01")["id"]
        self.fake.writes.clear()

    def post(self, *extra, via_webhook=False):
        args = ["post", "--spec", self.spec(count=1, members=("alice",)), "--key", "team-01",
                "--message-file", self.message(), *extra]
        return self.run_admin(*args, *(["--out", str(self.result), "--via-webhook"] if via_webhook else []))

    def test_post_is_at_most_once(self):
        code, report = self.run_admin("post", "--channel-id", self.cid, "--message-file", self.message(), "--dry-run")
        self.assertEqual((code, report["status"], report["items"]), (0, "dry_run", {"post": "post"}))
        self.assertEqual(self.fake.writes, [])
        self.assertEqual(self.post()[1]["status"], "posted")
        code, report = self.post()
        self.assertEqual((code, report["status"]), (0, "already_posted"))
        self.assertEqual([w for w in self.fake.writes if w[1].endswith("/posts")], [("POST", "/api/v4/posts")])
        self.assertEqual([p["message"] for p in self.fake.posts[self.cid]], [MESSAGE])

    def test_an_error_after_a_write_is_partial_not_refused(self):
        self.fake.drop = ("POST", "/posts")
        code, report = self.post()
        self.assertEqual((code, report["status"], report["writes"]), (1, "transport_error", 1))
        self.assertEqual(self.post()[1]["status"], "already_posted")        # the server did post it

    def test_a_trailing_newline_does_not_defeat_at_most_once(self):
        self.run_admin("post", "--channel-id", self.cid, "--message-file", self.message(MESSAGE + "\n"))
        self.assertEqual(self.post()[1]["status"], "already_posted")
        self.assertEqual([p["message"] for p in self.fake.posts[self.cid]], [MESSAGE])

    def test_webhook_post_carries_no_credentials(self):
        code, report = self.post(via_webhook=True)
        self.assertEqual((code, report["status"], report["via"]), (0, "posted", "webhook"))
        req = self.fake.seen[-1]
        self.assertTrue(req.full_url.startswith(URL + "/hooks/"))
        self.assertFalse(req.has_header("Authorization"))
        self.assertNotIn(TOKEN.encode(), req.data + req.full_url.encode())
        self.assertEqual(json.loads(req.data), {"text": MESSAGE})
        self.assertEqual(self.post(via_webhook=True)[1]["status"], "already_posted")
        self.assertEqual(len(self.fake.posts[self.cid]), 1)

    def test_webhook_url_must_stay_on_the_server(self):
        res = self.stored()
        res["channels"]["team-01"]["webhook_url"] = "https://other.example/hooks/x"
        self.result.write_text(json.dumps(res), encoding="utf-8")
        self.fake.seen.clear()
        code, report = self.post(via_webhook=True)
        self.assertEqual((code, report["status"], self.fake.seen), (2, "bad_webhook_url", []))

    def test_pin(self):
        code, report = self.post("--pin")
        self.assertEqual((code, report["status"], report["pinned"]), (0, "posted", True))
        self.assertEqual(self.fake.pinned, {self.fake.posts[self.cid][0]["id"]})

    def test_pin_of_a_webhook_post_finds_it_by_message(self):
        code, report = self.post("--pin", via_webhook=True)
        self.assertEqual((code, report["pinned"]), (0, True))
        self.assertEqual(self.fake.pinned, {self.fake.posts[self.cid][0]["id"]})

    def test_arguments_and_message(self):
        for argv, category in ((["--spec", self.spec(), "--message-file", self.message()], "bad_arguments"),
                               (["--channel-id", self.cid, "--via-webhook", "--message-file", self.message()],
                                "bad_arguments"),
                               (["--spec", self.spec(count=1, members=("alice",)), "--key", "team-01", "--out",
                                 str(self.result), "--via-webhook", "--root-id", ident("p", 1),
                                 "--message-file", self.message()], "bad_arguments"),
                               (["--channel-id", self.cid, "--message-file", self.message("  \n")], "empty_message")):
            code, report = self.run_admin("post", *argv)
            self.assertEqual((code, report["status"]), (2, category))


if __name__ == "__main__":
    unittest.main()
