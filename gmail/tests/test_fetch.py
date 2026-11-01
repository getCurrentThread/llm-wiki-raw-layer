import json
import unittest
from gmail.fetch import sync
from gmail.imap import label_id, responses, utf7
from appkit.archive import FetchError, load, locked
from .fixtures import Fixture, Server, raw_message, LABEL, MID, OTHER, USER
from tests.core import appfix as h


class FetchTests(unittest.TestCase):
    def setUp(self):
        self.fx, self.imap = Fixture(), Server()
        self.addCleanup(h.force_rmtree, self.fx.base)

    def sync(self, user=USER, **kwargs):
        return sync(self.fx.root, self.imap.client(user), **kwargs)

    def manifest(self):
        return load(self.fx.root / "manifest.json")

    def state(self, ident=MID):
        return load(self.fx.root / f"state/{ident[:2]}.json")[ident]

    def test_genesis_signs_in_first_and_only_reads(self):
        result = self.sync()
        self.assertEqual((result["errors"], result["messages_written"]), (0, 1))
        self.assertEqual(self.imap.calls[:2], [("login",), ("noop",)])
        self.assertNotIn(USER, json.dumps(self.manifest()))
        commands = {c[1] if c[0] == "uid" else c[0] for c in self.imap.calls}
        self.assertLessEqual(commands, {"login", "noop", "list", "select", "FETCH", "SEARCH"})
        self.assertTrue(all(c[2] is True for c in self.imap.calls if c[0] == "select"))
        fetches = [c[3] for c in self.imap.calls if c[:2] == ("uid", "FETCH")]
        self.assertIn("BODY.PEEK[]", fetches[-1])
        self.assertFalse(any("BODY[" in f for f in fetches))

    def test_labels_flags_categories_and_korean_names(self):
        self.imap.labels = [LABEL, "합성 라벨"]
        self.imap.messages[MID]["labelIds"] = ["INBOX", "IMPORTANT", "합성 라벨", "CATEGORY_PERSONAL", "UNREAD"]
        self.sync()
        self.assertEqual(self.state()["label_ids"],
                         sorted(["CATEGORY_PERSONAL", "IMPORTANT", "INBOX", label_id("합성 라벨")]))
        labels = load(self.fx.root / "labels.json")
        self.assertEqual(labels[label_id("합성 라벨")]["name"], "합성 라벨")
        self.assertEqual(labels[label_id(LABEL)], {"id": label_id(LABEL), "name": LABEL, "type": "user"})

    def test_genesis_resumable_max_items(self):
        self.imap.messages[OTHER] = raw_message(OTHER)
        first = self.sync(max_items=1)
        self.assertEqual((first["limited"], first["messages_written"]), (True, 1))
        second = self.sync(max_items=1)
        self.assertEqual((second["limited"], second["messages_written"]), (False, 1))

    def test_labels_add_remove_msg_immutable(self):
        self.sync()
        self.assertEqual(h.capture(self.fx), 0)
        part = self.fx.root / self.manifest()["files"][0]
        before = part.read_bytes()
        for labels in (["INBOX", LABEL], ["INBOX"]):
            self.imap.messages[MID]["labelIds"] = labels
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
        self.imap.messages[MID]["labelIds"] = ["INBOX", "UNREAD"]
        self.sync()
        self.assertEqual(h.capture(self.fx), 0)
        self.assertEqual(n, len(h.records(self.fx)))

    def test_delete_tombstone_once_and_return(self):
        self.sync()
        saved = self.imap.messages.pop(MID)
        self.sync()
        before = self.state()
        self.assertIn("_removedAt", before)
        self.sync()
        self.assertEqual(before, self.state())
        self.assertNotIn("_removedAt", load(self.fx.root / self.manifest()["files"][0])["messages"][0])
        self.imap.messages[MID] = saved
        self.sync()
        self.assertNotIn("_removedAt", self.state())

    def test_excluded_never_fetched_unspam_later_fetched(self):
        self.imap.messages[MID]["labelIds"] = ["SPAM"]
        self.imap.messages[OTHER] = raw_message(OTHER, labels=["INBOX", "CATEGORY_PROMOTIONS"])
        result = self.sync()
        self.assertEqual((result["messages_written"], result["excluded"]), (0, 2))
        self.assertEqual(self.state()["label_ids"], ["SPAM"])
        self.assertFalse(any(len(c) > 2 and "BODY" in c[3] for c in self.imap.calls if c[:2] == ("uid", "FETCH")))
        self.imap.messages[MID]["labelIds"] = ["INBOX"]
        self.assertEqual(self.sync()["messages_written"], 1)

    def test_labels_directory_nonshrinking(self):
        self.sync()
        self.imap.labels = []
        self.sync()
        self.assertIn("_removedAt", load(self.fx.root / "labels.json")[label_id(LABEL)])

    def test_account_changed_casefold_and_bad_password(self):
        self.sync()
        self.assertEqual(self.sync(user="OWNER@example.com")["errors"], 0)
        with self.assertRaisesRegex(FetchError, "account_changed"):
            self.sync(user="another@example.org")
        with self.assertRaisesRegex(FetchError, "auth_required"):
            sync(self.fx.root, self.imap.client(password="wrong"))

    def test_partition_caps_and_noop(self):
        self.imap.messages[OTHER] = raw_message(OTHER)
        self.assertEqual(self.sync(partition_max_messages=1)["partitions_written"], 2)
        self.assertEqual(self.manifest()["files"], sorted(self.manifest()["files"]))
        self.assertEqual(self.sync()["messages_written"], 0)

    def test_failed_listing_does_not_infer_delete(self):
        self.sync()
        self.imap.fail.add("scan")
        self.assertEqual(self.sync()["errors"], 1)
        self.assertNotIn("_removedAt", self.state())

    def test_fetch_failure_keeps_content_and_deletion_state(self):
        self.sync()
        del self.imap.messages[MID]
        self.imap.messages[OTHER] = raw_message(OTHER)
        self.imap.messages["abc125"] = raw_message("abc125")
        self.imap.fail.add("abc125")
        self.assertEqual(self.sync()["errors"], 1)
        self.assertNotIn("_removedAt", self.state())
        self.assertEqual(self.state(OTHER), {"label_ids": ["INBOX"]})
        written = {v["id"] for rel in self.manifest()["files"] for v in load(self.fx.root / rel)["messages"]}
        self.assertIn(OTHER, written)
        self.imap.fail.clear()
        self.assertEqual(self.sync()["errors"], 0)
        self.assertIn("_removedAt", self.state())

    def test_crash_partition_recovery_no_duplicates(self):
        self.sync()
        man = self.manifest()
        original = man["files"].pop()
        self.fx.save("manifest.json", man)
        self.assertEqual(self.sync()["messages_written"], 0)
        self.assertIn(original, self.manifest()["files"])

    def test_lock(self):
        with locked(self.fx.root):
            with self.assertRaisesRegex(FetchError, "collector_busy"):
                self.sync()

    def test_orphan_shard_recovery_never_shrinks(self):
        self.sync()
        self.fx.save("state/ff.json", {"ff": {"label_ids": ["INBOX"]}})
        self.sync()
        self.assertIn("state/ff.json", self.manifest()["state_files"])
        self.assertIn("ff", load(self.fx.root / "state/ff.json"))


class ParseTests(unittest.TestCase):
    def test_literals_quoting_and_unsolicited_updates(self):
        data = [(b'1 (UID 7 X-GM-LABELS (\\Inbox "a \\"b\\"") BODY[] {5}', b"hello"), b" FLAGS (\\Seen))",
                b"2 (FLAGS (\\Seen))"]
        self.assertEqual(responses(data), [{"UID": "7", "X-GM-LABELS": ["\\Inbox", 'a "b"'], "BODY[]": b"hello",
                                            "FLAGS": ["\\Seen"]}, {"FLAGS": ["\\Seen"]}])
        for bad in ([b"1 (UID 7"], [b"1 UID 7)"]):
            with self.assertRaises(FetchError):
                responses(bad)

    def test_modified_utf7(self):
        self.assertEqual(utf7("~peter/mail/&U,BTFw-/&ZeVnLIqe-"), "~peter/mail/台北/日本語")
        self.assertEqual(utf7("A&-B"), "A&B")
        with self.assertRaises(FetchError):
            utf7("&2D0-")
