import json
import unittest
from unittest.mock import patch
from rawlayer.capture import fsio, publicout
from gmail.collector import sync, main
from gmail.readers.archive import containers, safe_rel
from gmail.keys import gm_id
from tools.archiveio import load
from .fixtures import Fixture, Transport, MID
from . import helpers as h


class HardeningTests(unittest.TestCase):
    def setUp(self):
        self.fx, self.http = Fixture(), Transport()
        self.addCleanup(h.force_rmtree, self.fx.base)
        sync(self.fx.root, self.http.client())

    def manifest(self):
        return load(self.fx.root / "manifest.json")

    def test_missing_partition_and_shard_exit_75(self):
        for field in ("files", "state_files"):
            with self.subTest(field=field):
                self.assertEqual(h.capture(self.fx), 0)
                path = self.fx.root / self.manifest()[field][0]
                data = path.read_bytes()
                path.unlink()
                self.assertEqual(h.capture(self.fx), 75)
                notes = h.manifests(self.fx)[-1]["origins"]["gm_archive"]["notes"]
                self.assertNotIn("vanished", notes)
                path.write_bytes(data)

    def test_bad_partition_rolls_back_only_its_container(self):
        self.assertEqual(h.capture(self.fx), 0)
        n = len(h.records(self.fx))
        path = self.fx.root / self.manifest()["files"][0]
        path.write_text("{", encoding="utf-8")
        self.fx.save("state/ab.json", {MID: {"label_ids": ["INBOX", "TRASH"]}})
        self.assertEqual(h.capture(self.fx), 75)
        self.assertEqual([r["kind"] for r in h.records(self.fx)[n:]], ["lbls"])

    def test_path_escape_rejected_without_open(self):
        man = self.manifest()
        man["files"] = ["exports/../outside.json"]
        self.fx.save("manifest.json", man)
        original = fsio.read_shared
        def read(path):
            self.assertNotIn("..", str(path))
            return original(path)
        with patch("gmail.readers.archive.fsio.read_shared", side_effect=read):
            self.assertEqual(h.capture(self.fx), 75)
        for value in ("exports/../outside.json", "exports\\bad.json", "C:/bad.json"):
            self.assertFalse(safe_rel(value, "exports"))

    def test_orphan_count_and_tmp_ignored(self):
        self.fx.save("exports/orphan.json", {})
        self.fx.save("state/ff.json", {})
        self.fx.save("exports/ignored.tmp", {})
        self.assertEqual(h.capture(self.fx), 0)
        self.assertEqual(h.manifests(self.fx)[-1]["origins"]["gm_archive"]["notes"]["orphan_file"], 2)

    def test_bad_pk_count_and_hex_not_snowflake(self):
        man = self.manifest()
        path = self.fx.root / man["files"][0]
        value = load(path)
        value["messages"][0]["id"] = "BAD"
        self.fx.save(man["files"][0], value)
        self.assertEqual(h.capture(self.fx), 0)
        self.assertEqual(h.manifests(self.fx)[-1]["origins"]["gm_archive"]["notes"]["pk_bad"], 1)
        for ident in ("f", "ffffffffffffffff", "0123"):
            self.assertEqual(gm_id(ident), ident)
        for ident in ("F", "a" * 17, "bad:id", 123):
            self.assertIsNone(gm_id(ident))

    def test_manifest_stability_per_lazy_container(self):
        cs = containers(self.fx.root)
        man = self.manifest()
        man["generation"] = "changed"
        self.fx.save("manifest.json", man)
        from rawlayer.capture.model import ContainerError
        with self.assertRaises(ContainerError):
            list(cs[0].iter_obs())

    def test_nonfinite_and_bad_state(self):
        path = self.fx.root / self.manifest()["files"][0]
        value = load(path)
        value["messages"][0]["size_estimate"] = float("nan")
        path.write_text(json.dumps(value))
        self.fx.save("state/ab.json", {MID: {"label_ids": "wrong"}})
        self.assertEqual(h.capture(self.fx), 75)
        value["messages"][0]["size_estimate"] = 0
        value["messages"][0]["internal_date_ms"] = 2 ** 63 - 1
        self.fx.save(self.manifest()["files"][0], value)
        self.assertEqual(h.capture(self.fx), 75)

    def test_manifest_missing_or_unsorted(self):
        man = self.manifest()
        man["files"] = man["files"] * 2
        self.fx.save("manifest.json", man)
        self.assertEqual(h.capture(self.fx), 75)

    def test_duplicate_message_partition_rejected(self):
        man = self.manifest()
        value = load(self.fx.root / man["files"][0])
        rel = "exports/20991001T000000000000Z.json"
        self.fx.save(rel, value)
        man["files"].append(rel)
        self.fx.save("manifest.json", man)
        self.assertEqual(h.capture(self.fx), 75)
        self.assertEqual(len(h.by_key(h.records(self.fx))["gm:msg:" + MID]), 1)

    def test_publicout_rejects_private_values_and_core_manifests_clean(self):
        self.assertEqual(h.capture(self.fx), 0)
        for man in h.manifests(self.fx):
            publicout.check(man)
            body = json.dumps(man)
            for secret in ("owner@example.com", "sender@example.org", "Synthetic deployment", MID):
                self.assertNotIn(secret, body)
        for secret in ("owner@example.com", "Synthetic deployment", "gm:msg:" + MID):
            with self.assertRaises(publicout.LeakError):
                publicout.check({"status": secret})

    def test_collector_cli_aggregate_exit_codes_and_source_key(self):
        import contextlib
        import io
        from tools.archiveio import CollectorError
        for result, expected in (({"status": "ok", "errors": 0}, 0), ({"status": "ok", "errors": 1}, 75),
                                 (CollectorError("auth_required"), 2), (CollectorError("account_changed"), 2)):
            out = io.StringIO()
            kw = {"side_effect": result} if isinstance(result, Exception) else {"return_value": result}
            with patch("gmail.collector.sync_config", **kw), patch("rawlayer.capture.precheck.check"), contextlib.redirect_stdout(out):
                self.assertEqual(main(["--config", str(self.fx.config_path)]), expected)
            publicout.check(json.loads(out.getvalue()))
        out = io.StringIO()
        with patch("gmail.collector.sync_config", return_value={"status": "owner@example.com"}), patch("rawlayer.capture.precheck.check"), contextlib.redirect_stdout(out):
            main(["--config", str(self.fx.config_path)])
        self.assertNotIn("owner@example.com", out.getvalue())
