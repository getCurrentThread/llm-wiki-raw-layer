import base64
import contextlib
from email.message import EmailMessage
import hashlib
import io
import json
import unittest
from unittest.mock import patch
from gmail.collector import sync, main
from gmail.mime import extract
from gmail.search import SPEC
from rawlayer.search import common
from tools.archiveio import load
from .fixtures import Fixture, Transport, MID, OTHER, raw_message
from . import helpers as h


class ContentOnlyTests(unittest.TestCase):
    def setUp(self):
        self.fx, self.http = Fixture(), Transport()
        self.addCleanup(h.force_rmtree, self.fx.base)
        self.assertEqual(self.sync()["errors"], 0)

    def sync(self):
        return sync(self.fx.root, self.http.client())

    def partial(self):
        self.http.messages[OTHER] = raw_message(OTHER)
        self.http.messages["abc125"] = raw_message("abc125")
        self.http.history = [{"historyId": "12", "history": [{
            "messagesAdded": [{"message": {"id": ident, "labelIds": ["INBOX"]}}
                              for ident in (OTHER, "abc125")],
            "messagesDeleted": [{"message": {"id": MID}}]}]}]
        self.http.failures["/messages/abc125"] = [403]
        self.assertEqual(self.sync()["errors"], 1)
        self.assertEqual(load(self.fx.root / "manifest.json")["cursor"], "10")
        states = load(self.fx.root / "state/ab.json")
        self.assertNotIn(OTHER, states)
        self.assertNotIn("_removedAt", states[MID])
        man = load(self.fx.root / "manifest.json")
        self.assertIn(OTHER, {v["id"] for rel in man["files"]
                             for v in load(self.fx.root / rel)["messages"]})

    def test_partial_fetch_then_expiry_deletes_content_only_and_hides_search(self):
        self.partial()
        self.assertEqual(h.capture(self.fx), 0)
        del self.http.messages[OTHER]
        self.http.failures["/history"] = [404]
        self.http.profile["historyId"] = "30"
        self.assertEqual(self.sync()["errors"], 0)
        state = load(self.fx.root / "state/ab.json")[OTHER]
        self.assertEqual(state["label_ids"], [])
        self.assertIn("_removedAt", state)
        self.assertEqual(load(self.fx.root / "manifest.json")["cursor"], "30")
        self.assertEqual(h.capture(self.fx), 0)
        common.update(self.fx.data_root, SPEC)
        for extra, expected in (([], 2), (["--include-removed"], 3)):
            rc, out, err = h.run_rawsearch(["gmail", "--config", self.fx.config_path,
                                          "deployment", "--json", *extra])
            self.assertEqual(rc, 0, err)
            self.assertEqual(json.loads(out)["total"], expected)

    def test_successful_incremental_reconciles_untouched_content_only(self):
        self.partial()
        self.http.history = [{"historyId": "20", "history": []}]
        self.http.messages[OTHER]["labelIds"] = ["Label_1", "UNREAD"]
        self.http.calls.clear()
        self.assertEqual(self.sync()["errors"], 0)
        self.assertEqual(load(self.fx.root / "state/ab.json")[OTHER], {"label_ids": ["Label_1"]})
        self.assertEqual(load(self.fx.root / "manifest.json")["cursor"], "20")
        calls = [q for p, q in self.http.calls if p == "/messages/" + OTHER]
        self.assertEqual(calls, [{"format": ["minimal"], "fields": ["id,labelIds"]}])

    def test_successful_resync_reconciles_present_content_only_without_get(self):
        self.partial()
        self.http.failures["/history"] = [404]
        self.http.messages[OTHER]["labelIds"] = ["Label_1"]
        self.http.profile["historyId"] = "30"
        self.http.calls.clear()
        self.assertEqual(self.sync()["errors"], 0)
        self.assertEqual(load(self.fx.root / "state/ab.json")[OTHER], {"label_ids": ["Label_1"]})
        self.assertFalse(any(p == "/messages/" + OTHER for p, q in self.http.calls))
        self.assertEqual(load(self.fx.root / "manifest.json")["cursor"], "30")
        # Repeat with a surviving content-only ID outside this history window.
        states = load(self.fx.root / "state/ab.json")
        del states[OTHER]
        self.fx.save("state/ab.json", states)
        self.http.history = [{"historyId": "31", "history": []}]
        self.assertEqual(self.sync()["errors"], 0)
        self.assertEqual(load(self.fx.root / "state/ab.json")[OTHER], {"label_ids": ["Label_1"]})

    def test_unreconciled_content_only_keeps_cursor_and_cli_exit_75(self):
        self.partial()
        self.http.history = [{"historyId": "20", "history": []}]
        self.http.failures["/messages/" + OTHER] = [403]
        out = io.StringIO()
        with patch("gmail.collector.sync_config", side_effect=lambda *a, **kw: self.sync()), \
                patch("rawlayer.capture.precheck.check"), contextlib.redirect_stdout(out):
            self.assertEqual(main(["--config", str(self.fx.config_path)]), 75)
        self.assertEqual(json.loads(out.getvalue())["errors"], 1)
        self.assertEqual(load(self.fx.root / "manifest.json")["cursor"], "10")
        self.assertNotIn(OTHER, load(self.fx.root / "state/ab.json"))

    def test_cursor_guard_rejects_unreconciled_state_after_minimal_fetch(self):
        self.partial()
        self.http.history = [{"historyId": "20", "history": []}]
        # Simulate a state dropped during reconciliation; the final guard must
        # prevent cursor publication even though the minimal request succeeded.
        import gmail.collector as collector
        archive = collector._archive

        class DroppingState(dict):
            def __setitem__(self, key, value):
                if key != OTHER:
                    super().__setitem__(key, value)

            def __deepcopy__(self, memo):
                return DroppingState(self)

        def missing(*args):
            known, states = archive(*args)
            return known, DroppingState(states)

        with patch("gmail.collector._archive", side_effect=missing):
            self.assertEqual(self.sync()["errors"], 1)
        self.assertEqual(load(self.fx.root / "manifest.json")["cursor"], "10")


class MimeReviewTests(unittest.TestCase):
    def extract_message(self, msg):
        obj = raw_message()
        obj["raw"] = base64.urlsafe_b64encode(msg.as_bytes()).decode().rstrip("=")
        return extract(obj)

    def test_case_insensitive_urls_in_body_header_and_filename(self):
        msg = EmailMessage()
        msg["Subject"] = "HTTPS://example.org/reset?token=synthetic#frag"
        msg.set_content("HTTPS://example.org/reset?token=synthetic#frag "
                        "Http://example.net/path?q=synthetic#frag "
                        "hTtPs://example.com/mixed?q=synthetic#frag "
                        "https://example.org/lower?q=synthetic#frag")
        msg.add_attachment(b"synthetic", maintype="application", subtype="octet-stream",
                           filename="Http://example.net/file?q=synthetic#frag")
        value = self.extract_message(msg)
        self.assertEqual(value["body_text"], "https://example.org/reset http://example.net/path "
                         "https://example.com/mixed https://example.org/lower")
        self.assertEqual(value["subject"], "https://example.org/reset")
        self.assertEqual(value["attachments"][0]["filename"], "http://example.net/file")

    def test_inline_related_image_without_filename_has_decoded_metadata(self):
        payload = b"synthetic image bytes"
        msg = EmailMessage()
        msg.set_content("synthetic body")
        msg.add_related(payload, maintype="image", subtype="png", cid="<synthetic@example.org>")
        value = self.extract_message(msg)
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
        value = self.extract_message(msg)
        self.assertEqual(value["attachments"], [{"part_path": "2", "filename": "", "mime_type": "application/octet-stream",
                                                 "size": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}])
        self.assertNotIn("synthetic binary", json.dumps(value))

    def test_alternative_text_parts_are_not_attachments(self):
        msg = EmailMessage()
        msg.set_content("synthetic plain")
        msg.add_alternative("<p>synthetic html</p>", subtype="html")
        msg.get_payload()[1].add_related(b"synthetic", maintype="image", subtype="png",
                                        cid="<synthetic@example.org>")
        value = self.extract_message(msg)
        self.assertEqual(value["body_text"], "synthetic plain")
        self.assertEqual(len(value["attachments"]), 1)
        self.assertEqual(value["attachments"][0]["part_path"], "2.2")
        self.assertEqual(value["attachments"][0]["mime_type"], "image/png")
