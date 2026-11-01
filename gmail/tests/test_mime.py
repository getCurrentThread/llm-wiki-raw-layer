import base64
from email import policy
from email.message import EmailMessage
import hashlib
import json
import unittest
from gmail.mime import extract, clean_text
from appkit.archive import FetchError
from .fixtures import raw_message


class MimeTests(unittest.TestCase):
    def wrap(self, data):
        obj = raw_message()
        obj["raw"] = base64.urlsafe_b64encode(data).decode().rstrip("=")
        return obj

    def test_plain_headers_and_optional_bcc(self):
        value = extract(raw_message())
        self.assertIn("배포계획", value["body_text"])
        self.assertIn("sender@example.org", value["from"])
        self.assertNotIn("bcc", value)
        value = extract(self.wrap(b"Bcc: invented@example.com\r\n\r\nsynthetic"))
        self.assertEqual(value["bcc"], "invented@example.com")

    def test_html_only_drops_script_style(self):
        msg = EmailMessage()
        msg.set_content("<style>secret</style><script>secret</script><p>one</p><div>two &amp; three</div>", subtype="html")
        self.assertEqual(extract(self.wrap(msg.as_bytes()))["body_text"], "one\ntwo & three")

    def test_alternative_prefers_plain(self):
        value = extract(raw_message(body="plain synthetic", html="<p>html synthetic</p>"))
        self.assertEqual(value["body_text"], "plain synthetic")

    def test_base64_quoted_printable_and_charsets(self):
        for charset in ("utf-8", "cp949", "euc-kr"):
            for cte in ("base64", "quoted-printable"):
                with self.subTest(charset=charset, cte=cte):
                    msg = EmailMessage(policy=policy.default)
                    msg.set_content("합성 배포", charset=charset, cte=cte)
                    self.assertEqual(extract(self.wrap(msg.as_bytes()))["body_text"], "합성 배포")

    def test_unknown_charset_fallback(self):
        raw = b"Content-Type: text/plain; charset=unknown\r\n\r\n" + "합성".encode("cp949")
        self.assertEqual(extract(self.wrap(raw))["body_text"], "합성")

    def test_url_strip_normalize_and_body_cap(self):
        value = extract(raw_message(body="  hello   https://example.org/reset?token=synthetic#fragment\n합성본문"), 35)
        self.assertNotIn("token", value["body_text"])
        self.assertNotIn("#", value["body_text"])
        self.assertTrue(value["body_truncated"])
        self.assertLessEqual(len(value["body_text"].encode()), 35)
        self.assertEqual(clean_text("https://example.net/x?q=y#z."), "https://example.net/x.")
        self.assertEqual(extract(raw_message(), 0)["body_text"], "")

    def test_attachment_metadata_hash_and_mime_path(self):
        payload = b"synthetic attachment"
        obj = extract(raw_message(html="<p>synthetic</p>", attachment=payload))
        attachment = obj["attachments"][0]
        self.assertEqual(attachment["part_path"], "2")
        self.assertEqual(attachment["size"], len(payload))
        self.assertEqual(attachment["sha256"], hashlib.sha256(payload).hexdigest())
        self.assertEqual(attachment["filename"], "synthetic.bin")
        self.assertNotIn("attachmentId", attachment)
        self.assertNotIn("synthetic attachment", obj["body_text"])

    def test_invalid_raw_and_ids(self):
        for field, value in (("raw", "%%%"), ("id", "BAD"), ("threadId", "word"),
                             ("internalDate", "-1"), ("internalDate", str(2 ** 63 - 1))):
            obj = raw_message()
            obj[field] = value
            with self.assertRaises(FetchError):
                extract(obj)

    def test_case_insensitive_urls_in_body_header_and_filename(self):
        msg = EmailMessage()
        msg["Subject"] = "HTTPS://example.org/reset?token=synthetic#frag"
        msg.set_content("HTTPS://example.org/reset?token=synthetic#frag "
                        "Http://example.net/path?q=synthetic#frag "
                        "hTtPs://example.com/mixed?q=synthetic#frag "
                        "https://example.org/lower?q=synthetic#frag")
        msg.add_attachment(b"synthetic", maintype="application", subtype="octet-stream",
                           filename="Http://example.net/file?q=synthetic#frag")
        value = extract(self.wrap(msg.as_bytes()))
        self.assertEqual(value["body_text"], "https://example.org/reset http://example.net/path "
                         "https://example.com/mixed https://example.org/lower")
        self.assertEqual(value["subject"], "https://example.org/reset")
        self.assertEqual(value["attachments"][0]["filename"], "http://example.net/file")

    def test_inline_related_image_without_filename_has_decoded_metadata(self):
        payload = b"synthetic image bytes"
        msg = EmailMessage()
        msg.set_content("synthetic body")
        msg.add_related(payload, maintype="image", subtype="png", cid="<synthetic@example.org>")
        value = extract(self.wrap(msg.as_bytes()))
        self.assertEqual(value["body_text"], "synthetic body")
        self.assertEqual(value["attachments"], [{"part_path": "2", "filename": "", "mime_type": "image/png",
                                                 "size": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}])

    def test_binary_leaf_without_disposition_is_metadata_only(self):
        payload = b"synthetic binary bytes"
        msg = EmailMessage()
        msg.set_content("synthetic body")
        msg.make_mixed()
        binary = EmailMessage()
        binary.set_content(payload, maintype="application", subtype="octet-stream", cte="base64")
        msg.attach(binary)
        value = extract(self.wrap(msg.as_bytes()))
        self.assertEqual(value["attachments"], [{"part_path": "2", "filename": "", "mime_type": "application/octet-stream",
                                                 "size": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}])
        self.assertNotIn("synthetic binary", json.dumps(value))

    def test_alternative_text_parts_are_not_attachments(self):
        msg = EmailMessage()
        msg.set_content("synthetic plain")
        msg.add_alternative("<p>synthetic html</p>", subtype="html")
        msg.get_payload()[1].add_related(b"synthetic", maintype="image", subtype="png",
                                        cid="<synthetic@example.org>")
        value = extract(self.wrap(msg.as_bytes()))
        self.assertEqual(value["body_text"], "synthetic plain")
        self.assertEqual(len(value["attachments"]), 1)
        self.assertEqual(value["attachments"][0]["part_path"], "2.2")
        self.assertEqual(value["attachments"][0]["mime_type"], "image/png")
