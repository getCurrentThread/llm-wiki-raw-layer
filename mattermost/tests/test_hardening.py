# -*- coding: utf-8 -*-
"""Adoption hardening (comparison report 2026-10-04, section 4) on the mattermost app's synthetic archives: exit code on
an empty archive, the app's option, and MM orphan / traversal. Recovery, ledger defects, config, provenance, srcmap
shape, the lit() contract and real process kills are core rules, tested on the toy app in tests/core."""
import json
import shutil
import unittest

from rawlayer.capture.config import Config, ConfigError
from mattermost.tests.fixtures import Fixture, post
from mattermost.tests.helpers import MM, _force, capture, invariants, manifests, records
from mattermost.tests.test_m2 import std_channels


def set_cfg(fx, **kv):
    with fx.config_path.open(encoding="utf-8") as fh:
        cfg = json.load(fh)
    for k, v in kv.items():
        if v is None:
            cfg.pop(k, None)
        else:
            cfg[k] = v
    with fx.config_path.open("w", encoding="utf-8") as fh:
        json.dump(cfg, fh)


class Base(unittest.TestCase):
    def setUp(self):
        self.fx = Fixture()

    def tearDown(self):
        shutil.rmtree(self.fx.base, onerror=_force)


class TestExit(Base):
    def test_every_origin_on_the_standard_inputs(self):
        """mm_archive on an empty archive (a manifest without channels): exit 0."""
        self.assertEqual(capture(self.fx, MM), 0)
        m = manifests(self.fx)[-1]
        self.assertEqual({o: d["status"] for o, d in m["origins"].items()}, {"mm_archive": "ok"})
        self.assertEqual(invariants(self.fx)[0], {})


class TestConfig(Base):
    def refused(self, **kv):
        set_cfg(self.fx, **kv)
        with self.assertRaises(ConfigError):
            Config(self.fx.config_path)

    def test_option_types(self):
        for v in ("feedback", [""], [3]):
            with self.subTest(value=v):
                self.fx.write_config()
                self.refused(mm_exclude_patterns=v)
        self.fx.write_config()
        set_cfg(self.fx, mm_exclude_patterns=None)              # left out: the default (none excluded)
        self.assertEqual(Config(self.fx.config_path).mm_exclude, [])


class TestMMChannelFiles(Base):
    def test_excluded_channel_file_is_not_orphan(self):
        ch = std_channels()
        ch["c2"]["display"] = "My FEEDback room"
        self.fx.mm_write(ch)
        set_cfg(self.fx, mm_exclude_patterns=["feedBack"])
        self.assertEqual(capture(self.fx, MM), 0)
        notes = manifests(self.fx)[0]["origins"]["mm_archive"]["notes"]
        self.assertEqual((notes.get("excluded"), notes.get("orphan_file")), (1, None))

    def test_channel_path_cannot_leave_posts(self):
        ch = std_channels()
        ch["c9"] = {"path": "../evil.jsonl", "posts": [post("p99", "c9")]}
        self.fx.mm_write(ch)
        self.assertEqual(capture(self.fx, MM), 0)
        self.assertNotIn("mm:post:p99", {r["key"] for r in records(self.fx)})
        self.assertGreaterEqual(manifests(self.fx)[0]["origins"]["mm_archive"]["notes"].get("bad_line"), 1)


if __name__ == "__main__":
    unittest.main()
