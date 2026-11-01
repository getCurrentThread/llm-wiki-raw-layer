"""Gmail over IMAP with an app password: one read-only scan per run (EXAMINE, UID FETCH with BODY.PEEK, UID
SEARCH; never STORE, COPY, APPEND or EXPUNGE). X-GM-MSGID and X-GM-THRID are the decimal form of the hex message
and thread IDs Gmail's web interface shows (Gmail's IMAP extensions); labels are given the API's system IDs."""
import base64
from email.utils import parsedate_to_datetime
import hashlib
import imaplib
import re
from appkit.archive import FetchError

HOST = "imap.gmail.com"
# X-GM-LABELS and FLAGS names -> system label IDs; any other backslash name is no label
SYSTEM = {"\\Inbox": "INBOX", "\\Sent": "SENT", "\\Important": "IMPORTANT", "\\Starred": "STARRED",
          "\\Flagged": "STARRED", "\\Draft": "DRAFT", "\\Drafts": "DRAFT", "\\Trash": "TRASH", "\\Spam": "SPAM"}
# Gmail's inbox categories are no IMAP labels: X-GM-RAW runs the web search syntax over All Mail
CATEGORIES = {"CATEGORY_PERSONAL": "primary", "CATEGORY_SOCIAL": "social", "CATEGORY_PROMOTIONS": "promotions",
              "CATEGORY_UPDATES": "updates", "CATEGORY_FORUMS": "forums"}
DIRECTORY = ("INBOX", "SENT", "IMPORTANT", "STARRED", "DRAFT", "TRASH", "SPAM", "UNREAD", *CATEGORIES)
# LIST attributes of folders that are no user label (RFC 6154 special use, Gmail's \Important, unselectable parents)
SPECIAL = {"\\All", "\\Archive", "\\Drafts", "\\Flagged", "\\Important", "\\Junk", "\\Sent", "\\Trash", "\\Noselect"}
# the folders scanned, with the label their messages carry: Spam and Trash are not in All Mail
FOLDERS = (("\\All", None), ("\\Trash", "TRASH"), ("\\Junk", "SPAM"))
SCAN = "(UID X-GM-MSGID X-GM-LABELS FLAGS)"
RAW = "(UID X-GM-MSGID X-GM-THRID X-GM-LABELS FLAGS INTERNALDATE RFC822.SIZE BODY.PEEK[])"
_TOKEN = re.compile(rb'\s*(?:([()])|"((?:[^"\\]|\\.)*)"|([^\s()"]+))')


def utf7(value):
    """IMAP's modified UTF-7 (RFC 3501 5.1.3), in which Gmail sends label and folder names."""
    def part(m):
        if not m[1]:
            return "&"
        b = m[1].replace(",", "/")
        return base64.b64decode(b + "=" * (-len(b) % 4), validate=True).decode("utf-16-be")
    try:
        return re.sub(r"&([A-Za-z0-9+,]*)-", part, value)
    except ValueError:
        raise FetchError("bad_response") from None


def label_id(name):
    """A user label's ID: names may hold any character, IDs stay keys (no colon, no control character)."""
    return "Label_" + hashlib.sha256(name.encode("utf-8")).hexdigest()[:16]


def parse(data):
    """imaplib response data -> its top-level parenthesized lists, of str, nested lists and bytes (literals)."""
    done, stack = [], []
    for part in data:
        head, literal = part if isinstance(part, tuple) else (part, None)
        if not isinstance(head, bytes):
            continue
        if literal is not None:
            head = head[:head.rindex(b"{")]
        for m in _TOKEN.finditer(head):
            if m[1] == b"(":
                stack.append([])
            elif m[1] == b")":
                if not stack:
                    raise FetchError("bad_response")
                value = stack.pop()
                (stack[-1] if stack else done).append(value)
            elif stack:  # outside parentheses: a FETCH response's sequence number
                stack[-1].append(m[3].decode("utf-8", "replace") if m[3] is not None
                                 else re.sub(r"\\(.)", r"\1", m[2].decode("utf-8", "replace")))
        if literal is not None:
            if not stack:
                raise FetchError("bad_response")
            stack[-1].append(literal)
    if stack:
        raise FetchError("bad_response")
    return done


def responses(data):
    """imaplib FETCH data -> one {ATTRIBUTE: value} per message."""
    return [{str(k).upper(): v for k, v in zip(r[::2], r[1::2])} for r in parse(data)]


def _number(value, bits=64):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,20}", value) or int(value) >> bits:
        raise FetchError("bad_response")
    return value


def _hex(value):
    return f"{int(_number(value)):x}"


def _quote(name):
    return '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'


class ImapClient:
    def __init__(self, user, password, connect=None):
        self.user, self.password, self.requests = user, password, 0
        self.connect = connect or (lambda: imaplib.IMAP4_SSL(HOST, 993, timeout=60))
        self.imap = self.selected = self.listing = None
        self.exists, self.where, self.names = 0, {}, set()

    def close(self):
        if self.imap is not None:
            try:
                self.imap.logout()
            except (imaplib.IMAP4.error, OSError):
                pass

    def _run(self, name, *args):
        try:
            if self.imap is None:
                conn = self.connect()
                try:
                    conn.login(self.user, self.password)
                except imaplib.IMAP4.error as e:
                    raise FetchError("transport_error" if isinstance(e, imaplib.IMAP4.abort) else "auth_required") \
                        from None
                self.imap = conn
            self.requests += 1
            typ, data = getattr(self.imap, name)(*args)
        except imaplib.IMAP4.abort:
            raise FetchError("transport_error") from None
        except imaplib.IMAP4.error:
            raise FetchError("api_error") from None
        except OSError:
            raise FetchError("transport_error") from None
        if typ != "OK":
            raise FetchError("api_error")
        return data

    def _examine(self, folder):
        if self.selected != folder:
            data = self._run("select", _quote(folder), True)
            self.selected, self.exists = folder, int(_number((data[0] or b"0").decode("ascii", "replace")))

    def _label_ids(self, r):
        labels, flags = r.get("X-GM-LABELS", []), r.get("FLAGS", [])
        if not isinstance(labels, list) or not isinstance(flags, list):
            raise FetchError("bad_response")
        labels = [v.decode("utf-8", "replace") if isinstance(v, bytes) else v for v in labels]
        if not all(isinstance(v, str) for v in labels + flags):
            raise FetchError("bad_response")
        names = {utf7(v) for v in labels if not v.startswith("\\")}
        self.names |= names
        out = {SYSTEM[v] for v in labels + flags if v in SYSTEM} | {label_id(n) for n in names}
        if "\\Seen" not in flags:
            out.add("UNREAD")
        return out

    def _folders(self):
        special = {}
        for line in self._run("list"):
            if not isinstance(line, bytes):
                # shortcut: a folder name sent as a literal is skipped (Gmail quotes them); upgrade if one appears
                continue
            row = parse([b"(" + line + b")"])
            if len(row) != 1 or len(row[0]) != 3 or not isinstance(row[0][0], list) or not isinstance(row[0][2], str):
                raise FetchError("bad_response")
            attrs, name = set(map(str, row[0][0])), row[0][2]
            special.update((use, name) for use, _extra in FOLDERS if use in attrs)
            if not attrs & SPECIAL and name.upper() != "INBOX" and not re.match(r"\[(Gmail|Google ?Mail)\](/|$)", name):
                self.names.add(utf7(name))
        if "\\All" not in special:
            raise FetchError("bad_response")
        return special

    def _scan(self):
        if self.listing is not None:
            return
        listing, special = {}, self._folders()
        for use, extra in FOLDERS:
            if use not in special:
                continue
            self._examine(special[use])
            if not self.exists:
                continue
            uids = {}
            for r in responses(self._run("uid", "FETCH", "1:*", SCAN)):
                if "X-GM-MSGID" not in r:
                    continue  # an unsolicited FLAGS update
                ident, uid = _hex(r["X-GM-MSGID"]), _number(r.get("UID"), 32)
                listing.setdefault(ident, set()).update(self._label_ids(r) | ({extra} if extra else set()))
                self.where.setdefault(ident, (special[use], uid))
                uids[uid] = ident
            for label, term in CATEGORIES.items() if extra is None else ():
                for uid in (self._run("uid", "SEARCH", "X-GM-RAW", f'"category:{term}"')[0] or b"").split():
                    ident = uids.get(uid.decode("ascii", "replace"))
                    if ident:
                        listing[ident].add(label)
        self.listing = listing

    def address(self):
        """Signs in. The login's address is the account's: the archive binds it."""
        self._run("noop")
        return self.user

    def labels(self):
        """The label directory, as {id, name, type} rows: the system labels, then every user label seen."""
        self._scan()
        return [{"id": v, "name": v, "type": "system"} for v in DIRECTORY] + \
            [{"id": label_id(n), "name": n, "type": "user"} for n in sorted(self.names)]

    def members(self):
        """Every message's label IDs, by hex message ID: All Mail, Trash and Spam."""
        self._scan()
        return self.listing

    def message(self, ident):
        """One message as extract() takes it, its raw RFC 822 bytes included."""
        self._scan()
        if ident not in self.where:
            raise FetchError("api_error", 404)
        folder, uid = self.where[ident]
        self._examine(folder)
        for r in responses(self._run("uid", "FETCH", uid, RAW)):
            if "X-GM-MSGID" not in r or _hex(r["X-GM-MSGID"]) != ident:
                continue
            try:
                body = r["BODY[]"]
                stamp = parsedate_to_datetime(r["INTERNALDATE"].replace("-", " ", 2))
                size = int(_number(r["RFC822.SIZE"]))
                if not isinstance(body, bytes) or stamp.tzinfo is None:
                    raise ValueError("fetch")
            except (KeyError, TypeError, ValueError, AttributeError):
                raise FetchError("bad_response") from None
            # categories and the folder labels come from the scan: no per-message IMAP query gives them
            values = self._label_ids(r) | (self.listing[ident] & {*CATEGORIES, "TRASH", "SPAM"})
            return {"id": ident, "threadId": _hex(r.get("X-GM-THRID")), "labelIds": sorted(values),
                    "internalDate": int(stamp.timestamp()) * 1000, "sizeEstimate": size, "raw": body}
        raise FetchError("api_error", 404)
