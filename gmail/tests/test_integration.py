import contextlib
import io
import json
import sqlite3
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET
from tests.core import appfix
from rawlayer import collect, schedule
from rawlayer.capture.config import Config
from rawlayer.search import common
from gmail.collector import sync, sync_config
from gmail.search import SPEC, GmailApplier
from gmail.origins import GmArchive
from gmail import inputs
from .fixtures import Fixture, Transport, MID, OTHER, THREAD, raw_message
from . import helpers as h


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.fx, self.http = Fixture(), Transport()
        self.addCleanup(h.force_rmtree, self.fx.base)
        self.http.messages[OTHER] = raw_message(OTHER, labels=["INBOX", "Label_1"], attachment=b"synthetic")
        sync(self.fx.root, self.http.client())

    def build(self):
        self.assertEqual(h.capture(self.fx), 0)
        common.update(self.fx.data_root, SPEC)

    def query(self, *args):
        rc, out, err = h.run_rawsearch(["gmail", "--config", self.fx.config_path, "--json", *args])
        self.assertEqual(rc, 0, err)
        return json.loads(out)

    def snapshot(self):
        with contextlib.closing(sqlite3.connect(common.index_path(self.fx.data_root, SPEC))) as con:
            return {t: con.execute(f"SELECT * FROM {t} ORDER BY 1").fetchall()
                    for t in ("msgs", "states", "labels", "membership", "counts")}

    def test_collect_pipeline_ledger_index_search_replay(self):
        with appfix.preserved_env(), patch("rawlayer.capture.precheck.check"), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(collect.main(["gmail", "--config", str(self.fx.config_path)]), 0)
        self.assertEqual(self.query("deployment")["total"], 2)
        self.assertFalse(h.invariants(self.fx)[0])
        self.assertEqual(h.replay_diff(self.fx), 0)

    def test_modes_filters_and_shared_envelope(self):
        self.build()
        for mode in ("hits", "count", "timeline", "senders", "labels", "threads"):
            result = self.query("배포", "--mode", mode, "--label", "Inbox", "--from", "sender@example.org", "--thread", THREAD,
                                "--since", "2024-01", "--until", "2025-01")
            self.assertEqual(result["total"], 2)
            self.assertEqual(result["source"], "gmail")
            self.assertFalse(result["index"]["stale"])
            rc, out, err = h.run_rawsearch(["gmail", "--config", self.fx.config_path, "deployment", "--mode", mode])
            self.assertEqual(rc, 0, err)
            self.assertTrue(out)
        self.assertEqual(self.query("deployment", "--label", "Label_1")["total"], 1)
        self.assertEqual(self.query("deployment", "--label", "Synthetic label")["total"], 1)
        self.assertEqual(self.query("deployment", "--from", "absent@example.net")["total"], 0)
        self.assertEqual(self.query("synthetic.bin")["total"], 1)
        self.assertEqual(self.query("recipient@example.net")["total"], 2)

    def test_incremental_equals_rebuild_label_delete_rename(self):
        self.build()
        self.http.history = [{"historyId": "12", "history": [{"labelsAdded": [{"message": {"id": MID}, "labelIds": ["TRASH"]}],
                                                                  "messagesDeleted": [{"message": {"id": OTHER}}]}]}]
        self.http.labels[-1]["name"] = "Renamed synthetic label"
        sync(self.fx.root, self.http.client())
        self.build()
        self.assertEqual(self.query("deployment")["total"], 0)
        self.assertEqual(self.query("deployment", "--include-removed")["total"], 2)
        incremental = self.snapshot()
        common.update(self.fx.data_root, SPEC, rebuild=True)
        self.assertEqual(incremental, self.snapshot())

    def test_hex_text_paging_and_context(self):
        self.http.messages["ffffffffffffffff"] = raw_message("ffffffffffffffff")
        sync(self.fx.root, self.http.client(), full=True)
        self.build()
        first = self.query("--thread", THREAD, "--limit", "1")
        self.assertEqual(first["rows"][0]["msg_id"], "ffffffffffffffff")
        older = self.query("--thread", THREAD, "--limit", "1", "--before", first["page"]["before"])
        self.assertNotEqual(first["rows"][0]["msg_id"], older["rows"][0]["msg_id"])
        newer = self.query("--thread", THREAD, "--limit", "1", "--after", older["page"]["after"])
        self.assertEqual(first["rows"][0]["msg_id"], newer["rows"][0]["msg_id"])
        self.assertTrue(self.query("deployment", "--context", "1")["rows"][0]["context"])

    def test_usage_errors(self):
        self.build()
        for args in ([], ["--thread", "BAD"], ["--label", "INBOX", "--before", "bad"], ["deployment", "--limit", "-1"]):
            rc, out, err = h.run_rawsearch(["gmail", "--config", self.fx.config_path, *args])
            self.assertEqual(rc, 2)

    def test_schedule_dry_run_and_missing_credentials(self):
        out = io.StringIO()
        with patch("rawlayer.capture.precheck.check"), patch("rawlayer.schedule.schtasks") as tasks, contextlib.redirect_stdout(out):
            self.assertEqual(schedule.main(["install", "gmail", "--config", str(self.fx.config_path), "--dry-run"]), 0)
        tasks.assert_not_called()
        ET.fromstring(out.getvalue())
        self.assertIn("00:44:00", out.getvalue())
        self.assertIn("PT3H", out.getvalue())
        self.assertIn("PT1H", out.getvalue())
        cfg = Config(self.fx.config_path)
        cfg.gm_sync = True
        from gmail.apps import GmailConfig
        self.assertTrue(GmailConfig("gmail").task_checks(cfg))

    def test_options_and_protected_schedule_credentials(self):
        from gmail.apps import GmailConfig, body_bytes
        app, cfg = GmailConfig("gmail"), Config(self.fx.config_path)
        self.assertEqual(cfg.gm_body_max_bytes, 131072)
        self.assertEqual(cfg.gm_partition_max_messages, 1000)
        self.assertFalse(cfg.gm_sync)
        cfg.gm_sync = True
        cfg.gm_client_file, cfg.gm_token_file = str(self.fx.base / "client.json"), str(self.fx.base / "token.json")
        for path in (cfg.gm_client_file, cfg.gm_token_file):
            from pathlib import Path
            Path(path).write_text("{}")
        with patch("rawlayer.capture.precheck.check"):
            self.assertEqual(app.task_checks(cfg), [])
        for value in (-1, True, 16 * 1024 * 1024 + 1):
            with self.assertRaises(ValueError):
                body_bytes(value)

    def test_frozen_peak_and_sync_config_fake_transport(self):
        cfg = Config(self.fx.config_path)
        self.assertEqual(inputs.resolve(cfg, "T1", self.fx.base)["gm_archive"], self.fx.base / "gm/archive")
        self.assertGreater(GmArchive().estimate_peak(type("Inputs", (), {"gm_archive": self.fx.root})()), 0)
        from gmail.api import GmailClient
        client = self.http.client()
        with patch("gmail.collector.refresh", return_value="synthetic"), patch("gmail.collector.GmailClient", return_value=client):
            self.assertEqual(sync_config(cfg, auth_only=True)["status"], "authenticated")

    def test_bad_records_total_and_state_before_content(self):
        self.build()
        recs = h.records(self.fx)
        snapshots = []
        for full in (False, True):
            with contextlib.closing(sqlite3.connect(":memory:")) as con:
                con.executescript(common.META_DDL)
                SPEC.create_tables(con)
                applier = GmailApplier(con, full)
                for rec in sorted(recs, key=lambda r: r["kind"] == "msg"):
                    applier.record(rec, 1)
                    if not full:
                        applier.end_manifest(1)
                for bad in (None, {}, dict(recs[0], rev=2 ** 80), dict(recs[0], key="bad"), dict(recs[0], native=[])):
                    applier.record(bad, 1)
                applier.end_manifest(1)
                if full:
                    applier.finish()
                snapshots.append({t: con.execute(f"SELECT * FROM {t} ORDER BY 1").fetchall() for t in ("msgs", "membership", "counts")})
        self.assertEqual(snapshots[0], snapshots[1])
