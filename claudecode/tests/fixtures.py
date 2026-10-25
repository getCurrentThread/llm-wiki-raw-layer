import json
import tempfile
import uuid
from pathlib import Path
from rawlayer.capture import paths

TEST_PARENT = tempfile.gettempdir()
paths.DATA_PARENT = Path(TEST_PARENT)
CID = "session-one"
MID = "message-one"


def row(uuid=MID, text="synthetic deployment", **extra):
    return {"session": CID, "agent": "", "uuid": uuid, "parent": "", "n": 0,
            "ts": "2024-10-01T00:00:00.000Z", "role": "user", "cwd": "/synthetic/project",
            "branch": "main", "model": "", "text": text, "tools": [], "from": "transcript", **extra}


message = row


class Fixture:
    def __init__(self):
        self.base = Path(tempfile.mkdtemp(prefix="cc-test-"))
        self.root = self.base / "archive"
        self.data_root = self.base / "data"
        self.root.mkdir()
        self.data_root.mkdir()
        self.config_path = self.base / "config.json"
        self.write_config()
        self.write({CID: [[row()]]})

    def write_config(self, **options):
        self.config_path.write_text(json.dumps({"v": 1, "impl": "claude", "data_root": str(self.data_root),
                                              "sources": {"cc_archive": str(self.root)}, **options}), encoding="utf-8")

    def save(self, path, value):
        p = self.root / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(value), encoding="utf-8")

    def write(self, containers):
        man = {"version": 1, "generation": uuid.uuid4().hex, "files": {}, "containers": {}}
        for cid, partitions in containers.items():
            files = []
            for i, rows in enumerate(partitions):
                rel = f"exports/{cid}/20241001T000000{i:06d}Z.json"
                self.save(rel, {"container": cid, "exportedAt": "2024-10-01T00:00:00Z",
                                "rows": rows, "rowCount": len(rows)})
                files.append(rel)
            man["containers"][cid] = {"id": cid, "kind": "session", "session": cid, "agent": "",
                                      "project": "synthetic", "agent_type": "", "description": "", "files": files}
        self.save("manifest.json", man)
        return man
