# -*- coding: utf-8 -*-
"""tests/core/ledgercheck.py itself: the pending fold of the ledger invariants (Q10 item 9, formerly C10) on
hand-made manifests, judged with the toy app's origins (toy_b two-phase absent, toy_m not)."""
import unittest

from rawlayer import registry
from tests.core import ledgercheck
from tests.core import toyfix as T


class TestPendingFold(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.enterClassContext(registry.use([T.TOY]))

    @staticmethod
    def man(seq, add=(), drop=()):
        return (seq, "r", "p", {"origins": {"toy_b": {"pending": {"add": list(add), "drop": list(drop)}}}})

    def test_absent_after_absent_without_new_pending(self):
        mans = [self.man(1, add=["K"]), self.man(2), self.man(3), self.man(4)]
        self.assertEqual(ledgercheck.pending_fold_violations(mans, {2: {"K"}}, "toy_b"), [])
        self.assertEqual(ledgercheck.pending_fold_violations(mans, {2: {"K"}, 4: {"K"}}, "toy_b"),
                         [("absent_not_allowed", "K")])
        self.assertEqual(ledgercheck.pending_fold_violations(mans, {2: {"K"}}, "toy_m"),
                         [("absent_not_allowed", "K")])

    def test_set_rules(self):
        mans = [self.man(1, add=["A", "B"]), self.man(2, add=["A"], drop=["C"]), self.man(3, add=["Z", "D"]),
                self.man(4, add=["E"], drop=["E"])]
        got = ledgercheck.pending_fold_violations(mans, {}, "toy_b")
        self.assertEqual(got, [("pending_bad", "A"), ("pending_bad", "C"), ("pending_bad", "Z"), ("pending_bad", "E")])


if __name__ == "__main__":
    unittest.main()
