# -*- coding: utf-8 -*-
"""Korean query normalization: a user query rewritten into another query of the same language (textindex's), so
that what it matches is still defined by textindex's truth -- this module only changes the string. Standard library
only; it replaces a morphological analyser (the RAG tutorial's Kiwi) on the query side alone.

The index matches Korean as substrings (bigram phrases), so a term's stem matches every form that contains it:
cutting a particle or an ending off a term only widens what it finds, and nothing needs reindexing.

  rewrite(q)   each positive, unquoted, non-prefix, all-Hangul term loses at most one suffix (the longest of
               PARTICLES, HA, or PAST_TAIL after a syllable with the final ㅆ), when at least two syllables remain
               and the term is not PROTECTED; a stem ending in 하/해/했 becomes those three alternatives, and one
               ending in a ㅐ/ㅔ syllable gets its final ㅆ toggled as a second alternative (보내 <-> 보냈). The
               alternatives are written out in disjunctive form, because the language has no parentheses and AND
               binds tighter than OR ("송금 보냈" -> "송금 보냈 OR 송금 보내"); past MAX_GROUPS (counted before
               they are written out) the stems alone are used. Excluded terms are left as typed (widening them
               would hide more rows) and go last.
  relax(q)     the positive terms OR-ed (a zero-hit fallback), exclusions kept; a term that can only be matched
               literally (quoted, one character, punctuation) is dropped, since textindex refuses literal terms
               next to OR. None when that changes nothing: one term, or already all OR-ed.

Both return a query whose matches are a superset of the original's."""
import itertools
import math

from . import textindex as ti

PARTICLES = ("에서는", "으로는", "에게서", "에서", "에게", "한테", "으로", "까지", "부터", "보다", "처럼", "이랑", "하고",
             "에는", "에도", "이나", "은", "는", "이", "가", "을", "를", "에", "의", "도", "만", "와", "과", "로", "랑")
# particles that follow only a syllable with a final consonant (C) or only one without (V; 로 also after ㄹ):
# "플레이" keeps its 이 (플레 ends in a vowel), "기능이" loses it
AGREE = {"이": "C", "은": "C", "을": "C", "과": "C", "으로": "C", "으로는": "C", "이랑": "C", "이나": "C",
         "가": "V", "는": "V", "를": "V", "와": "V", "랑": "V", "로": "V"}
HA = ("했습니다", "했는데", "했어요", "합니다", "했어", "했다", "했고", "하는", "하고", "하다", "하면", "해서", "해요",
      "했", "해", "한", "할", "함")
PAST_TAIL = ("습니다", "어요", "는데", "어", "다", "고")      # only after a syllable whose final is ㅆ (보냈다)
PROTECTED = frozenset(("평가", "회의", "관리자", "전문가", "고양이", "어린이", "원숭이", "만족도"))
MAX_GROUPS = 8

_SSANG_SIOT = 20                # the final ㅆ's index in a Hangul syllable
_RIEUL = 8                      # the final ㄹ's
_AE_E = (1, 5)                  # the medials ㅐ and ㅔ
# (suffix, rule) longest first; rule: "SS" after a final ㅆ, "C"/"V" as AGREE, "" none (a particle may also be one of
# HA's endings, 하고: the particle's rule wins only when it has one)
_SUFFIXES = sorted({(s, AGREE.get(s, "")) for s in PARTICLES + HA} | {(s, "SS") for s in PAST_TAIL},
                   key=lambda x: (-len(x[0]), x[1]))


def _hangul(text):
    return bool(text) and all("가" <= c <= "힣" for c in text)


def _final(syl):
    return (ord(syl) - 0xAC00) % 28


def _medial(syl):
    return (ord(syl) - 0xAC00) // 28 % 21


def stem(term):
    """`term` without its longest suffix, when at least two syllables remain; else `term`."""
    if term in PROTECTED:
        return term
    for suf, rule in _SUFFIXES:
        base = term[:-len(suf)]
        if not term.endswith(suf) or len(base) < 2:
            continue
        fin = _final(base[-1])
        if (rule == "SS" and fin != _SSANG_SIOT) or (rule == "C" and fin == 0) or \
                (rule == "V" and fin not in (0, _RIEUL if suf == "로" else 0)):
            continue
        return base
    return term


def forms(term):
    """The alternatives a stem stands for: 하/해/했 at its end, or the final ㅆ toggled on a ㅐ/ㅔ syllable."""
    last = term[-1]
    if last in "하해했" and len(term) >= 2:
        return [term[:-1] + x for x in "하해했"]
    if _medial(last) in _AE_E and _final(last) in (0, _SSANG_SIOT):
        toggled = chr(ord(last) + (_SSANG_SIOT if _final(last) == 0 else -_SSANG_SIOT))
        return [term, term[:-1] + toggled]
    return [term]


def _show(op, is_phrase, text):
    s = f'"{text}"' if is_phrase else text
    return "-" + s if op == "NOT" else s


def _parts(query):
    """([[(is_phrase, text), ...] per OR-group], [excluded term strings]) of a query."""
    groups, excluded = [[]], []
    for op, is_phrase, text in ti._split_terms(query):
        if op == "NOT":
            excluded.append(_show(op, is_phrase, text))
            continue
        if op == "OR" and groups[-1]:
            groups.append([])
        groups[-1].append((is_phrase, text))
    return [g for g in groups if g], excluded


def _join(groups, excluded):
    return " ".join([" OR ".join(" ".join(g) for g in groups)] + excluded).strip()


def canonical(query):
    """`query` re-serialized as rewrite() writes it, nothing changed: rewrite(q) != canonical(q) iff it rewrote."""
    groups, excluded = _parts(query)
    return _join([[_show("AND", p, t) for p, t in g] for g in groups], excluded)


def rewrite(query, expand=True):
    groups, excluded = _parts(query)

    def alts(is_phrase, text, full):
        if is_phrase or not _hangul(text):
            return [_show("AND", is_phrase, text)]
        s = stem(text)
        return forms(s) if full else [s]

    for full in ((True, False) if expand else (False,)):
        opts = [[alts(p, t, full) for p, t in g] for g in groups]
        # counted before the product is built: a long query's product would not fit in memory
        if full and sum(math.prod(map(len, g)) for g in opts) > MAX_GROUPS:
            continue
        return _join([list(c) for g in opts for c in itertools.product(*g)], excluded)
    return query                # unreachable: the stems-only pass always returns


def relax(query):
    groups, excluded = _parts(query)
    terms, dropped = [], False
    for g in groups:
        for is_phrase, text in g:
            bare = text[:-1] if not is_phrase and text.endswith("*") else text     # a prefix term is not literal
            if is_phrase or ti._loses_characters(bare) or ti.is_weak_term(bare):
                dropped = True
            elif text not in terms:
                terms.append(text)
    if not terms or (not dropped and (len(terms) < 2 or all(len({t for _, t in g}) == 1 for g in groups))):
        return None             # nothing left, one term, or already all OR-ed
    return _join([[t] for t in terms], excluded)
