import contextlib
import io
import json
import unittest
from unittest.mock import patch
from gmail import fetch
from gmail.fetch import sync, main
from gmail.search import SPEC
from rawlayer.search import index
from appkit.archive import FetchError, load, locked
from .fixtures import Fixture, Transport, raw_message, MID, OTHER
from tests.core import appfix as h


class FetchTests(unittest.TestCase):
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

    def test_auth_flag_runs_the_consent_step_alone(self):
        argv, out = ["--auth", "--config", str(self.fx.config_path)], io.StringIO()
        with patch.object(fetch, "authorize", side_effect=[{"status": "auth_ready"}, FetchError("auth_failed")]),                 contextlib.redirect_stdout(out):
            self.assertEqual((fetch.main(argv), fetch.main(argv)), (0, 2))
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                fetch.main([*argv, "--full"])
        self.assertEqual([json.loads(line) for line in out.getvalue().splitlines()],
                         [{"app": "gmail", "status": "auth_ready"},
                          {"app": "gmail", "status": "auth_failed", "http_status": None}])

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
        with self.assertRaisesRegex(FetchError, "account_changed"):
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
            with self.assertRaisesRegex(FetchError, "collector_busy"):
                self.sync()
        self.http.messages["word"] = raw_message("word")
        self.assertGreater(self.sync()["pk_bad"], 0)

    def test_paginated_genesis_over_500(self):
        self.http.messages = {format(i, "x"): raw_message(format(i, "x")) for i in range(1, 503)}
        self.http.list_page_size = 500
        result = self.sync(max_items=1)
        self.assertTrue(result["limited"])
        self.assertEqual(len(load(self.fx.root / "_collector_state.json")["pending"]), 501)

    def test_endpoint_cannot_receive_credentials(self):
        client = self.http.client()
        with self.assertRaises(FetchError):
            client.request("https://example.com")
        self.assertFalse(self.http.calls)

    def test_publish_failure_keeps_old_cursor(self):
        self.sync()
        self.events(labelsAdded=[{"message": {"id": MID}, "labelIds": ["TRASH"]}])
        from gmail import fetch
        original, failed = fetch.atomic, False
        def atomic(root, path, value, **kwargs):
            nonlocal failed
            if path.name == "manifest.json" and not failed:
                failed = True
                raise FetchError("archive_busy")
            return original(root, path, value, **kwargs)
        with patch("gmail.fetch.atomic", side_effect=atomic):
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
        index.update(self.fx.data_root, SPEC)
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

    def test_unreconciled_content_only_keeps_cursor_and_cli_exit_75(self):
        self.partial()
        self.http.history = [{"historyId": "20", "history": []}]
        self.http.failures["/messages/" + OTHER] = [403]
        out = io.StringIO()
        with patch("gmail.fetch.sync_config", side_effect=lambda *a, **kw: self.sync()), \
                patch("rawlayer.capture.precheck.check"), contextlib.redirect_stdout(out):
            self.assertEqual(main(["--config", str(self.fx.config_path)]), 75)
        self.assertEqual(json.loads(out.getvalue())["errors"], 1)
        self.assertEqual(load(self.fx.root / "manifest.json")["cursor"], "10")
        self.assertNotIn(OTHER, load(self.fx.root / "state/ab.json"))

