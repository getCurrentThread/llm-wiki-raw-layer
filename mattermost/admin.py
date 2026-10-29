# -*- coding: utf-8 -*-
"""Mattermost administration over REST API v4: the write-side counterpart of the read-only collector, replacing
ad-hoc scripts (a private channel per team with its members and one incoming webhook, channel headers, messages
sent through a webhook and pinned, mattertok's `send`).

A spec file (JSON, it names people, so the owner keeps it under the data parent) describes a team's channels:

  {"team": "team-a", "channels": {"<key>": {"name": "...", "display_name": "...", "type": "P", "purpose": "...",
   "header": "...", "members": ["alice"], "webhook": {"display_name": "...", "description": "..."}}}}

  plan    read-only: what apply would do (new / exists / conflict), unknown users and non-team members as counts
  apply   ensure channels, members and locked hooks, then write RESULT (key -> channel id, hook id, webhook URL)
  verify  re-read the API and compare it with the spec and RESULT: members EXACTLY {token user} + spec members
  post    send one message as the token user or through a channel's webhook, at most once, optionally pin it

Writes are never retried: MattermostClient.request retries 429/5xx/transport failures, which is right for GETs but
could create a channel, hook or post twice after a timeout. AdminClient sends every write once; every create is
preceded by a lookup, so a rerun after an uncertain write adopts what exists instead of duplicating. A GET 404 means
"absent". A channel is adopted only when its type and display name match the spec, and a hook is always created
with channel_locked (otherwise its URL could post into any other channel the token's user belongs to).

RESULT holds webhook URLs (secrets), so it must be an absolute path inside the data parent, like the token file.
Output is one JSON line with counts and per-key statuses; tokens, webhook URLs, display names and usernames are never
printed; `writes` counts the writes attempted. Exit codes: 0 ok, 1 some items failed or an error came after a
write (the rest is done, rerunning continues), 2 refused before any write (bad spec or config, auth_required,
unknown user, user not in the team, unsafe result path, a RESULT that belongs to another team)."""
import argparse
import json
from pathlib import Path
import re
import sys
from urllib import error, request

from mattermost.collector import TRANSPORT, MattermostClient, _id, base_url, credential_path, token
from tools.archiveio import CollectorError, atomic, load, now

PER_PAGE = 200
RECENT = 60                 # posts read to find an identical message (at-most-once) or the post to pin
_KEY = re.compile(r"[A-Za-z0-9_-]{1,64}")
_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{1,63}")
_TEAM = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")


def bad(category):
    raise CollectorError(category)


class AdminClient(MattermostClient):
    writes = 0                  # writes attempted: an error after one is a partial run (exit 1), not a refusal

    def _once(self, req):
        """One attempt, paced like every request; HTTP errors and transport failures become CollectorErrors."""
        self._pace()
        try:
            self.requests += 1
            with self.opener(req, timeout=60) as response:
                return response.read()
        except error.HTTPError as e:
            status = e.code
            e.close()
            raise CollectorError("auth_required" if status == 401 else "api_error", status) from None
        except TRANSPORT:
            raise CollectorError("transport_error") from None

    def write(self, method, path, body=None):
        if not re.fullmatch(r"/[A-Za-z0-9_/-]+", path):
            bad("bad_endpoint")
        headers = {"Authorization": "Bearer " + self.credential, "Accept": "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        self.writes += 1
        raw = self._once(request.Request(self.api + path, data=data, headers=headers, method=method))
        try:
            return json.loads(raw) if raw else None
        except ValueError:
            raise CollectorError("bad_response") from None

    def webhook(self, url, text):
        """POST to an incoming webhook: no Authorization header, the URL is the secret."""
        self.writes += 1
        self._once(request.Request(url, data=json.dumps({"text": text}).encode("utf-8"), method="POST",
                                   headers={"Content-Type": "application/json"}))

    def find(self, path, query=None):
        """GET, None when the server answers 404 (absent); any other error raises."""
        try:
            return self.request("GET", path, query)
        except CollectorError as e:
            if e.status == 404:
                return None
            raise


# ------------------------------------------------------------------------------------------------------------ spec

def _text(value, category, limit):
    if not isinstance(value, str) or len(value) > limit:
        bad(category)
    return value


def load_spec(path):
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        bad("bad_spec")
    if not isinstance(raw, dict) or not isinstance(raw.get("team"), str) or not _TEAM.fullmatch(raw["team"]):
        bad("spec_team")
    if not isinstance(raw.get("channels"), dict) or not raw["channels"]:
        bad("spec_channels")
    names, channels = set(), {}
    for key, c in raw["channels"].items():
        if not _KEY.fullmatch(key) or not isinstance(c, dict):
            bad("spec_key")
        if not isinstance(c.get("name"), str) or not _NAME.fullmatch(c["name"]):
            bad("spec_name")
        if c["name"] in names:
            bad("spec_name_duplicate")
        names.add(c["name"])
        shown = c.get("display_name")
        if not isinstance(shown, str) or not 1 <= len(shown) <= 64:
            bad("spec_display_name")
        if c.get("type", "P") not in ("O", "P"):
            bad("spec_type")
        members = c.get("members", [])
        if not isinstance(members, list) or any(not isinstance(m, str) or not m.strip() for m in members) \
                or len({m.lower() for m in members}) != len(members):
            bad("spec_members")
        item = {"name": c["name"], "display_name": shown, "type": c.get("type", "P"),
                "members": [m.lower() for m in members]}
        for field in ("purpose", "header"):
            if field in c:
                item[field] = _text(c[field], "spec_text", 1024)
        if "webhook" in c:
            hook = c["webhook"]
            if not isinstance(hook, dict):
                bad("spec_webhook")
            item["webhook"] = {"display_name": _text(hook.get("display_name", shown), "spec_webhook", 64) or shown,
                               "description": _text(hook.get("description", ""), "spec_webhook", 500)}
        channels[key] = item
    return raw["team"], channels


def result_path(value):
    """RESULT holds webhook URLs: an absolute path inside the data parent, checked before anything else."""
    try:
        return credential_path(value)
    except CollectorError:
        bad("unsafe_result_path")


def read_result(path, team_id=None):
    data = load(path, {})
    if not isinstance(data, dict) or not isinstance(data.get("channels", {}), dict):
        bad("bad_result")
    # another team's RESULT is never overwritten: its webhook URLs would be lost
    if team_id is not None and data.get("team_id", team_id) != team_id:
        bad("result_other_team")
    return data


# --------------------------------------------------------------------------------------------------------- the API

def open_client(a):
    from rawlayer import registry
    from rawlayer.capture.config import Config
    from rawlayer.management.base import default_config
    registry.select("mattermost")
    cfg = Config(a.config or default_config("mattermost"))
    a.client = AdminClient(cfg.mm_url, token(cfg.mm_token_file))
    return cfg, a.client


def get_team(client, name):
    team = client.find(f"/teams/name/{name}")
    if not isinstance(team, dict):
        bad("unknown_team")
    return _id(team.get("id"))


def lookup_users(client, channels):
    """({username: user id}, usernames the server does not know) for every member named in the spec."""
    wanted = sorted({m for c in channels.values() for m in c["members"]})
    found = {}
    for i in range(0, len(wanted), 100):
        rows = client.request("POST", "/users/usernames", body=wanted[i:i + 100])
        if not isinstance(rows, list):
            bad("bad_response")
        for u in rows:
            if isinstance(u, dict) and isinstance(u.get("username"), str):
                found[u["username"].lower()] = _id(u.get("id"))
    return found, [m for m in wanted if m not in found]


def outside_team(client, team_id, users):
    """Usernames that are not active members of the team (delete_at > 0 is a deactivated membership)."""
    out = []
    for name, uid in sorted(users.items()):
        m = client.find(f"/teams/{team_id}/members/{uid}")
        if not isinstance(m, dict) or (m.get("delete_at") or 0) > 0:
            out.append(name)
    return out


def hooks_of(client, team_id, channel_id):
    out, page = [], 0
    while True:
        rows = client.request("GET", "/hooks/incoming", {"team_id": team_id, "page": page, "per_page": PER_PAGE})
        if not isinstance(rows, list):
            bad("bad_response")
        out += [h for h in rows if isinstance(h, dict) and h.get("channel_id") == channel_id and not h.get("delete_at")]
        if len(rows) < PER_PAGE:
            return out
        page += 1


def members_of(client, channel_id):
    out, page = set(), 0
    while True:
        rows = client.request("GET", f"/channels/{channel_id}/members", {"page": page, "per_page": PER_PAGE})
        if not isinstance(rows, list):
            bad("bad_response")
        out.update(r.get("user_id") for r in rows if isinstance(r, dict))
        if len(rows) < PER_PAGE:
            return out
        page += 1


def channel_by_name(client, team_id, name):
    ch = client.find(f"/teams/{team_id}/channels/name/{name}")
    if ch is not None and not isinstance(ch, dict):
        bad("bad_response")
    return None if ch is None else dict(ch, id=_id(ch.get("id")))


def differs(ch, c, team_id):
    """Why an existing channel is not the one the spec describes ('' when it is)."""
    if (ch.get("delete_at") or 0) > 0:
        return "archived"
    if ch.get("team_id") != team_id:
        return "team"
    if ch.get("type") != c["type"]:
        return "type"
    return "display_name" if ch.get("display_name") != c["display_name"] else ""


# ------------------------------------------------------------------------------------------------------- commands

def report(status, items, ok=True, **counts):
    return {"status": status, **counts, "items": items}, 0 if ok else 1


def cmd_plan(a):
    team, channels = load_spec(a.spec)
    _cfg, client = open_client(a)
    client.authenticate()
    team_id = get_team(client, team)
    users, unknown = lookup_users(client, channels)
    missing = outside_team(client, team_id, users)
    items = {}
    for key, c in channels.items():
        ch = channel_by_name(client, team_id, c["name"])
        items[key] = "new" if ch is None else "conflict" if differs(ch, c, team_id) else "exists"
    counts = {s: sum(v == s for v in items.values()) for s in ("new", "exists", "conflict")}
    ok = not (unknown or missing or counts["conflict"])
    return report("ok" if ok else "problems", items, ok, channels=len(items), **counts,
                  unknown_users=len(unknown), not_in_team=len(missing))


def apply_item(client, team_id, me, users, c, entry, st):
    """Ensure one channel; `entry` (the RESULT row) is filled as soon as each id is known. Returns the status."""
    ch, changed = channel_by_name(client, team_id, c["name"]), False
    if ch is None:
        body = {"team_id": team_id, "name": c["name"], "display_name": c["display_name"], "type": c["type"]}
        body.update({k: c[k] for k in ("purpose", "header") if k in c})
        ch = client.write("POST", "/channels", body)
        if not isinstance(ch, dict):
            bad("bad_response")
        ch, changed = dict(ch, id=_id(ch.get("id"))), True
        st["created"] += 1
    elif differs(ch, c, team_id):
        bad("conflict")
    cid = entry["channel_id"] = ch["id"]
    entry["name"] = c["name"]
    patch = {k: c[k] for k in ("header", "purpose") if k in c and ch.get(k, "") != c[k]}
    if patch:
        client.write("PUT", f"/channels/{cid}/patch", patch)
        changed = True
    for name in c["members"]:
        uid = users[name]
        if uid != me and client.find(f"/channels/{cid}/members/{uid}") is None:
            client.write("POST", f"/channels/{cid}/members", {"user_id": uid})
            st["members_added"] += 1
            changed = True
    if "webhook" in c:
        hooks = hooks_of(client, team_id, cid)
        if len(hooks) > 1:
            bad("several_hooks")
        if hooks and not hooks[0].get("channel_locked"):
            bad("hook_unlocked")
        if not hooks:
            hook = client.write("POST", "/hooks/incoming", {"channel_id": cid, "channel_locked": True,
                                                            **c["webhook"]})
            hooks = [hook] if isinstance(hook, dict) else bad("bad_response")
            st["hooks_created"] += 1
            changed = True
        entry["hook_id"] = _id(hooks[0].get("id"))
        entry["webhook_url"] = client.api[:-len("/api/v4")] + "/hooks/" + entry["hook_id"]
    return "updated" if changed else "unchanged"


def cmd_apply(a):
    team, channels = load_spec(a.spec)
    out = result_path(a.out)
    _cfg, client = open_client(a)
    client.authenticate()
    team_id = get_team(client, team)
    users, unknown = lookup_users(client, channels)
    if unknown:
        bad("unknown_users")
    if outside_team(client, team_id, users):
        bad("not_in_team")
    res = read_result(out, team_id)
    res.update(team_id=team_id)
    rows = res.setdefault("channels", {})
    items, st = {}, {"created": 0, "members_added": 0, "hooks_created": 0}
    try:
        for key, c in channels.items():
            entry, before = {}, st["created"]
            try:
                status = apply_item(client, team_id, client.user_id, users, c, entry, st)
                items[key] = "created" if st["created"] > before else status
            except CollectorError as e:
                if e.category == "auth_required":
                    raise
                items[key] = "failed:" + e.category
            if entry:
                rows[key] = {**rows.get(key, {}), **entry}
    finally:
        res["updated_at"] = now()
        atomic(out.parent, out, res)
    failed = sum(v.startswith("failed") for v in items.values())
    return report("ok" if not failed else "partial", items, not failed, channels=len(items), **st, errors=failed)


def cmd_verify(a):
    team, channels = load_spec(a.spec)
    out = result_path(a.out)
    _cfg, client = open_client(a)
    client.authenticate()
    team_id = get_team(client, team)
    users, _unknown = lookup_users(client, channels)
    res = read_result(out)
    items, problems = {}, {}
    for key, c in channels.items():
        found, row = [], res.get("channels", {}).get(key)
        ch = channel_by_name(client, team_id, c["name"])
        if ch is None:
            found.append("channel_missing")
        else:
            why = differs(ch, c, team_id)
            found += [why] if why else []
            found += ["header_" + k for k in ("header", "purpose") if k in c and ch.get(k, "") != c[k]]
            if not isinstance(row, dict) or row.get("channel_id") != ch["id"]:
                found.append("result_mismatch")
            want = {users.get(m) for m in c["members"]} | {client.user_id}
            if None in want:
                found.append("unknown_users")
            elif members_of(client, ch["id"]) != want:
                found.append("members")
            if "webhook" in c:
                hooks = hooks_of(client, team_id, ch["id"])
                if len(hooks) != 1:
                    found.append("hook_count")
                else:
                    h, row = hooks[0], row if isinstance(row, dict) else {}
                    found += [name for name, bad_ in (
                        ("hook_id", h.get("id") != row.get("hook_id")),
                        ("hook_unlocked", h.get("channel_locked") is not True),
                        ("hook_display_name", h.get("display_name") != c["webhook"]["display_name"]),
                        ("result_url", row.get("webhook_url") != f"{client.api[:-len('/api/v4')]}/hooks/{h.get('id')}"),
                    ) if bad_]
        items[key] = "ok" if not found else "differs:" + ",".join(found)
        for f in found:
            problems[f] = problems.get(f, 0) + 1
    return report("ok" if not problems else "differs", items, not problems, channels=len(items), problems=problems)


def recent(client, channel_id):
    data = client.request("GET", f"/channels/{channel_id}/posts", {"per_page": RECENT})
    if not isinstance(data, dict) or not isinstance(data.get("posts") or {}, dict):
        bad("bad_response")
    posts = data.get("posts") or {}
    return [posts[p] for p in data.get("order") or [] if isinstance(posts.get(p), dict)]     # newest first


def cmd_post(a):
    if bool(a.channel_id) == bool(a.spec) or (a.spec and not a.key) or (a.channel_id and (a.key or a.via_webhook)) \
            or (a.via_webhook and (not a.out or a.root_id)):
        bad("bad_arguments")
    try:
        # trailing whitespace (an editor's final newline) is dropped, so the at-most-once match is exact
        message = Path(a.message_file).read_text(encoding="utf-8").rstrip()
    except (OSError, ValueError):
        bad("bad_message_file")
    if not message.strip():
        bad("empty_message")
    out, row, c = (result_path(a.out) if a.out else None), {}, None
    if a.spec:
        team, channels = load_spec(a.spec)
        if a.key not in channels:
            bad("unknown_key")
        c = channels[a.key]
        if out:
            row = read_result(out).get("channels", {}).get(a.key)
            if not isinstance(row, dict):
                bad("no_result")
    cfg, client = open_client(a)
    url = row.get("webhook_url") if a.via_webhook else None
    if a.via_webhook and (not isinstance(url, str) or not url.startswith(base_url(cfg.mm_url) + "/hooks/")):
        bad("bad_webhook_url")
    client.authenticate()
    cid = a.channel_id or row.get("channel_id")
    if not cid:
        ch = channel_by_name(client, get_team(client, team), c["name"])
        cid = ch["id"] if ch else bad("channel_missing")
    cid = _id(cid)
    same = next((p for p in recent(client, cid) if p.get("message") == message and not p.get("delete_at")), None)
    if a.dry_run:
        return report("dry_run", {"post": "skip" if same else "post"}, via="webhook" if url else "api")
    status = "already_posted"
    if not same:
        if url:
            client.webhook(url, message)
        else:
            same = client.write("POST", "/posts", {"channel_id": cid, "message": message,
                                                   **({"root_id": _id(a.root_id)} if a.root_id else {})})
        status = "posted"
    pinned = False
    if a.pin:
        if not isinstance(same, dict) or not same.get("id"):
            same = next((p for p in recent(client, cid) if p.get("message") == message), None)
        if isinstance(same, dict) and same.get("is_pinned") is not True and same.get("id"):
            client.write("POST", f"/posts/{_id(same['id'])}/pin")
        pinned = isinstance(same, dict)
    ok = pinned or not a.pin
    return report(status if ok else "pin_failed", {}, ok, via="webhook" if url else "api", pinned=pinned)


COMMANDS = {"plan": cmd_plan, "apply": cmd_apply, "verify": cmd_verify, "post": cmd_post}


def main(argv=None):
    p = argparse.ArgumentParser(prog="mattermost.admin", description="Mattermost administration (see the module doc)")
    sub = p.add_subparsers(dest="command", required=True)
    for name in COMMANDS:
        s = sub.add_parser(name)
        s.add_argument("--config")
        s.add_argument("--spec", required=name != "post")
        s.add_argument("--out", required=name in ("apply", "verify"))
    s = sub.choices["post"]
    s.add_argument("--key")
    s.add_argument("--channel-id")
    s.add_argument("--message-file", required=True)
    s.add_argument("--via-webhook", action="store_true")
    s.add_argument("--root-id")
    s.add_argument("--pin", action="store_true")
    s.add_argument("--dry-run", action="store_true")
    a = p.parse_args(sys.argv[1:] if argv is None else argv)
    try:
        body, code = COMMANDS[a.command](a)
    except CollectorError as e:
        body, code = {"status": e.category, **({"http_status": e.status} if e.status else {})}, 2
    except Exception as e:
        body, code = {"status": "error", "error_type": type(e).__name__}, 2
    if code == 2 and getattr(getattr(a, "client", None), "writes", 0):
        code = 1                # something was already written: a partial run, rerunning continues
    body["writes"] = getattr(getattr(a, "client", None), "writes", 0)
    print(json.dumps({"app": "mattermost", "command": a.command, **body}))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
