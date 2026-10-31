"""Tokenizer + query compiler (rawsearch.textindex): regressions and a randomized property test.

Every search here runs against a real in-memory FTS5 external-content table built exactly like
the rawsearch index (fts_ddl + ng_text) and applies the Query the way the search does
(sql_parts + register_functions). All data is synthesised in code.
"""

import os
import random
import sqlite3
import unicodedata
import unittest

from rawlayer.search import textindex as ti
from rawlayer.search.textindex import QueryError, build_fts_query


def make_db(bodies, titles=None):
    """titles: one per body -> a title index (tng column, title_rank) like gmail's and notion's."""
    con = sqlite3.connect(":memory:")
    ti.register_functions(con)
    con.execute("CREATE TABLE docs(id INTEGER PRIMARY KEY, message TEXT NOT NULL, ng TEXT NOT NULL, tng TEXT NOT NULL)")
    con.execute(ti.fts_ddl("docs", title=titles is not None))
    if titles is not None:
        con.execute(ti.title_rank("docs_fts"))
    ngs = [ti.ng_text(b) for b in bodies]
    con.executemany("INSERT INTO docs(id, message, ng, tng) VALUES(?,?,?,?)",
                    [(i + 1, b, ng, ti.title_ng(titles[i], ng) if titles else "") for i, (b, ng) in enumerate(zip(bodies, ngs))])
    con.execute("INSERT INTO docs_fts(docs_fts) VALUES('rebuild')")
    return con


def run_query(con, q):
    frm, where, params = ti.sql_parts(q, "docs")
    sql = f"SELECT t.id FROM {frm} WHERE {' AND '.join(where) or '1'}"
    return {r[0] for r in con.execute(sql, params)}


def hits(bodies, query):
    """Bodies (by value) a query returns."""
    con = make_db(bodies)
    try:
        ids = run_query(con, build_fts_query(query))
    finally:
        con.close()
    return {bodies[i - 1] for i in ids}


# --------------------------------------------------------------------------------------------
# Independent reference implementation of the TRUTH in the textindex docstring.
# --------------------------------------------------------------------------------------------

def t_norm(s):
    return unicodedata.normalize("NFC", unicodedata.normalize("NFC", s).casefold())


def t_is_l(c):
    return c in "0123456789abcdefghijklmnopqrstuvwxyz"


def t_is_n(c):
    o = ord(c)
    return (0xAC00 <= o <= 0xD7A3 or 0x1100 <= o <= 0x11FF or 0x3130 <= o <= 0x318F
            or 0x4E00 <= o <= 0x9FFF or 0x3040 <= o <= 0x30FF)


def t_is_literal(t, quoted):
    if quoted or any(not (t_is_n(c) or t_is_l(c)) for c in t):
        return True
    return len(t) == 1 and t_is_n(t[0])


def t_term_matches(body, text, quoted, prefix):
    m, t = t_norm(body), t_norm(text)
    if t_is_literal(t, quoted):
        return t in m
    start = 0
    while True:
        i = m.find(t, start)
        if i < 0:
            return False
        j = i + len(t)
        ok = True
        if t_is_l(t[0]) and i > 0 and t_is_l(m[i - 1]):
            ok = False
        if not prefix and t_is_l(t[-1]) and j < len(m) and t_is_l(m[j]):
            ok = False
        if ok:
            return True
        start = i + 1


def t_query_matches(body, items):
    """items: [(kind, op, text, quoted, prefix)], kind 'pos'/'neg', op 'AND'/'OR'."""
    groups = [[]]
    for kind, op, text, quoted, prefix in items:
        if kind == "neg":
            continue
        if op == "OR" and groups[-1]:
            groups.append([])
        groups[-1].append((text, quoted, prefix))
    pos = any(all(t_term_matches(body, *term) for term in g) for g in groups if g)
    neg = any(t_term_matches(body, text, quoted, prefix)
              for kind, _, text, quoted, prefix in items if kind == "neg")
    return pos and not neg


def t_expect_refusal(items):
    has_or = any(op == "OR" for kind, op, *_ in items if kind == "pos")
    for _, _, text, quoted, _ in items:
        t = t_norm(text)
        if not any(t_is_n(c) or t_is_l(c) for c in t):
            return True
        if has_or and t_is_literal(t, quoted):
            return True
    return False


def render_query(items):
    out = []
    for kind, op, text, quoted, prefix in items:
        s = f'"{text}"' if quoted else text + ("*" if prefix else "")
        if kind == "neg":
            s = "-" + s
        elif op == "OR":
            s = "OR " + s
        out.append(s)
    return " ".join(out)


# --------------------------------------------------------------------------------------------
# Regressions and documented behaviour
# --------------------------------------------------------------------------------------------

class MissedMatchRegressions(unittest.TestCase):
    """ngram-1 emitted a lone token for a 1-char CJK run next to Latin/digits: 0 hits."""

    def test_digit_then_one_syllable_then_particle(self):
        self.assertEqual(hits(["10월에 보자", "11월에 보자"], "10월"), {"10월에 보자"})
        self.assertEqual(hits(["3시에 만나", "4시에 만나"], "3시"), {"3시에 만나"})

    def test_latin_then_one_syllable_then_particle(self):
        self.assertEqual(hits(["SQL문을 고쳤다", "SQL 문을 고쳤다"], "SQL문"), {"SQL문을 고쳤다"})

    def test_one_syllable_then_latin(self):
        self.assertEqual(hits(["오전10시", "오전 10시", "전10"], "전10"), {"오전10시", "전10"})

    def test_bare_forms_still_match(self):
        self.assertEqual(hits(["10월", "3시", "sql문"], "10월"), {"10월"})
        self.assertEqual(hits(["10월", "3시", "sql문"], "SQL문"), {"sql문"})

    def test_digit_edge_is_a_whole_run(self):
        self.assertEqual(hits(["210월에", "10월에", "x10월"], "10월"), {"10월에"})


class GapFalsePositiveRegressions(unittest.TestCase):
    """ngram-1 let bigrams of two separate runs line up as one phrase."""

    def test_space_between_runs(self):
        self.assertEqual(hits(["가나 나다", "가나다라"], "가나다"), {"가나다라"})

    def test_punctuation_and_emoji_between_runs(self):
        bodies = ["가나,나다", "가나\U0001F600나다", "가나\n나다", "가나다"]
        self.assertEqual(hits(bodies, "가나다"), {"가나다"})

    def test_latin_head_across_gap(self):
        self.assertEqual(hits(["a가 가나", "a가나"], "a가나"), {"a가나"})


class KbBehaviour(unittest.TestCase):

    def test_stem_matches_with_particle_and_compound(self):
        self.assertEqual(hits(["송금을 했어", "입금송금 완료", "송신 완료"], "송금"),
                         {"송금을 했어", "입금송금 완료"})

    def test_particle_narrows(self):
        self.assertEqual(hits(["송금을 했어", "입금송금 완료", "송금이 늦어"], "송금을"),
                         {"송금을 했어"})

    def test_cpp_is_literal(self):
        q = build_fts_query("C++")
        self.assertEqual(q.verify, ["c++"])
        self.assertEqual(hits(["C++ 공부", "c 언어", "vc++ 설치", "c+ 등급"], "C++"),
                         {"C++ 공부", "vc++ 설치"})

    def test_quoted_phrase_is_literal_and_narrowed(self):
        q = build_fts_query('"정기 회의"')
        self.assertEqual(q.verify, ["정기 회의"])
        self.assertTrue(q.fts)
        self.assertEqual(hits(["정기 회의 공지", "정기회의 공지", "회의 정기"], '"정기 회의"'),
                         {"정기 회의 공지"})

    def test_single_syllable_is_weak(self):
        q = build_fts_query("돈")
        self.assertEqual((q.fts, q.weak), ("", ["돈"]))
        self.assertEqual(hits(["돈을 보냄", "송금", "돈"], "돈"), {"돈을 보냄", "돈"})

    def test_or_and_exclude(self):
        bodies = ["회의 시작", "미팅 시작", "회의 취소", "잡담"]
        self.assertEqual(hits(bodies, "회의 OR 미팅"), {"회의 시작", "미팅 시작", "회의 취소"})
        self.assertEqual(hits(bodies, "회의 -취소"), {"회의 시작"})
        self.assertEqual(hits(bodies, "회의 OR 미팅 -취소"), {"회의 시작", "미팅 시작"})

    def test_and_binds_tighter_than_or(self):
        bodies = ["a b", "c", "a", "b c"]
        self.assertEqual(hits(bodies, "a b OR c"), {"a b", "c", "b c"})
        self.assertEqual(hits(bodies, "a OR b c"), {"a b", "a", "b c"})

    def test_latin_whole_run_and_prefix(self):
        bodies = ["API 키", "rapid 배포", "apis 목록", "api키"]
        self.assertEqual(hits(bodies, "api"), {"API 키", "api키"})
        self.assertEqual(hits(bodies, "api*"), {"API 키", "apis 목록", "api키"})

    def test_exclusion_uses_the_same_rule(self):
        self.assertEqual(hits(["rapid 회의", "api 회의"], "회의 -api"), {"rapid 회의"})

    def test_case_and_normalisation(self):
        nfd = unicodedata.normalize("NFD", "송금을 OAuth")
        self.assertEqual(hits([nfd, "다른 글"], "송금"), {nfd})
        self.assertEqual(hits([nfd, "다른 글"], "oauth"), {nfd})
        self.assertEqual(hits([nfd, "다른 글"], '"금을 oa"'), {nfd})

    def test_url_is_not_a_column_filter(self):
        q = build_fts_query("https://example.com/a")
        self.assertEqual(hits(["see https://example.com/a now", "example.com"], "https://example.com/a"),
                         {"see https://example.com/a now"})
        self.assertTrue(q.fts)

    def test_word_ending_in_a_colon_is_not_a_column_filter(self):
        """Mail subjects and notes: "Re: 견적", "TODO: 배포" are text; only word:value reads as a column filter."""
        self.assertEqual(hits(["Re: 견적 요청", "견적 없음"], "Re: 견적"), {"Re: 견적 요청"})
        self.assertTrue(build_fts_query("TODO: 배포").fts)

    def test_refused_forms(self):
        for bad in ["", "   ", "room:스터디", "sender:x", "-취소 회의", "C++ OR 회의",
                    "회의 OR -취소", "회의 -취소 OR 미팅", '"unbalanced', "!!!", "회의 OR 돈",
                    "회의 OR 미팅 -C++"]:
            with self.subTest(query=bad):
                with self.assertRaises(QueryError):
                    build_fts_query(bad)

    def test_runs_past_the_fts5_token_cut(self):
        """Review finding: FTS5 cuts tokens at 32768 bytes, so a query run that long also matched documents whose
        run merely starts with it. Such non-literal terms are refused; up to MAX_TERM_RUN the index is exact even
        against documents whose runs are cut."""
        rng = random.Random(7)
        base = "".join(rng.choice("abcdefghij") for _ in range(40000))
        for n in (ti.MAX_TERM_RUN + 1, 32768, 40000):
            for q in (base[:n], base[:n] + "*", "회의 -" + base[:n], "회의 " + base[:n] + "에서"):
                with self.subTest(n=n, q=q[:12]), self.assertRaises(QueryError):
                    build_fts_query(q)
        cut = base[:ti.MAX_TERM_RUN]
        bodies = [cut, cut + "zz", "zz" + cut, cut[:-1], base, base[:32768] + "q", "다른 " + cut + "에서"]
        self.assertEqual(hits(bodies, cut), {cut, "다른 " + cut + "에서"})
        self.assertEqual(hits(bodies, cut + "*"), {cut, cut + "zz", base, base[:32768] + "q", "다른 " + cut + "에서"})
        self.assertEqual(hits(bodies, f'"{base[:32768]}"'), {base, base[:32768] + "q"})   # literal: rs_contains

    def test_api_surface(self):
        self.assertNotEqual(ti.TOKENIZER_VERSION, "ngram-1")
        self.assertTrue(ti.SCHEMA_VERSION)
        q = build_fts_query("송금")
        self.assertEqual(q._fields, ("fts", "verify", "exclude", "weak", "marks", "exclude_fts"))
        self.assertTrue(ti.is_weak_term("돈"))
        self.assertFalse(ti.is_weak_term("송금"))
        self.assertEqual(ti.ng_text("오늘 송금을"), f"오늘 {ti.SEP} 송금 금을")
        self.assertEqual(ti.ng_text("10월에"), "10 월 월에")
        self.assertEqual(ti.ng_text(""), "")
        self.assertIn("tokenize='ascii'", ti.fts_ddl("item"))

    def test_title_ng_only_when_contiguous(self):
        ng = ti.ng_text("회의록 정리\n본문 API문서")
        self.assertEqual(ti.title_ng("회의록 정리", ng), ti.ng_text("회의록 정리"))
        self.assertEqual(ti.title_ng("API문서", ng), "api 문 문서")
        for title in ("정리 회의록", "회의 정리", "", "!!", "가", "문서 api"):   # not one run of ng
            self.assertEqual(ti.title_ng(title, ng), "", title)
        self.assertEqual(ti.title_ng("a", ti.ng_text("ab")), "")

    def test_title_column_ranks_title_hits_first(self):
        bodies = ["예산 예산 예산 회의\n다른 내용", "예산안 검토\n본문", "본문만 예산 이야기 예산"]
        titles = ["다른 제목", "예산안 검토", "x"]
        for con, first in ((make_db(bodies), 1), (make_db(bodies, titles), 2)):
            try:
                frm, where, params = ti.sql_parts(build_fts_query("예산"), "docs")
                ids = [r[0] for r in con.execute(f"SELECT t.id FROM {frm} WHERE {' AND '.join(where)} "
                                                 "ORDER BY docs_fts.rank, t.id", params)]
                self.assertEqual(sorted(ids), [1, 2, 3])
                self.assertEqual(ids[0], first, ids)
            finally:
                con.close()

    def test_title_triggers_keep_the_index_consistent(self):
        from rawlayer.search import common
        con = make_db(["회의 안건", "잡담"], ["회의 안건", "잡담"])
        try:
            common.create_triggers(con, "docs", "docs_fts", title=True)
            ng = ti.ng_text("예산 회의")
            con.execute("INSERT INTO docs(message, ng, tng) VALUES(?,?,?)", ("예산 회의", ng, ti.title_ng("예산", ng)))
            con.execute("UPDATE docs SET message='', ng='', tng='' WHERE id=1")
            con.execute("DELETE FROM docs WHERE id=2")
            con.execute("INSERT INTO docs_fts(docs_fts, rank) VALUES('integrity-check', 1)")
            self.assertEqual(run_query(con, build_fts_query("회의")), {3})
            self.assertEqual(run_query(con, build_fts_query("예산")), {3})
            self.assertIn("bm25(1.0, 3.0)", con.execute("SELECT v FROM docs_fts_config WHERE k='rank'").fetchone()[0])
        finally:
            con.close()

    def test_tokens_have_no_dropped_characters(self):
        for s in ["a,b", "가!나", "  x  ", "\U0001F600", "é가", "C++ 2.0"]:
            toks = ti.ng_tokens(s)
            self.assertTrue(all(tok == ti.SEP or tok.isalnum() for tok in toks), s)
            self.assertTrue(all(len(tok) <= 2 or tok.isascii() for tok in toks), s)

    def test_highlight(self):
        out = ti.highlight("오늘 송금을 했어요", ["송금"])
        self.assertEqual(out, "오늘 [송금]을 했어요")
        self.assertEqual(ti.highlight("", ["x"]), "")

    def test_highlight_finds_what_casefold_matching_found(self):
        """Review finding: matching casefolds (ß/SS, capital sharp s, ligatures) but the snippet only lowercased, so
        such hits were not marked and a long message's snippet could miss the match entirely."""
        self.assertEqual(ti.highlight("STRASSE 주소", ["straße"]), "[STRASSE] 주소")
        self.assertEqual(ti.highlight("Straße 주소", ["STRASSE"]), "[Straße] 주소")
        self.assertEqual(ti.highlight("ﬁle 업로드", ["file"]), "[ﬁle] 업로드")
        self.assertEqual(ti.highlight("ẞ 표기", ["ss"]), "[ẞ] 표기")          # capital sharp s
        far = ti.highlight("x" * 300 + " STRASSE 끝", ["straße"], width=40)
        self.assertTrue(far.startswith("…") and "[STRASSE] 끝" in far, far)
        self.assertEqual(ti.highlight("가나다 나다", ["나다", "가나다"]), "[가나다] [나다]")    # no nested marks
        for body, term in (("STRASSE 주소", "straße"), ("ﬁle 업로드", "file")):
            self.assertEqual(hits([body], term), {body})                  # and the search does find them


# --------------------------------------------------------------------------------------------
# Randomized property test: index + Query == truth
# --------------------------------------------------------------------------------------------

BODY_ALPHABET = (["가", "나", "다"] * 4 + ["ㅋ", "1", "0", "1", "a", "b", "C", "a",
                 "ß", "é", " ", " ", " ", "-", "+", ".", "!", "\U0001F600"])
TERM_ALPHABET = [c for c in BODY_ALPHABET if not c.isspace()]


def gen_body(rng):
    s = "".join(rng.choice(BODY_ALPHABET) for _ in range(rng.randint(0, 14)))
    if rng.random() < 0.15:
        s = unicodedata.normalize("NFD", s)
    return s


CLEAN_CHARS = set("가나다ㅋ10abCß") | {chr(c) for c in range(0x1100, 0x1200)}


def gen_term(rng, bodies, clean=False):
    """A random term: mostly a substring of some body, else random. clean: no D characters."""
    pool = [b for b in bodies if b.strip()]
    for _ in range(20):
        if pool and rng.random() < 0.75:
            b = rng.choice(pool)
            i = rng.randrange(len(b))
            s = b[i:i + rng.randint(1, 5)]
        else:
            s = "".join(rng.choice(TERM_ALPHABET) for _ in range(rng.randint(1, 4)))
        if clean:
            s = "".join(c for c in s if c in CLEAN_CHARS)
            if len(t_norm(s)) < 2:
                continue
            return s, False, rng.random() < 0.15
        quoted = False
        if any(c.isspace() for c in s):
            if rng.random() < 0.5:
                quoted = True
            else:
                parts = s.split()
                s = rng.choice(parts) if parts else ""
        elif rng.random() < 0.1:
            quoted = True
        if not s.strip():
            continue
        if not quoted and s.startswith("-"):
            quoted = True
        prefix = not quoted and rng.random() < 0.15
        return s, quoted, prefix
    return "가나", False, False


def gen_items(rng, bodies):
    """1-4 terms; some queries use OR (their terms are mostly clean, so most are not refused)."""
    use_or = rng.random() < 0.35
    items = []
    for k in range(rng.randint(1, 4)):
        text, quoted, prefix = gen_term(rng, bodies, clean=use_or and rng.random() < 0.95)
        if k > 0 and rng.random() < 0.25:
            items.append(("neg", "AND", text, quoted, prefix))
            continue
        op = "AND"
        if use_or and k > 0 and items[-1][0] == "pos" and rng.random() < 0.6:
            op = "OR"
        items.append(("pos", op, text, quoted, prefix))
    return items


class PropertyTest(unittest.TestCase):

    def test_index_agrees_with_truth(self):
        seed = int(os.environ.get("RAWSEARCH_PROP_SEED", "20261005"))
        rng = random.Random(seed)
        checked = refused = corpora = 0
        seen = dict.fromkeys(("or", "exclude", "literal", "prefix", "nonempty", "scan"), 0)
        while checked < 6000:
            corpora += 1
            self.assertLess(corpora, 2000, f"generator stalled (seed={seed})")
            bodies = [gen_body(rng) for _ in range(rng.randint(10, 40))]
            # every other corpus is a title index: tng (a prefix of the body, or another body) must not change a match
            con = make_db(bodies, [b[:rng.randint(0, len(b))] if rng.random() < 0.7 else rng.choice(bodies)
                                   for b in bodies] if corpora % 2 else None)
            try:
                for _ in range(25):
                    items = gen_items(rng, bodies)
                    query = render_query(items)
                    expect_refused = t_expect_refusal(items)
                    try:
                        q = build_fts_query(query)
                    except QueryError as e:
                        self.assertTrue(expect_refused,
                                        f"seed={seed} query={query!r} refused unexpectedly: {e}")
                        refused += 1
                        continue
                    self.assertFalse(expect_refused,
                                     f"seed={seed} query={query!r} should have been refused")
                    got = run_query(con, q)
                    want = {i + 1 for i, b in enumerate(bodies) if t_query_matches(b, items)}
                    if got != want:
                        diff = sorted(got ^ want)
                        self.fail(f"seed={seed} query={query!r} q={q!r} "
                                  f"extra={[bodies[i - 1] for i in diff if i in got]!r} "
                                  f"missing={[bodies[i - 1] for i in diff if i in want]!r}")
                    checked += 1
                    seen["or"] += any(op == "OR" for k, op, *_ in items if k == "pos")
                    seen["exclude"] += any(k == "neg" for k, *_ in items)
                    seen["literal"] += bool(q.verify or q.weak or q.exclude)
                    seen["prefix"] += any(p for *_, p in items)
                    seen["nonempty"] += bool(want)
                    seen["scan"] += not q.fts
            finally:
                con.close()
        self.assertGreaterEqual(checked, 5000)
        self.assertGreater(refused, 100, f"seed={seed}")
        for what, n in seen.items():  # keep the generator honest: every feature is exercised
            self.assertGreater(n, 400, f"seed={seed} only {n} cases with {what}")


if __name__ == "__main__":
    unittest.main()
