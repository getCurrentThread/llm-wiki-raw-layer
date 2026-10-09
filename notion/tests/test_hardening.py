import unittest
from notion.keys import nt_id, iso_ms
from .fixtures import Fixture, PID, BID, page
from . import helpers as h


class HardeningTests(unittest.TestCase):
    def setUp(self):
        self.fx = Fixture()
        self.addCleanup(h.force_rmtree, self.fx.base)

    def test_bad_file_all_or_nothing(self):
        self.assertEqual(h.capture(self.fx), 0)
        n = len(h.records(self.fx))
        (self.fx.root / f"pages/{PID[:2]}/{PID}.json").write_text("{", encoding="utf-8")
        self.assertEqual(h.capture(self.fx), 75)
        self.assertEqual(len(h.records(self.fx)), n)

    def test_missing_block_suspect(self):
        self.assertEqual(h.capture(self.fx), 0)
        self.fx.write(page(blocks={}))
        self.assertEqual(h.capture(self.fx), 3)

    def test_invalid_ids_times(self):
        for value in (None, 12, "bad", "", True):
            self.assertIsNone(nt_id(value))
        for value in (None, "bad", "2024-10-01T00:00:00", True):
            self.assertIsNone(iso_ms(value))

    def test_bad_order(self):
        value = page(order=[[BID, -1, None]])
        self.fx.write(value)
        self.assertEqual(h.capture(self.fx), 75)

    def test_data_source_and_users(self):
        from .fixtures import OTHER
        self.fx.save(f"ds/{OTHER}.json", {"id": OTHER, "title": []})
        self.assertEqual(h.capture(self.fx), 0)
        self.assertIn("ds", {r["kind"] for r in h.records(self.fx)})

    def test_nonfinite_page_is_bad_file(self):
        value = page()
        value["page"]["bad"] = float("nan")
        self.fx.write(value)
        self.assertEqual(h.capture(self.fx), 75)

    def test_invalid_order_parent(self):
        self.fx.write(page(order=[[BID, 0, "invalid"]]))
        self.assertEqual(h.capture(self.fx), 75)
