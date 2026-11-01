# -*- coding: utf-8 -*-
"""tools/search_eval.py (a measuring tool, not a contract): one smoke test of its metrics, and the refusal to send
a row's text to a remote server. Synthetic data only; no app is searched here."""
import unittest

from tools import search_eval as E


class SearchEval(unittest.TestCase):
    def test_score_and_summarize(self):
        s = E.score(["a", "b", "g1", "c", "g2"], ["g1", "g2"])
        self.assertEqual((s["hit@1"], s["hit@3"], s["recall@3"]), (0.0, 1.0, 0.5))
        self.assertAlmostEqual(s["rr"], 1 / 3)
        ok = dict(E.score(["g"], ["g"]), ms=10.0, error=False, zero=False)
        bad = dict(E.score([], ["g"]), ms=99.0, error=True, zero=False)
        s = E.summarize([ok, bad])
        self.assertEqual((s["n"], s["errors"], s["mrr"], s["hit@1"]), (2, 1, 0.5, 0.5))

    def test_ask_llm_refuses_a_remote_server(self):
        with self.assertRaises(E.EvalError):
            E.ask_llm("http://example.com:11434", "m", "x")


if __name__ == "__main__":
    unittest.main()
