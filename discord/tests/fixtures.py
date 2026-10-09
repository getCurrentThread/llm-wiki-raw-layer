import json
import tempfile
from pathlib import Path
from rawlayer.capture import paths

TEST_PARENT = tempfile.gettempdir()
paths.DATA_PARENT = Path(TEST_PARENT)
CID = "100"
MID = "1300000000000000000"
UID = "200"


def message(mid=MID, text="synthetic deployment", **extra):
    return {"id": mid, "type": "Default", "content": text, "author": {"id": UID, "name": "author"},
            "timestamp": "2024-10-01T00:00:00Z", "isPinned": False, "attachments": [], "embeds": [], **extra}


class Fixture:
    def __init__(self):
        self.base = Path(tempfile.mkdtemp(prefix="dc-test-"))
        self.root = self.base / "archive"
        self.data_root = self.base / "data"
        self.root.mkdir()
        self.data_root.mkdir()
        self.config_path = self.base / "config.json"
        self.write_config()
        self.write({CID: [[message()]]})

    def write_config(self, patterns=()):
        self.config_path.write_text(json.dumps({"v": 1, "impl": "claude", "data_root": str(self.data_root),
                                              "sources": {"dc_archive": str(self.root)},
                                              "dc_exclude_patterns": list(patterns)}), encoding="utf-8")

    def save(self, path, value):
        p = self.root / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(value), encoding="utf-8")

    def write(self, channels, guild="300", parents=None):
        man = {"version": 1, "generation": "synthetic", "guilds": {guild: {"id": guild, "name": "Guild"}}, "channels": {}}
        for cid, partitions in channels.items():
            files = []
            for i, msgs in enumerate(partitions):
                rel = f"exports/{guild}/{cid}/20241001T000000{i:06d}Z.json"
                self.save(rel, {"guild": {"id": guild}, "channel": {"id": cid}, "messageCount": len(msgs), "messages": msgs})
                files.append(rel)
            man["channels"][cid] = {"id": cid, "guild_id": guild, "type": "GuildTextChat", "name": "channel" + cid,
                                     "category": "category", "parent_id": (parents or {}).get(cid), "files": files}
        self.save("manifest.json", man)
        self.save("users.json", {UID: {"id": UID, "name": "Synthetic", "nicks": {guild: "Alias"}}})
        return man
