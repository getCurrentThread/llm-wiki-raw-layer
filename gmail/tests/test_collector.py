import copy
import json
import unittest
from unittest.mock import patch
from gmail.collector import sync
from gmail.api import GmailClient, Bucket, pages
from tools.archiveio import CollectorError, load, locked
from .fixtures import Fixture, Transport, Response, http_error, raw_message, MID, OTHER
from . import helpers as h


class CollectorTests(unittest.TestCase):
    def setUp(self):
        self.fx, self.http = Fixture(), Transport()
        self.addCleanup(h.force_rmtree, self.fx.base)

    def sync(self, **kwargs):
        return sync(self.fx.root, self.http.client(), **kwargs)

    def manifest(self):
        return load(self.fx.root / "manifest.json")

    def state(self, ident=MID):
        return load(self.fx.root / f"state/{ident[:2]}.json")[ident]

    def events(self, **fields):
        self.http.history = [{"historyId": "12", "history": [{"id": "11", **fields}]}]

    def test_genesis_profile_before_listing_and_cursor(self):
        self.assertEqual(self.sync()["errors"], 0)
        self.assertEqual(self.http.calls[0][0], "/profile")
        self.assertEqual(self.manifest()["cursor"], "10")
        self.assertTrue(self.manifest()["genesis_complete"])
        self.assertNotIn("emailAddress", json.dumps(self.manifest()))

    def test_genesis_resumable_max_items(self):
        self.http.messages[OTHER] = raw_message(OTHER)
        first = self.sync(max_items=1)
        self.assertTrue(first["limited"])
        self.assertIsNone(self.manifest()["cursor"])
        self.assertEqual(len(load(self.fx.root / "_collector_state.json")["pending"]), 1)
        self.http.profile["historyId"] = "20"
        self.assertEqual(self.sync(max_items=1)["messages_written"], 1)
        self.assertEqual(self.manifest()["cursor"], "10")
        self.assertFalse(any(p == "/history" for p, _ in self.http.calls))

    def test_genesis_time_budget(self):
        self.assertTrue(self.sync(time_budget=0)["limited"])
        self.assertIsNone(self.manifest()["cursor"])
        self.assertEqual(self.sync()["messages_written"], 1)

    def test_history_paging_fetch_after_last_page(self):
        self.sync()
        self.http.messages[OTHER] = raw_message(OTHER)
        self.http.calls.clear()
        self.http.history = [{"historyId": "11", "history": [], "nextPageToken": "1"},
                             {"historyId": "12", "history": [{"messagesAdded": [{"message": {"id": OTHER, "labelIds": ["INBOX"]}}]}]}]
        self.assertEqual(self.sync()["messages_written"], 1)
        calls = [p for p, _ in self.http.calls]
        self.assertLess(max(i for i, p in enumerate(calls) if p == "/history"), calls.index("/messages/" + OTHER))
        self.assertEqual(self.manifest()["cursor"], "12")

    def test_labels_add_remove_msg_immutable(self):
        self.sync()
        self.assertEqual(h.capture(self.fx), 0)
        part = self.fx.root / self.manifest()["files"][0]
        before = part.read_bytes()
        for field in ("labelsAdded", "labelsRemoved"):
            self.events(**{field: [{"message": {"id": MID}, "labelIds": ["Label_1"]}]})
            self.assertEqual(self.sync()["errors"], 0)
            self.assertEqual(h.capture(self.fx), 0)
        by = h.by_key(h.records(self.fx))
        self.assertEqual(len(by["gm:msg:" + MID]), 1)
        self.assertEqual(len(by["gm:lbls:" + MID]), 3)
        self.assertEqual(before, part.read_bytes())

    def test_unread_no_revision(self):
        self.sync()
        self.assertEqual(h.capture(self.fx), 0)
        n = len(h.records(self.fx))
        self.events(labelsAdded=[{"message": {"id": MID}, "labelIds": ["UNREAD"]}])
        self.sync()
        self.assertEqual(h.capture(self.fx), 0)
        self.assertEqual(n, len(h.records(self.fx)))

    def test_delete_tombstone_once(self):
        self.sync()
        self.events(messagesDeleted=[{"message": {"id": MID}}])
        self.sync()
        before = self.state()
        self.assertIn("_removedAt", before)
        self.sync()
        self.assertEqual(before, self.state())
        self.assertNotIn("_removedAt", load(self.fx.root / self.manifest()["files"][0])["messages"][0])

    def test_404_full_diff_and_per_label_rebuild(self):
        self.sync()
        del self.http.messages[MID]
        self.http.messages[OTHER] = raw_message(OTHER, labels=["Label_1"])
        self.http.failures["/history"] = [404]
        self.http.profile["historyId"] = "30"
        self.http.calls.clear()
        result = self.sync()
        self.assertEqual(result["errors"], 0)
        self.assertIn("_removedAt", self.state())
        self.assertEqual(self.state(OTHER)["label_ids"], ["Label_1"])
        self.assertEqual(self.manifest()["cursor"], "30")
        self.assertTrue(any(q.get("labelIds") == ["Label_1"] for p, q in self.http.calls if p == "/messages"))

    def test_resync_resume_before_history(self):
        self.sync()
        self.http.messages.update({OTHER: raw_message(OTHER), "abc125": raw_message("abc125")})
        self.http.failures["/history"] = [404]
        self.assertTrue(self.sync(max_items=1)["limited"])
        self.assertEqual(self.manifest()["cursor"], "10")
        self.http.calls.clear()
        self.sync()
        self.assertFalse(any(p == "/history" for p, _ in self.http.calls))
        self.assertFalse(load(self.fx.root / "_collector_state.json")["active"])

    def test_partial_history_page_keeps_cursor_no_deletion(self):
        self.sync()
        self.http.history = [{"historyId": "12", "history": [{"messagesDeleted": [{"message": {"id": MID}}]}], "nextPageToken": "1"}]
        self.http.failures["/history"] = [None, 403]
        self.assertEqual(self.sync()["errors"], 1)
        self.assertEqual(self.manifest()["cursor"], "10")
        self.assertNotIn("_removedAt", self.state())

    def test_fetch_failure_keeps_cursor_and_deletion_state(self):
        self.sync()
        self.http.messages[OTHER] = raw_message(OTHER)
        self.events(messagesAdded=[{"message": {"id": OTHER, "labelIds": ["INBOX"]}}],
                    messagesDeleted=[{"message": {"id": MID}}])
        self.http.failures["/messages/" + OTHER] = [403]
        self.assertEqual(self.sync()["errors"], 1)
        self.assertEqual(self.manifest()["cursor"], "10")
        self.assertNotIn("_removedAt", self.state())
        self.assertEqual(self.sync()["errors"], 0)
        self.assertIn("_removedAt", self.state())

    def test_excluded_never_fetched_unspam_later_fetched(self):
        self.http.messages[MID]["labelIds"] = ["SPAM"]
        self.assertEqual(self.sync()["messages_written"], 0)
        self.assertFalse(any(p.startswith("/messages/") for p, _ in self.http.calls))
        self.http.messages[MID]["labelIds"] = ["INBOX"]
        self.events(labelsRemoved=[{"message": {"id": MID}, "labelIds": ["SPAM"]}],
                    labelsAdded=[{"message": {"id": MID}, "labelIds": ["INBOX"]}])
        self.assertEqual(self.sync()["messages_written"], 1)

    def test_later_excluded_recorded_without_fetch(self):
        self.sync()
        self.http.calls.clear()
        self.events(labelsAdded=[{"message": {"id": MID}, "labelIds": ["TRASH"]}])
        self.assertEqual(self.sync()["messages_written"], 0)
        self.assertIn("TRASH", self.state()["label_ids"])
        self.assertFalse(any(p.startswith("/messages/") for p, _ in self.http.calls))

    def test_labels_directory_nonshrinking(self):
        self.sync()
        self.http.labels = [v for v in self.http.labels if v["id"] != "Label_1"]
        self.sync()
        self.assertIn("_removedAt", load(self.fx.root / "labels.json")["Label_1"])

    def test_account_changed_and_casefold(self):
        self.sync()
        self.http.profile["emailAddress"] = "OWNER@example.com"
        self.assertEqual(self.sync()["errors"], 0)
        self.http.profile["emailAddress"] = "another@example.org"
        with self.assertRaisesRegex(CollectorError, "account_changed"):
            self.sync()

    def test_partition_caps_and_noop(self):
        self.http.messages[OTHER] = raw_message(OTHER)
        self.assertEqual(self.sync(partition_max_messages=1)["partitions_written"], 2)
        self.assertEqual(self.manifest()["files"], sorted(self.manifest()["files"]))
        self.assertEqual(self.sync()["messages_written"], 0)

    def test_failed_listing_does_not_infer_delete(self):
        self.sync()
        self.http.failures["/messages"] = [403]
        self.assertEqual(self.sync(full=True)["errors"], 1)
        self.assertNotIn("_removedAt", self.state())
        self.assertEqual(self.manifest()["cursor"], "10")

    def test_crash_partition_recovery_no_duplicates(self):
        self.sync()
        man = self.manifest()
        original = man["files"].pop()
        self.fx.save("manifest.json", man)
        self.assertEqual(self.sync()["messages_written"], 0)
        self.assertIn(original, self.manifest()["files"])

    def test_lock_and_invalid_ids(self):
        with locked(self.fx.root):
            with self.assertRaisesRegex(CollectorError, "collector_busy"):
                self.sync()
        self.http.messages["word"] = raw_message("word")
        self.assertGreater(self.sync()["pk_bad"], 0)

    def test_paginated_genesis_over_500(self):
        self.http.messages = {format(i, "x"): raw_message(format(i, "x")) for i in range(1, 503)}
        self.http.list_page_size = 500
        result = self.sync(max_items=1)
        self.assertTrue(result["limited"])
        self.assertEqual(len(load(self.fx.root / "_collector_state.json")["pending"]), 501)

    def test_rate_retry_after_and_bounded_exhaustion(self):
        failures = [http_error(429, headers={"Retry-After": "7"}),
                    http_error(403, "userRateLimitExceeded"), Response({"historyId": "1"})]
        delays = []
        def opener(req, **kwargs):
            value = failures.pop(0)
            if isinstance(value, Exception):
                raise value
            return value
        client = GmailClient("synthetic", opener=opener, sleep=delays.append)
        self.assertEqual(client.request("/profile")["historyId"], "1")
        self.assertIn(7, delays)
        self.assertIn(2, delays)
        def always(req, **kwargs):
            raise http_error(429)
        client = GmailClient("synthetic", opener=always, sleep=lambda n: None)
        with self.assertRaises(CollectorError):
            client.request("/profile")
        self.assertEqual(client.requests, 6)

    def test_bucket_250_gets_headroom(self):
        bucket = Bucket(self.http.sleep, lambda: self.http.t)
        for _ in range(251):
            bucket.take(20)
        self.assertGreaterEqual(self.http.t, 60)

    def test_pagination_cycle_rejected(self):
        client = GmailClient("synthetic", opener=lambda *a, **k: Response({"nextPageToken": "same"}), sleep=lambda n: None)
        with self.assertRaisesRegex(CollectorError, "bad_response"):
            list(pages(client, "/messages"))

    def test_endpoint_cannot_receive_credentials(self):
        client = self.http.client()
        with self.assertRaises(CollectorError):
            client.request("https://example.com")
        self.assertFalse(self.http.calls)

    def test_publication_order_cursor_last(self):
        from gmail import collector
        original, writes = collector.atomic, []
        def atomic(root, path, value, **kwargs):
            writes.append((str(path.relative_to(root)).replace("\\", "/"), copy.deepcopy(value)))
            return original(root, path, value, **kwargs)
        with patch("gmail.collector.atomic", side_effect=atomic):
            self.sync()
        kinds = [p.split("/")[0] for p, _ in writes]
        self.assertLess(kinds.index("exports"), kinds.index("state"))
        self.assertLess(kinds.index("state"), kinds.index("labels.json"))
        self.assertLess(kinds.index("labels.json"), kinds.index("manifest.json"))
        self.assertEqual(next(v for p, v in reversed(writes) if p == "manifest.json")["cursor"], "10")

    def test_full_recovers_pending_message_deleted_during_genesis(self):
        self.http.messages[OTHER] = raw_message(OTHER)
        self.sync(max_items=1)
        del self.http.messages[OTHER]
        self.assertEqual(self.sync()["errors"], 1)
        self.assertIsNone(self.manifest()["cursor"])
        self.http.profile["historyId"] = "20"
        self.assertEqual(self.sync(full=True)["errors"], 0)
        self.assertTrue(self.manifest()["genesis_complete"])
        self.assertEqual(self.manifest()["cursor"], "20")

    def test_partition_byte_cap_splits(self):
        from gmail.collector import Writer
        man, stats = {"files": []}, {"messages_written": 0, "partitions_written": 0}
        with patch("gmail.collector.PARTITION_BYTES", 300):
            writer = Writer(self.fx.root, man, stats, 1000)
            writer.add({"id": MID, "body_text": "a" * 80})
            writer.add({"id": OTHER, "body_text": "b" * 80})
            writer.flush()
        self.assertEqual(stats["partitions_written"], 2)
        self.assertTrue(all((self.fx.root / p).stat().st_size <= 300 for p in man["files"]))

    def test_publish_failure_keeps_old_cursor(self):
        self.sync()
        self.events(labelsAdded=[{"message": {"id": MID}, "labelIds": ["TRASH"]}])
        from gmail import collector
        original, failed = collector.atomic, False
        def atomic(root, path, value, **kwargs):
            nonlocal failed
            if path.name == "manifest.json" and not failed:
                failed = True
                raise CollectorError("archive_busy")
            return original(root, path, value, **kwargs)
        with patch("gmail.collector.atomic", side_effect=atomic):
            self.assertEqual(self.sync()["errors"], 1)
        self.assertEqual(self.manifest()["cursor"], "10")

    def test_orphan_shard_recovery_never_shrinks(self):
        self.sync()
        self.fx.save("state/ff.json", {"ff": {"label_ids": ["INBOX"]}})
        self.sync()
        self.assertIn("state/ff.json", self.manifest()["state_files"])
        self.assertIn("ff", load(self.fx.root / "state/ff.json"))

    def test_history_without_unknown_label_membership_fails_closed(self):
        self.sync()
        self.http.calls.clear()
        self.events(messagesAdded=[{"message": {"id": OTHER}}])
        self.assertEqual(self.sync()["errors"], 1)
        self.assertEqual(self.manifest()["cursor"], "10")
        self.assertFalse(any(p.startswith("/messages/") for p, _ in self.http.calls))

    def test_unknown_unspam_uses_fetched_current_labels(self):
        self.http.messages[MID]["labelIds"] = ["SPAM"]
        self.sync()
        self.http.messages[MID]["labelIds"] = ["INBOX", "Label_1"]
        self.events(labelsRemoved=[{"message": {"id": MID}, "labelIds": ["SPAM"]}])
        self.assertEqual(self.sync()["messages_written"], 1)
        self.assertEqual(self.state()["label_ids"], ["INBOX", "Label_1"])
