"""Extract searchable content and attachment metadata; never persist raw MIME."""
import base64
import hashlib
import re
from email import policy
from email.parser import BytesParser
from html.parser import HTMLParser
from urllib.parse import urlsplit, urlunsplit
from tools.archiveio import CollectorError
from .keys import gm_id, MAX_MS


class HTMLText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.hidden = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.hidden += 1
        if tag in ("p", "div", "br", "li", "tr", "h1", "h2", "blockquote"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.hidden = max(0, self.hidden - 1)
        if tag in ("p", "div", "li", "tr", "blockquote"):
            self.parts.append("\n")

    def handle_data(self, value):
        if not self.hidden:
            self.parts.append(value)


def decode(payload, declared):
    for charset in dict.fromkeys([declared, "utf-8", "cp949", "euc-kr", "latin-1"]):
        if not charset:
            continue
        try:
            return payload.decode(charset)
        except (UnicodeError, LookupError):
            continue
    return payload.decode("utf-8", errors="replace")


def clean_text(value):
    def strip(match):
        raw = match[0]
        tail = raw[len(raw.rstrip('.,;!)')):]
        try:
            u = urlsplit(raw.rstrip('.,;!)'))
            return urlunsplit((u.scheme, u.netloc, u.path, "", "")) + tail
        except ValueError:
            return ""
    value = re.sub(r'https?://[^\s<>"\']+', strip, value, flags=re.IGNORECASE)
    return "\n".join(" ".join(line.split()) for line in value.splitlines() if line.strip())


def extract(obj, body_max_bytes=131072):
    if gm_id(obj.get("id")) is None or gm_id(obj.get("threadId")) is None:
        raise CollectorError("bad_response")
    try:
        raw = obj["raw"]
        data = base64.b64decode(raw + "=" * (-len(raw) % 4), altchars=b"-_", validate=True)
        message = BytesParser(policy=policy.default).parsebytes(data)
        timestamp = int(obj["internalDate"])
        if not 0 <= timestamp <= MAX_MS or type(obj.get("sizeEstimate")) is not int:
            raise ValueError("timestamp")
    except (KeyError, ValueError, TypeError):
        raise CollectorError("bad_response") from None
    plain, html, attachments = [], [], []

    def visit(part, path):
        filename = part.get_filename()
        if filename or part.get_content_disposition() == "attachment" or \
                not part.is_multipart() and part.get_content_type() not in ("text/plain", "text/html"):
            payload = part.get_payload(decode=True) or b""
            if part.is_multipart():
                payload = b"".join(child.as_bytes(policy=policy.default) for child in part.iter_parts())
            attachments.append({"part_path": path, "filename": clean_text(filename or ""),
                                "mime_type": part.get_content_type(), "size": len(payload),
                                "sha256": hashlib.sha256(payload).hexdigest()})
            return
        if part.is_multipart():
            for i, child in enumerate(part.iter_parts(), 1):
                visit(child, path + "." + str(i) if path else str(i))
        elif part.get_content_type() in ("text/plain", "text/html"):
            text = decode(part.get_payload(decode=True) or b"", part.get_content_charset())
            (plain if part.get_content_type() == "text/plain" else html).append(text)

    visit(message, "" if message.is_multipart() else "1")
    if plain:
        body = "\n".join(plain)
    else:
        parser = HTMLText()
        parser.feed("\n".join(html))
        body = "".join(parser.parts)
    body = clean_text(body).encode("utf-8")
    out = {"id": obj["id"], "thread_id": obj["threadId"], "internal_date_ms": timestamp,
           "body_text": body[:body_max_bytes].decode("utf-8", errors="ignore"),
           "body_truncated": len(body) > body_max_bytes, "attachments": attachments,
           "size_estimate": obj["sizeEstimate"]}
    for header, field in (("Message-ID", "message_id"), ("From", "from"), ("To", "to"),
                          ("Cc", "cc"), ("Bcc", "bcc"), ("Subject", "subject"), ("Date", "date")):
        if header in message:
            out[field] = clean_text(str(message[header]))
    return out
