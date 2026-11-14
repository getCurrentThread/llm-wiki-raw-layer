import unittest
from .fixtures import Fixture, PID, BID, page
from tests.core import appfix as h


class HardeningTests(unittest.TestCase):
    def setUp(self):
        self.fx = Fixture()
        self.addCleanup(h.force_rmtree, self.fx.base)

    def test_bad_file_all_or_nothing(self):
        """A torn page file, a bad block order or a non-finite value: the page is a bad file (exit 75), nothing of it
        is recorded."""
        self.assertEqual(h.capture(self.fx), 0)
        n = len(h.records(self.fx))
        nan = page()
        nan["page"]["bad"] = float("nan")
        for bad in ("{", page(order=[[BID, -1, None]]), page(order=[[BID, 0, "invalid"]]), nan):
            with self.subTest(bad=str(bad)[:40]):
                if isinstance(bad, str):
                    (self.fx.root / f"pages/{PID[:2]}/{PID}.json").write_text(bad, encoding="utf-8")
                else:
                    self.fx.write(bad)
                self.assertEqual(h.capture(self.fx), 75)
                self.assertEqual(len(h.records(self.fx)), n)

    def test_missing_block_suspect(self):
        self.assertEqual(h.capture(self.fx), 0)
        self.fx.write(page(blocks={}))
        self.assertEqual(h.capture(self.fx), 3)

    def test_data_source_and_users(self):
        from .fixtures import OTHER
        self.fx.save(f"ds/{OTHER}.json", {"id": OTHER, "title": []})
        self.assertEqual(h.capture(self.fx), 0)
        self.assertIn("ds", {r["kind"] for r in h.records(self.fx)})
