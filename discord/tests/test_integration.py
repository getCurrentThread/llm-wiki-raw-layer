import contextlib
import io
import unittest
import xml.etree.ElementTree as ET
from unittest.mock import patch
from tests.core import appfix
from rawlayer import collect, schedule
from rawlayer.capture.config import Config
from discord import inputs
from .fixtures import Fixture
from . import helpers as h


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.fx = Fixture()
        self.addCleanup(h.force_rmtree, self.fx.base)

    def test_collect_pipeline(self):
        with appfix.preserved_env(), patch("rawlayer.capture.precheck.check"), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(collect.main(["discord", "--config", str(self.fx.config_path)]), 0)
        rc, out, err = h.run_rawsearch(["discord", "--config", self.fx.config_path, "deployment", "--json"])
        self.assertEqual(rc, 0, err)

    def test_schedule_dry_run_synthetic_no_registration(self):
        out = io.StringIO()
        with patch("rawlayer.capture.precheck.check"), patch("rawlayer.schedule.schtasks") as tasks, contextlib.redirect_stdout(out):
            rc = schedule.main(["install", "discord", "--config", str(self.fx.config_path), "--dry-run"])
        self.assertEqual(rc, 0)
        tasks.assert_not_called()
        xml = out.getvalue()
        ET.fromstring(xml)
        self.assertIn("00:14:00", xml)
        self.assertIn("PT3H", xml)
        self.assertIn("PT1H", xml)
        self.assertIn("collect discord", xml)
        self.assertIn("S-1-5-18", xml)

    def test_frozen_and_peak(self):
        from discord.origins import DcArchive
        self.assertEqual(inputs.resolve(Config(self.fx.config_path), "T1", self.fx.base)["dc_archive"], self.fx.base / "dc/archive")
        self.assertGreater(DcArchive().estimate_peak(type("Inputs", (), {"dc_archive": self.fx.root})()), 0)
