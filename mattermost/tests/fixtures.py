# -*- coding: utf-8 -*-
"""Synthetic inputs of the mattermost app built in code (no data files are committed). Every value is made up; ids stay
short so they cannot look like real Mattermost ids."""
import json
import os
import tempfile
from pathlib import Path

from rawlayer.capture import paths

# Test data roots live under %TEMP%, outside the production data parent. Config refuses a data root outside
# paths.DATA_PARENT (SPEC 10.1), so the test process points it at the temp directory; a child process that
# builds a Config must do the same (TEST_PARENT). TEST_PARENT stays the str tempfile gives: it is also handed to
# child processes as JSON text.
TEST_PARENT = tempfile.gettempdir()
paths.DATA_PARENT = Path(TEST_PARENT)


class Fixture:
    """The mm_archive input (an archive whose manifest has no channels yet) plus a data root, laid out like
    config.json expects."""

    def __init__(self, base=None):
        self.base = Path(base or tempfile.mkdtemp(prefix="llmw-"))
        b = self.base
        self.inputs = b / "in"
        self.data_root = b / "data"
        self.mm = self.inputs / "mm" / "archive"
        for d in (self.mm, self.data_root):
            d.mkdir(parents=True, exist_ok=True)
        with (self.mm / "manifest.json").open("w", encoding="utf-8") as fh:
            json.dump({"channels": {}}, fh)
        self.config_path = b / "config.json"
        self.write_config()

    def write_config(self, data_root=None):
        frozen = self.base / "frozen"
        for d in (frozen, data_root or self.data_root):
            Path(d).mkdir(parents=True, exist_ok=True)
        # config.json holds text: a data_root the caller passed as a str is written as given
        cfg = {"v": 1, "impl": "claude", "data_root": os.fspath(data_root or self.data_root),
               "inputs_root": str(frozen), "sources": {"mm_archive": str(self.mm)}, "mm_exclude_patterns": []}
        with self.config_path.open("w", encoding="utf-8") as fh:
            json.dump(cfg, fh)
        return self.config_path

    def mm_write(self, channels, users=None, partial_tail=None, orphan=False):
        """channels: {cid: {"path": rel, "posts": [post dict...] | None (no file), "display": str, ...}}.
        Lines are CRLF like mattertok writes them; json keeps U+2028 raw (ensure_ascii=False)."""
        man = {"version": 1, "channels": {}}
        for cid, c in channels.items():
            posts = c.get("posts")
            entry = {"channel_id": cid, "type": "O", "team_name": "t", "channel_name": c.get("name", "n" + cid),
                     "display": c.get("display", "d" + cid), "inaccessible_before": 0, "oldest_cursor": "",
                     "newest_post_id": "", "newest_create_at": 0, "backfill_done": True,
                     "post_count": c.get("post_count", len({p["id"] for p in posts or []})), "expected": 0,
                     "error": "", "path": c["path"]}
            man["channels"][cid] = entry
            p = self.mm.joinpath("posts", *c["path"].split("/"))
            if posts is None:
                p.unlink(missing_ok=True)
                continue
            p.parent.mkdir(parents=True, exist_ok=True)
            data = "".join(json.dumps(x, ensure_ascii=False) + "\r\n" for x in posts).encode("utf-8")
            if partial_tail == cid:
                data += b'{"id": "half'
            p.write_bytes(data)
        if orphan:
            p = self.mm / "posts" / "stray.jsonl"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"{}\r\n")
        with (self.mm / "manifest.json").open("w", encoding="utf-8") as fh:
            json.dump(man, fh, ensure_ascii=False)
        with (self.mm / "users.json").open("w", encoding="utf-8") as fh:
            json.dump(users if users is not None else {"u1": {"username": "a", "first_name": "", "last_name": "",
                                                              "nickname": ""}}, fh)
        return man


def post(pid, cid, msg="hi", create_at=1_600_000_000_000, files=(), root="", user="u1", **extra):
    p = {"id": pid, "channel_id": cid, "create_at": create_at, "update_at": create_at, "edit_at": 0,
         "delete_at": 0, "user_id": user, "root_id": root, "message": msg, "type": "", "props": {},
         "hashtags": "", "file_ids": [f["id"] for f in files], "pending_post_id": "", "reply_count": 0,
         "last_reply_at": 0, "participants": None, "is_pinned": False, "original_id": "",
         "metadata": {"files": list(files)} if files else {}}
    p.update(extra)
    return p


def mm_file(fid, pid, cid, name="f.txt"):
    return {"id": fid, "post_id": pid, "channel_id": cid, "user_id": "u1", "name": name, "extension": "txt",
            "size": 3, "mime_type": "text/plain", "create_at": 1, "update_at": 1, "delete_at": 0,
            "remote_id": "", "archived": False, "mini_preview": None}
