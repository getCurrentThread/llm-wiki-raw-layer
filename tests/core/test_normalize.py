"""Korean query normalization (rawlayer.search.normalize): the rewrite table, and the superset rule -- a rewritten
or relaxed query returns every row the original returns -- against a real in-memory index (test_textindex's).
All data is synthesised in code."""
import random
import unittest

from rawlayer.search import normalize as N
from rawlayer.search.textindex import QueryError, build_fts_query
from tests.core.test_textindex import make_db, run_query


class Rewrite(unittest.TestCase):
    CASES = [
        ("송금을", "송금"),
        ("회의에서 결정", "회의 결정"),
        ("기능이", "기능"),
        ("서울로", "서울"),
        ("데이터를", "데이터"),
        ("확인했다 -취소", "확인 -취소"),
        ("공부했어요", "공부"),
        ("송금을 보냈다", "송금 보냈 OR 송금 보내"),
        ("말해", "말하 OR 말해 OR 말했"),
        ("보내 OR 송금을", "보내 OR 보냈 OR 송금"),
        ("api 배포는", "api 배포"),
        ('"정기 회의" 배포를', '"정기 회의" 배포'),
    ]
    KEPT = ["평가", "회의", "플레이", "카카오페이", "있다", "사과", "고양이", "api*", "송금", "배포 -회의를"]

    def test_table(self):
        for q, want in self.CASES:
            with self.subTest(q=q):
                self.assertEqual(N.rewrite(q), want)

    def test_kept_as_typed(self):
        for q in self.KEPT:
            with self.subTest(q=q):
                self.assertEqual(N.rewrite(q), N.canonical(q))

    def test_stems_only_past_the_group_cap(self):
        q = " ".join(["보내"] * 4)      # 2**4 = 16 groups > MAX_GROUPS
        self.assertEqual(N.rewrite(q), q)
        self.assertEqual(N.rewrite("보냈다", expand=False), "보냈")
        q = " ".join(["보내", "말했다"] * 400)     # 6**400 groups: counted, never written out
        self.assertEqual(N.rewrite(q), " ".join(["보내", "말했"] * 400))

    def test_relax(self):
        self.assertEqual(N.relax("송금 보냈 -취소"), "송금 OR 보냈 -취소")
        self.assertIsNone(N.relax("송금"))
        self.assertIsNone(N.relax("송금 OR 보냈"))
        # a literal term cannot be OR-ed: it is dropped, the rest relax
        self.assertEqual(N.relax('"정기 회의" 배포'), "배포")
        self.assertEqual(N.relax("새 서버 안 켜짐 -취소"), "서버 OR 켜짐 -취소")
        self.assertEqual(N.relax("c++ api* 배포"), "api* OR 배포")
        self.assertIsNone(N.relax('"정기 회의" 송'))
        # a rewritten query is a cross product: as many groups as terms is not "already OR-ed"
        self.assertEqual(N.relax(N.rewrite("보냈다 건넸다")), "보냈 OR 건넸 OR 건네 OR 보내")


class Superset(unittest.TestCase):
    WORDS = ["송금", "배포", "회의", "서버", "데이터", "확인", "보내", "보냈", "공부", "기능", "결정", "일정", "평가",
             "고양이", "플레이", "api", "10월"]
    TAILS = ["", "", "을", "를", "이", "가", "에서", "으로", "로", "는", "은", "했다", "했어요", "하고", "다", "의", "도"]

    def corpus(self, rng):
        return [" ".join(rng.choice(self.WORDS) + rng.choice(self.TAILS) for _ in range(rng.randint(1, 6)))
                for _ in range(400)]

    def test_rewrite_and_relax_widen_only(self):
        rng = random.Random(7)
        bodies = self.corpus(rng)
        con = make_db(bodies)
        try:
            for _ in range(600):
                parts = [rng.choice(self.WORDS) + rng.choice(self.TAILS) for _ in range(rng.randint(1, 3))]
                if rng.random() < 0.2:
                    parts.insert(1, "OR")
                if rng.random() < 0.2 and len(parts) > 1 and parts[-1] != "OR":
                    parts.append("-" + rng.choice(self.WORDS))
                q = " ".join(p for i, p in enumerate(parts) if not (p == "OR" and (i == 0 or i == len(parts) - 1)))
                try:
                    base = run_query(con, build_fts_query(q))
                except QueryError:
                    continue
                for nq in (N.rewrite(q), N.rewrite(q, expand=False), N.relax(N.rewrite(q))):
                    if nq is None:
                        continue
                    with self.subTest(q=q, nq=nq):
                        self.assertLessEqual(base, run_query(con, build_fts_query(nq)))
        finally:
            con.close()


if __name__ == "__main__":
    unittest.main()
