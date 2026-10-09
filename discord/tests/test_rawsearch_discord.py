import json
import sqlite3
import unittest
from contextlib import closing
from rawlayer.search import common
from discord.search import SPEC, DiscordApplier, msg_text
from .fixtures import Fixture, CID, MID, UID, message
from . import helpers as h


class SearchTests(unittest.TestCase):
    def setUp(self):
        self.fx = Fixture()
        self.addCleanup(h.force_rmtree, self.fx.base)
        self.assertEqual(h.capture(self.fx), 0)
        common.update(self.fx.data_root, SPEC)

    def query(self, *args):
        rc, out, err = h.run_rawsearch(["discord", "--config", self.fx.config_path, "--json", *args])
        self.assertEqual(rc, 0, err)
        return json.loads(out)

    def snapshot(self):
        with closing(sqlite3.connect(common.index_path(self.fx.data_root, SPEC))) as con:
            return {t: con.execute(f"SELECT * FROM {t} ORDER BY 1").fetchall()
                    for t in ("msgs", "ch_src", "guild_src", "users", "channels", "counts")}

    def test_incremental_equals_rebuild_three_rounds(self):
        for text in ("edited deployment", "synthetic deployment"):
            self.fx.write({CID: [[message(text=text)]]})
            self.assertEqual(h.capture(self.fx), 0)
            common.update(self.fx.data_root, SPEC)
        incremental = self.snapshot()
        common.update(self.fx.data_root, SPEC, rebuild=True)
        self.assertEqual(incremental, self.snapshot())

    def test_filters_modes_envelope_and_text(self):
        for mode in ("hits", "count", "timeline", "senders", "channels"):
            result = self.query("deployment", "--mode", mode, "--guild", "Guild", "--sender", "Alias",
                                "--channel", CID, "--since", "2024-01", "--until", "2025-01")
            self.assertEqual(result["total"], 1)
            self.assertFalse(result["index"]["stale"])
            self.assertEqual(result["source"], "discord")
            rc, out, err = h.run_rawsearch(["discord", "--config", self.fx.config_path, "deployment", "--mode", mode])
            self.assertEqual(rc, 0, err)
            self.assertTrue(out)
        self.assertEqual(self.query("deployment", "--guild", "absent")["total"], 0)

    def test_context_browse_paging(self):
        msgs = [message(str(int(MID) + i), f"deployment {i}") for i in range(5)]
        self.fx.write({CID: [msgs]})
        self.assertEqual(h.capture(self.fx), 0)
        common.update(self.fx.data_root, SPEC)
        result = self.query("deployment", "--context", "1")
        self.assertTrue(result["rows"][0]["context"])
        first = self.query("--channel", CID, "--limit", "2")
        older = self.query("--channel", CID, "--limit", "2", "--before", first["page"]["before"])
        self.assertFalse({r["key"] for r in first["rows"]} & {r["key"] for r in older["rows"]})
        newer = self.query("--channel", CID, "--limit", "2", "--after", older["page"]["after"])
        self.assertEqual([r["key"] for r in first["rows"]], [r["key"] for r in newer["rows"]])

    def test_thread_labels_rename(self):
        reply = message(str(int(MID) + 1), reference={"messageId": MID, "channelId": CID})
        self.fx.write({CID: [[message(), reply]], MID: [[message()]]}, parents={MID: CID})
        self.assertEqual(h.capture(self.fx), 0)
        common.update(self.fx.data_root, SPEC)
        self.assertEqual(self.query("--thread", MID)["total"], 3)
        man = json.loads((self.fx.root / "manifest.json").read_text())
        man["channels"][CID]["name"] = "renamed"
        self.fx.save("manifest.json", man)
        self.fx.save("users.json", {UID: {"id": UID, "name": "Renamed user"}})
        self.assertEqual(h.capture(self.fx), 0)
        common.update(self.fx.data_root, SPEC)
        result = self.query("--list-channels", "--guild", "Guild")
        self.assertTrue(any(r["label"].startswith("renamed ›") for r in result["rows"]))
        incremental = self.snapshot()
        common.update(self.fx.data_root, SPEC, rebuild=True)
        self.assertEqual(incremental, self.snapshot())

    def test_deleted_and_text_fallback(self):
        self.fx.write({CID: [[message(_removedAt="2024-10-01T00:00:00Z", text="")]]})
        self.assertEqual(h.capture(self.fx), 0)
        common.update(self.fx.data_root, SPEC)
        self.assertEqual(self.query("deployment")["total"], 0)
        self.assertEqual(self.query("deployment", "--include-deleted")["total"], 1)
        self.assertEqual(msg_text({"embeds": [{"title": "embed"}]}), ("embed", "embeds"))
        self.assertEqual(msg_text({"attachments": [{"fileName": "file.txt"}]}), ("file.txt", "files"))
        self.assertEqual(msg_text({"forwardedMessage": {"content": "forward"}}), ("forward", "forwarded"))

    def test_thread_from_inner_message(self):
        inner = message(str(int(MID) + 1), "thread reply")
        self.fx.write({CID: [[message()]], MID: [[message(), inner]]}, parents={MID: CID})
        self.assertEqual(h.capture(self.fx), 0)
        common.update(self.fx.data_root, SPEC)
        result = self.query("--thread", inner["id"])
        self.assertEqual(result["root_id"], MID)
        self.assertEqual(result["total"], 3)

    def test_usage_errors(self):
        for args in ([], ["--thread", "bad"], ["--channel", CID, "--before", "bad"],
                     ["--channel", CID, "--before", "9" * 5000 + ".1.1"],
                     ["deployment", "--limit", "-1"], ["deployment", "--since", "bad"],
                     ["--thread", MID, "--list-channels"]):
            rc, out, err = h.run_rawsearch(["discord", "--config", self.fx.config_path, *args])
            self.assertEqual(rc, 2, (args, out, err))

    def test_direct_fold_full_incremental_and_odd_counts(self):
        records = h.records(self.fx)
        out = []
        for full in (False, True):
            con = sqlite3.connect(":memory:")
            self.addCleanup(con.close)
            con.executescript(common.META_DDL)
            SPEC.create_tables(con)
            app = DiscordApplier(con, full)
            for rec in records:
                app.record(rec, 1)
                if not full:
                    app.end_manifest(1)
            for bad in (None, {}, {"origin": "dc_archive", "op": "put", "rev": 1, "key": "bad", "kind": "msg"},
                        dict(records[0], rev=2 ** 80), dict(records[0], rev=99, native=[])):
                app.record(bad, 1)
            app.end_manifest(1)
            if full:
                app.finish()
            out.append({t: con.execute(f"SELECT * FROM {t} ORDER BY 1").fetchall() for t in ("msgs", "channels", "counts")})
        self.assertEqual(out[0], out[1])
        self.assertTrue(out[0]["counts"])
