import json
import tempfile
from pathlib import Path
from rawlayer.capture import paths

TEST_PARENT = tempfile.gettempdir()
paths.DATA_PARENT = Path(TEST_PARENT)
PID = "00000000-0000-0000-0000-000000000001"
BID = "00000000-0000-0000-0000-000000000002"
UID = "00000000-0000-0000-0000-000000000003"
OTHER = "00000000-0000-0000-0000-000000000004"
COMMENT = "00000000-0000-0000-0000-000000000005"


def rt(text):
    return [{"type": "text", "text": {"content": text}, "plain_text": text}]


def block(bid=BID, text="synthetic deployment", **extra):
    return {"id": bid, "type": "paragraph", "paragraph": {"rich_text": rt(text)},
            "created_time": "2024-10-01T00:00:00Z", "last_edited_time": "2024-10-01T00:00:00Z",
            "last_edited_by": {"id": UID}, **extra}


def page(pid=PID, title="Synthetic page", blocks=None, order=None, **extra):
    blocks = blocks if blocks is not None else {BID: block()}
    return {"page": {"id": pid, "properties": {"Name": {"type": "title", "title": rt(title)}},
                     "parent": {"type": "workspace", "workspace": True}, "in_trash": False,
                     "last_edited_time": "2024-10-01T00:00:00Z", "last_edited_by": {"id": UID}},
            "blocks": blocks, "order": order if order is not None else [[k, 0, None] for k in blocks], "comments": {}, **extra}


class Fixture:
    def __init__(self):
        self.base = Path(tempfile.mkdtemp(prefix="nt-test-"))
        self.root, self.data_root = self.base / "archive", self.base / "data"
        self.root.mkdir()
        self.data_root.mkdir()
        self.config_path = self.base / "config.json"
        self.config_path.write_text(json.dumps({"v": 1, "impl": "claude", "data_root": str(self.data_root),
                                              "sources": {"nt_archive": str(self.root)}}), encoding="utf-8")
        self.save("manifest.json", {"version": 1, "generation": "synthetic", "pages": {}, "data_sources": {}})
        self.save("users.json", {UID: {"id": UID, "name": "Synthetic"}})
        self.write(page())

    def save(self, rel, value):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(value), encoding="utf-8")

    def write(self, value):
        pid = value["page"]["id"]
        self.save(f"pages/{pid[:2]}/{pid}.json", value)
