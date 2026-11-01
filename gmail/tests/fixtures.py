import base64
import datetime as dt
from email import policy
from email.message import EmailMessage
import imaplib
import json
from pathlib import Path
import re
import tempfile
from rawlayer.capture import paths
from gmail.imap import CATEGORIES, ImapClient

paths.DATA_PARENT = Path(tempfile.gettempdir())
MID, OTHER, THREAD = "abc123", "abc124", "def123"
USER, PASSWORD, LABEL = "owner@example.com", "synthetic-app-password", "Synthetic label"
# system label IDs -> the X-GM-LABELS names Gmail sends
NAMES = {"INBOX": "\\Inbox", "SENT": "\\Sent", "IMPORTANT": "\\Important", "STARRED": "\\Starred", "DRAFT": "\\Draft"}
FOLDERS = {'"[Gmail]/All Mail"': None, '"[Gmail]/Trash"': "TRASH", '"[Gmail]/Spam"': "SPAM"}
MONTHS = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()


def raw_message(ident=MID, body="synthetic deployment 배포계획", labels=None, html=None, attachment=None,
                subject="Synthetic deployment"):
    msg = EmailMessage(policy=policy.default)
    msg["From"] = "Invented Sender <sender@example.org>"
    msg["To"] = "Invented Recipient <recipient@example.net>"
    msg["Subject"] = subject
    msg["Date"] = "Tue, 01 Oct 2024 00:00:00 +0000"
    msg["Message-ID"] = "<synthetic@example.com>"
    msg.set_content(body)
    if html is not None:
        msg.add_alternative(html, subtype="html")
    if attachment is not None:
        msg.add_attachment(attachment, maintype="application", subtype="octet-stream", filename="synthetic.bin")
    data = msg.as_bytes()
    return {"id": ident, "threadId": THREAD, "internalDate": 1727740800000, "sizeEstimate": len(data),
            "labelIds": ["INBOX"] if labels is None else labels, "raw": data}


def utf7(name):
    """Modified UTF-7, as Gmail encodes label names."""
    return re.sub(r"[^ -~]+|&", lambda m: "&-" if m[0] == "&" else "&" + base64.b64encode(
        m[0].encode("utf-16-be")).decode().rstrip("=").replace("/", ",") + "-", name)


class Server:
    """A fake imaplib connection over raw_message objects. Their labelIds hold system label IDs, CATEGORY_* (which
    X-GM-RAW answers), TRASH or SPAM (the folder), UNREAD (no \\Seen) and user label names. `fail` holds "scan" (the
    folder scan answers NO) and message IDs (their fetch answers NO)."""

    def __init__(self):
        self.messages = {MID: raw_message()}
        self.labels = [LABEL]
        self.calls, self.fail, self.selected = [], set(), None

    def client(self, user=USER, password=PASSWORD):
        return ImapClient(user, password, connect=lambda: self)

    def login(self, user, password):
        self.calls.append(("login",))
        if password != PASSWORD:
            raise imaplib.IMAP4.error("synthetic")
        return "OK", [b""]

    def noop(self):
        self.calls.append(("noop",))
        return "OK", [b""]

    def logout(self):
        self.calls.append(("logout",))
        return "BYE", [b""]

    def list(self):
        self.calls.append(("list",))
        rows = [b'(\\HasNoChildren) "/" "INBOX"', b'(\\Noselect \\HasChildren) "/" "[Gmail]"',
                b'(\\HasNoChildren \\All) "/" "[Gmail]/All Mail"', b'(\\HasNoChildren \\Trash) "/" "[Gmail]/Trash"',
                b'(\\HasNoChildren \\Junk) "/" "[Gmail]/Spam"']
        return "OK", rows + [f'(\\HasNoChildren) "/" "{utf7(n)}"'.encode() for n in self.labels]

    def select(self, mailbox, readonly=False):
        self.calls.append(("select", mailbox, readonly))
        self.selected = FOLDERS[mailbox]
        return "OK", [str(len(self.folder())).encode()]

    def folder(self):
        return {str(int(m["id"], 16) & 0xFFFFFFFF): m for _, m in sorted(self.messages.items())  # UIDs are 32-bit
                if next((f for f in ("TRASH", "SPAM") if f in m["labelIds"]), None) == self.selected}

    def attrs(self, uid, m):
        labels = [NAMES.get(v) or f'"{utf7(v)}"' for v in m["labelIds"]
                  if v in NAMES or v not in ("UNREAD", "TRASH", "SPAM") and not v.startswith("CATEGORY_")]
        flags = "" if "UNREAD" in m["labelIds"] else "\\Seen"
        return f"{uid} (UID {uid} X-GM-MSGID {int(m['id'], 16)} X-GM-LABELS ({' '.join(labels)}) FLAGS ({flags})"

    def uid(self, command, *args):
        self.calls.append(("uid", command, *args))
        folder = self.folder()
        if command == "SEARCH":
            label = next(k for k, v in CATEGORIES.items() if args[1] == f'"category:{v}"')
            return "OK", [" ".join(u for u, m in folder.items() if label in m["labelIds"]).encode()]
        if args[0] == "1:*":
            return ("NO", [b"synthetic"]) if "scan" in self.fail else \
                ("OK", [(self.attrs(u, m) + ")").encode() for u, m in folder.items()])
        m = folder.get(args[0])
        if m is None:
            return "OK", [None]
        if m["id"] in self.fail:
            return "NO", [b"synthetic"]
        d = dt.datetime.fromtimestamp(m["internalDate"] // 1000, dt.timezone.utc)
        head = (self.attrs(args[0], m) + f" X-GM-THRID {int(m['threadId'], 16)} INTERNALDATE "
                f'"{d.day:02d}-{MONTHS[d.month - 1]}-{d.year} {d:%H:%M:%S} +0000" '
                f"RFC822.SIZE {len(m['raw'])} BODY[] {{{len(m['raw'])}}}")
        return "OK", [(head.encode(), m["raw"]), b")"]


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
