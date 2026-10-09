import base64
import copy
from email import policy
from email.message import EmailMessage
import io
import json
from pathlib import Path
import tempfile
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit
from rawlayer.capture import paths
from gmail.api import GmailClient

paths.DATA_PARENT = Path(tempfile.gettempdir())
MID, OTHER, THREAD = "abc123", "abc124", "def123"


def raw_message(ident=MID, body="synthetic deployment 배포계획", labels=None, html=None, attachment=None):
    msg = EmailMessage(policy=policy.default)
    msg["From"] = "Invented Sender <sender@example.org>"
    msg["To"] = "Invented Recipient <recipient@example.net>"
    msg["Subject"] = "Synthetic deployment"
    msg["Date"] = "Tue, 01 Oct 2024 00:00:00 +0000"
    msg["Message-ID"] = "<synthetic@example.com>"
    msg.set_content(body)
    if html is not None:
        msg.add_alternative(html, subtype="html")
    if attachment is not None:
        msg.add_attachment(attachment, maintype="application", subtype="octet-stream", filename="synthetic.bin")
    data = msg.as_bytes()
    return {"id": ident, "threadId": THREAD, "internalDate": "1727740800000", "sizeEstimate": len(data),
            "labelIds": ["INBOX"] if labels is None else labels,
            "raw": base64.urlsafe_b64encode(data).decode().rstrip("=")}


class Response:
    def __init__(self, value, headers=None):
        self.data, self.headers = json.dumps(value).encode(), headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def read(self):
        return self.data


def http_error(status, reason="synthetic", headers=None):
    return HTTPError("https://example.com", status, "synthetic", headers or {},
                     io.BytesIO(json.dumps({"error": {"errors": [{"reason": reason}]}}).encode()))


class Transport:
    def __init__(self):
        self.messages = {MID: raw_message()}
        self.labels = [{"id": "INBOX", "name": "Inbox", "type": "system"},
                       {"id": "SPAM", "name": "Spam", "type": "system"},
                       {"id": "TRASH", "name": "Trash", "type": "system"},
                       {"id": "UNREAD", "name": "Unread", "type": "system"},
                       {"id": "Label_1", "name": "Synthetic label", "type": "user"}]
        self.profile = {"historyId": "10", "emailAddress": "owner@example.com"}
        self.history = [{"historyId": "11", "history": []}]
        self.calls, self.failures = [], {}
        self.list_page_size, self.t, self.delays = 500, 0.0, []

    def sleep(self, seconds):
        self.delays.append(seconds)
        self.t += seconds

    def client(self):
        return GmailClient("synthetic-access-token", opener=self, sleep=self.sleep, clock=lambda: self.t)

    def __call__(self, req, timeout=None):
        url = urlsplit(req.full_url)
        path = url.path.removeprefix("/gmail/v1/users/me")
        q = parse_qs(url.query)
        self.calls.append((path, q))
        failures = self.failures.get(path)
        if failures:
            status = failures.pop(0)
            if status:
                raise http_error(status)
        if path == "/profile":
            return Response(self.profile)
        if path == "/labels":
            return Response({"labels": self.labels})
        if path == "/history":
            index = int(q.get("pageToken", ["0"])[0])
            return Response(copy.deepcopy(self.history[index]))
        if path == "/messages":
            rows = [v for _, v in sorted(self.messages.items()) if not q.get("labelIds") or
                    set(q["labelIds"]) <= set(v["labelIds"])]
            index = int(q.get("pageToken", ["0"])[0])
            end = index + self.list_page_size
            out = {"messages": [{"id": v["id"], "threadId": v["threadId"]} for v in rows[index:end]]}
            if end < len(rows):
                out["nextPageToken"] = str(end)
            return Response(out)
        if path.startswith("/messages/"):
            ident = path.split("/")[-1]
            if ident not in self.messages:
                raise http_error(404)
            return Response(copy.deepcopy(self.messages[ident]))
        raise AssertionError("unexpected synthetic endpoint")


class Fixture:
    def __init__(self):
        self.base = Path(tempfile.mkdtemp(prefix="gm-test-"))
        self.root, self.data_root = self.base / "archive", self.base / "data"
        self.root.mkdir()
        self.data_root.mkdir()
        self.config_path = self.base / "config.json"
        self.config_path.write_text(json.dumps({"v": 1, "impl": "claude", "data_root": str(self.data_root),
                                              "sources": {"gm_archive": str(self.root)}}), encoding="utf-8")

    def save(self, rel, value):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(value), encoding="utf-8")
