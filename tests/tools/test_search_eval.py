# -*- coding: utf-8 -*-
"""tools/search_eval.py: the metrics on hand-made rankings, the query sanitising, and hand-written items surviving a
regeneration. Synthetic data only; no app is searched here."""
import unittest

from tools import search_eval as E


class Metrics(unittest.TestCase):
    def test_first_gold_at_rank_three(self):
        s = E.score(["a", "b", "g1", "c", "g2"], ["g1", "g2"])
        self.assertEqual((s["hit@1"], s["hit@3"], s["hit@5"]), (0.0, 1.0, 1.0))
        self.assertEqual((s["recall@1"], s["recall@3"], s["recall@5"]), (0.0, 0.5, 1.0))
        self.assertAlmostEqual(s["rr"], 1 / 3)

    def test_gold_beyond_ten_is_a_miss(self):
        s = E.score([str(i) for i in range(10)] + ["g"], ["g"])
        self.assertEqual((s["hit@10"], s["recall@10"], s["rr"]), (0.0, 0.0, 0.0))

    def test_empty_ranking(self):
        s = E.score([], ["g"])
        self.assertEqual(s["rr"], 0.0)
        self.assertEqual(s["hit@10"], 0.0)

    def test_summarize_counts_errors_as_misses(self):
        ok = dict(E.score(["g"], ["g"]), ms=10.0, error=False, zero=False)
        bad = dict(E.score([], ["g"]), ms=99.0, error=True, zero=False)
        s = E.summarize([ok, bad])
        self.assertEqual((s["n"], s["errors"], s["mrr"], s["hit@1"]), (2, 1, 0.5, 0.5))
        self.assertEqual(s["p50_ms"], 10.0)         # errors have no latency

    def test_a_window_element_holds_all_its_keys(self):
        s = E.score(["a", frozenset({"b", "g1"}), "g2"], ["g1", "g2"])
        self.assertEqual((s["hit@1"], s["recall@3"]), (0.0, 1.0))
        self.assertAlmostEqual(s["rr"], 1 / 2)

    def test_hybrid_rankings_split_the_retrievers(self):
        rows = [{"key": "a", "sources": {"kw": 2}, "context": [{"key": "a"}, {"key": "a2"}]},
                {"key": "b", "sources": {"kw": 1, "sem": 3}},
                {"key": "c", "sources": {"sem": 1}, "context": [{"key": "c1"}]}]
        r = E.hybrid_rankings(rows)
        self.assertEqual(r["kw_only"], ["b", "a"])
        self.assertEqual(r["sem_only"], ["c", "b"])
        self.assertEqual(r["fused_window"], [frozenset({"a", "a2"}), frozenset({"b"}), frozenset({"c", "c1"})])
        self.assertEqual(r["sem_window"][0], frozenset({"c", "c1"}))

    def test_percentile(self):
        self.assertEqual(E.pct([5, 1, 3, 2, 4], 50), 3)
        self.assertEqual(E.pct([1, 2, 3, 4, 100], 95), 100)
        self.assertIsNone(E.pct([], 50))


class Queries(unittest.TestCase):
    def test_words_drop_query_syntax(self):
        self.assertEqual(E.words('Re: "배포" 일정 -긴급 OR api* 10월 회의! x'),
                         ["Re", "배포", "일정", "긴급", "api", "10월", "회의"])

    def test_title_query_needs_six_characters(self):
        self.assertIsNone(E.title_query("Re: 안녕"))
        self.assertEqual(E.title_query("[공지] 서버 점검 안내"), "공지 서버 점검 안내")

    def test_phrase_windows_skip_broken_runs(self):
        wins = E.phrase_windows("오늘 배포는 a@b.com 내일 다시")
        self.assertIn("오늘 배포는", wins)
        self.assertIn("내일 다시", wins)
        self.assertFalse(any("@" in w for w in wins))
        self.assertNotIn("배포는 내일", wins)       # not consecutive in the text


    def test_inflected_items(self):
        cands = E.inflectable("내일 일정 공유 회의는 api 서버")
        self.assertIn(("일정", "일정"), cands)
        self.assertIn(("내일 일정", "일정"), cands)
        self.assertNotIn(("회의는", "회의는"), cands)       # already inflected
        rng = __import__("random").Random(1)
        q = E.inflect("내일 일정", "일정", rng)
        self.assertTrue(q.startswith("내일 일정") and q[len("내일 일정"):] in ("을", "이", "은", "에서", "으로"))
        self.assertIn(E.inflect("서버", "서버", rng)[2:], ("를", "가", "는", "에서", "로"))

    def test_paraphrase_query(self):
        text = "다음 주 화요일 서버 점검으로 배포가 하루 늦어집니다"
        self.assertEqual(E.paraphrase_query("출시 일정이 미뤄진 이유?\n(설명)", text), "출시 일정이 미뤄진 이유")
        self.assertIsNone(E.paraphrase_query("서버 점검으로 배포가 늦어", text))     # mostly the row's own bigrams
        self.assertIsNone(E.paraphrase_query("왜", text))                              # too short
        self.assertIsNone(E.paraphrase_query("가" * 70, text))                         # too long
        self.assertIsNone(E.paraphrase_query("", text))

    def test_ask_llm_refuses_a_remote_server(self):
        with self.assertRaises(E.EvalError):
            E.ask_llm("http://example.com:11434", "m", "x")


class Store(unittest.TestCase):
    def test_manual_items_survive(self):
        old = [{"id": "auto-001", "source": "auto", "query": "x", "gold": ["k1"]},
               {"id": "m1", "source": "manual", "kind": "manual", "query": "송금 보냈", "gold": ["k2"]}]
        new = [{"id": "auto-001", "source": "auto", "query": "y", "gold": ["k3"]}]
        merged = E.merge_items(old, new)
        self.assertEqual([it["query"] for it in merged], ["y", "송금 보냈"])


if __name__ == "__main__":
    unittest.main()
