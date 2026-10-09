"""Tokenizer and query compiler shared by the search indexer and the search query side.

Ported from the knowledge-base corpus engine (kbtext.py, tokenizer "ngram-1") with two match bugs
fixed. Both sides import this module, so the document side and the query side can never drift
apart. Standard library only.

WHY A BIGRAM SHADOW COLUMN
--------------------------
SQLite FTS5's unicode61 tokenizer treats a whole Korean eojeol (stem + particle) as one token, so
"송금을" and "송금" are unrelated, and the trigram tokenizer refuses 2-syllable queries. Instead every
message gets a pre-tokenised shadow column `ng`, indexed by an FTS5 external-content table with
tokenize='ascii' (see fts_ddl()). In `ng`, each run of Korean/CJK characters becomes its
overlapping bigrams, each Latin/digit run becomes one lowercase token:

    "오늘 송금을 했어요"   ->  "오늘 ⟨S⟩ 송금 금을 ⟨S⟩ 했어 어요"

and a query term is turned into an FTS5 *phrase* over the same tokens.

THE TRUTH (what a search must return -- the tests compare the index against exactly this)
-----------------------------------------------------------------------------------------
norm(s) = NFC(casefold(NFC(s))). Every character of a normalised string is one of
  N  Hangul syllables/jamo, CJK ideographs, hiragana, katakana (the _NGRAM_RANGES below),
  L  ASCII [0-9a-z]  (after casefold, so "OAuth" == "oauth"; "ß" folds to "ss", which is L),
  D  anything else: whitespace, punctuation, symbols, emoji, other scripts (é, Cyrillic, ...).

A query is a list of terms: whitespace-separated words or "quoted phrases"; OR between two terms;
a leading `-` excludes a term; a trailing `*` on an unquoted word asks for a prefix. For a term
with text T (the `*` removed) let t = norm(T). A term is LITERAL when it is quoted, or t contains
a D character, or t's only N/L character is a single N character (a "weak" term). Then:

  * a literal term matches message m  iff  t is a substring of norm(m);
  * any other term (t is two or more N/L characters, no D) matches m iff some occurrence
    norm(m)[i:j] == t satisfies
        - if t[0] is L:                       i == 0 or norm(m)[i-1] is not L, and
        - if t[-1] is L and there is no `*`:  j == len(norm(m)) or norm(m)[j] is not L.

So CJK parts match as substrings ("송금" finds "송금을" and "입금송금"; "송금을" narrows), a Latin/digit
run at a term edge matches a whole run ("api" finds "API 키" but not "rapid" or "apis"; "api*"
finds "apis"; "10월" finds "10월에" but not "210월"), and runs inside a term are fixed by the term
itself. Lossy terms ("C++", "AES-256") and quoted phrases match literally ("C++" finds "vc++").

Positive terms combine left to right with AND (the default) and OR, AND binding tighter than OR
(as in FTS5). The result is that combination AND NOT (any excluded term matches), excluded terms
using the same per-term rule as positive ones.

Refused with QueryError: an empty query; a query without a positive term; a first term that is
excluded; OR next to an excluded term; an unquoted `name:` term (looks like an FTS column filter;
"://" URLs are fine); a term with no N/L character at all; any literal term (positive or
excluded) in a query that uses OR -- a literal term cannot be OR-ed inside the FTS expression
without returning a wrong count; and a non-literal term with an L run longer than MAX_TERM_RUN
characters. SQLite FTS5 silently cuts every token at 32768 bytes (FTS5_MAX_TOKEN_SIZE), in the
index and in the query alike, so such a run would also match a longer run that merely starts with
it (prefix terms included). Terms with shorter runs are exact whatever the documents hold, and
literal terms are unaffected (a cut token is still a necessary condition, and rs_contains decides).
The Windows command line (32767 characters) cannot carry such a query anyway.

HOW THE INDEX AGREES WITH THE TRUTH (tokenizer "ngram-2")
---------------------------------------------------------
Document tokens (ng_tokens), for the maximal N/L runs of norm(text) in order:
  * an L run -> one token, the run itself;
  * an N run c1..cn -> its bigrams c1c2 .. c(n-1)cn, plus a single-character HEAD token c1 when an
    L run touches it on the left and a TAIL token cn when an L run touches it on the right
    ("10월에" -> "10 월 월에"; "SQL문을" -> "sql 문 문을"; "가a" -> "가 a");
  * SEP (U+E000, a private-use character the 'ascii' tokenizer treats as a token character) between
    two runs that dropped (D) characters separated, so runs that were apart never line up as a
    phrase: "가나 나다" -> "가나 ⟨S⟩ 나다", which the query "가나다" (phrase "가나 나다") no longer
    matches -- ngram-1 emitted "가나 나다" and returned it.
A non-literal term is tokenised by the very same rule and issued as one phrase; edge runs of the
term simply get no HEAD/TAIL token, which is what makes their outer side "open". Head/tail tokens
fix the ngram-1 miss where "10월" emitted the lone token "월" while "10월에" held only "월에".
The phrase is exact: no literal check is needed for these terms.

A literal term is checked with rs_contains() (see register_functions()) and, when possible,
narrowed through the index by a phrase that is a necessary condition: the term tokenised the same
way, minus the token of a Latin run that is open on the left, with a prefix `*` on a Latin run
that is open on the right.
"""

import re
import unicodedata
from typing import NamedTuple

# Bump TOKENIZER_VERSION when tokenization changes (an index built by another version must be
# rebuilt, never queried), SCHEMA_VERSION when the index tables change. The index code stamps both
# into its meta table and the search code refuses an index whose stamps differ.
TOKENIZER_VERSION = "ngram-2"
SCHEMA_VERSION = "1"
FTS_TOKENIZE = "ascii"

# Separator token between runs that dropped characters kept apart. A private-use character: never
# produced by the N/L classes, never part of any other token, a token character for 'ascii'.
SEP = chr(0xE000)

# Name of the SQL function register_functions() installs: rs_contains(text, needle) -> 0/1.
CONTAINS_FN = "rs_contains"

# Scripts written without spaces between words -> expand into bigrams.
_NGRAM_RANGES = (
    (0xAC00, 0xD7A3),  # Hangul syllables
    (0x1100, 0x11FF),  # Hangul jamo
    (0x3130, 0x318F),  # Hangul compatibility jamo
    (0xA960, 0xA97F),  # Hangul jamo extended-A
    (0xD7B0, 0xD7FF),  # Hangul jamo extended-B
    (0x4E00, 0x9FFF),  # CJK unified ideographs
    (0x3400, 0x4DBF),  # CJK ext A
    (0xF900, 0xFAFF),  # CJK compatibility ideographs
    (0x3040, 0x309F),  # Hiragana
    (0x30A0, 0x30FF),  # Katakana
)
_N_CLASS = "".join(f"\\u{lo:04x}-\\u{hi:04x}" for lo, hi in _NGRAM_RANGES)
_RUN_RE = re.compile(f"([{_N_CLASS}]+)|([0-9a-z]+)")
_N_CHAR = re.compile(f"[{_N_CLASS}]")
_LATIN = frozenset("0123456789abcdefghijklmnopqrstuvwxyz")
_COLUMN_FILTER = re.compile(r"^[A-Za-z_]+:")
# Longest Latin/digit run a non-literal term may carry: below FTS5's 32768-byte token cut (see the docstring).
MAX_TERM_RUN = 32000


def norm(text: str) -> str:
    """NFC + casefold (+ NFC again): the form every comparison in this module works on."""
    if not text:
        return ""
    if text.isascii():
        return text.lower()
    return unicodedata.normalize("NFC", unicodedata.normalize("NFC", text).casefold())


def _is_ngram_char(ch: str) -> bool:
    return _N_CHAR.match(ch) is not None


def _kind(ch: str):
    """'N', 'L' or None (dropped) for one character of an already normalised string."""
    if ch in _LATIN:
        return "L"
    if _N_CHAR.match(ch):
        return "N"
    return None


def _runs(normed: str) -> list:
    """Maximal runs of a normalised string as (kind, run, gap_before) triples.

    gap_before is True when dropped characters separate this run from the previous run; it is
    always False for the first run (dropped characters before it separate it from nothing).
    """
    out = []
    end = 0
    for m in _RUN_RE.finditer(normed):
        kind = "N" if m.group(1) else "L"
        out.append((kind, m.group(0), bool(out) and m.start() > end))
        end = m.end()
    return out


def _tokens_of(runs: list) -> list:
    """Token stream for a run list (the ng_tokens rule; see the module docstring)."""
    toks = []
    last = len(runs) - 1
    for idx, (kind, run, gap) in enumerate(runs):
        if gap:
            toks.append(SEP)
        if kind == "L":
            toks.append(run)
            continue
        if idx > 0 and not gap and runs[idx - 1][0] == "L":
            toks.append(run[0])  # HEAD: a Latin/digit run touches this run on the left
        toks.extend(run[i:i + 2] for i in range(len(run) - 1))
        if idx < last and not runs[idx + 1][2] and runs[idx + 1][0] == "L":
            toks.append(run[-1])  # TAIL: a Latin/digit run touches this run on the right
    return toks


def ng_tokens(text: str) -> list:
    """Tokens for the n-gram index (CJK bigrams + head/tail, Latin runs, SEP between gaps)."""
    if not text:
        return []
    return _tokens_of(_runs(norm(text)))


def ng_text(text: str) -> str:
    """The string stored in the `ng` shadow column."""
    return " ".join(ng_tokens(text))


def is_weak_term(term: str) -> bool:
    """True if `term`'s only indexable character is a single CJK character.

    Such a term has no bigram, so the index cannot narrow it; callers scan with rs_contains().
    """
    sig = [c for c in norm(term) if _kind(c)]
    return len(sig) == 1 and sig[0] not in _LATIN


def _loses_characters(term: str) -> bool:
    """True if tokenising this term drops characters, so it can only be matched literally.

    "C++" tokenises to just "c", which would match every standalone "c" -- and miss "vc++".
    """
    return any(not ch.isspace() and _kind(ch) is None for ch in norm(term))


class QueryError(ValueError):
    """Raised for a user query that cannot be translated into an FTS5 expression."""


def _split_terms(query: str) -> list:
    """Split a user query into (op, is_phrase, text) terms, op in AND / OR / NOT.

    Supported surface syntax, deliberately small:
      바나나 우유        two required terms (AND)
      "정기 회의"        quoted phrase, matched literally
      회의 OR 미팅       alternation
      회의 -취소         exclude the second term
      api*               prefix (an unquoted word ending in *)
    """
    terms = []
    pending_op = "AND"
    i = 0
    n = len(query)
    while i < n:
        ch = query[i]
        if ch.isspace():
            i += 1
            continue
        neg = False
        if ch == "-" and i + 1 < n and not query[i + 1].isspace():
            neg = True
            i += 1
        if i < n and query[i] == '"':
            j = query.find('"', i + 1)
            if j == -1:
                raise QueryError('unbalanced quote in query -- close the " or drop it')
            text, is_phrase = query[i + 1:j], True
            i = j + 1
        else:
            j = i
            while j < n and not query[j].isspace():
                j += 1
            text, is_phrase = query[i:j], False
            i = j
        if not neg and not is_phrase and text == "OR":
            if terms:
                if terms[-1][0] == "NOT":
                    raise QueryError("OR cannot follow an excluded (-) term -- exclusions apply "
                                     "to the whole query; put them at the end")
                pending_op = "OR"
            continue
        if not text.strip():
            continue
        if neg and pending_op == "OR":
            raise QueryError("OR cannot precede an excluded (-) term -- exclusions apply to the "
                             "whole query; put them at the end")
        terms.append(("NOT" if neg else pending_op, is_phrase, text))
        pending_op = "AND"
    return terms


class Query(NamedTuple):
    """A translated query. Apply it with sql_parts() (or exactly as sql_parts() does).

    fts          FTS5 expression over the n-gram index: exact for ordinary terms, a necessary
                 condition for literal ones. Empty means nothing can narrow -- the caller scans.
    verify       Normalised literal substrings every hit must contain (rs_contains(text, ?)).
    exclude      Normalised literal substrings no hit may contain (NOT rs_contains(text, ?)).
    weak         Normalised single-CJK-character terms: checked like verify, listed separately so
                 the caller can warn that they force a scan.
    marks        Positive terms as typed, for snippet highlighting.
    exclude_fts  FTS5 expression for excluded non-literal terms; a hit's rowid must not be in
                 (SELECT rowid FROM <fts> WHERE <fts> MATCH exclude_fts). Empty when none.
    """
    fts: str
    verify: list
    exclude: list
    weak: list
    marks: list
    exclude_fts: str = ""


def _phrase(toks: list, prefix: bool = False) -> str:
    return '"' + " ".join(toks) + '"' + ("*" if prefix else "")


def _literal_phrase(t: str) -> str:
    """A necessary-condition phrase for the literal (normalised) text t, or '' if none.

    If norm(m) contains t, the runs of t that are closed on both sides are runs of m as well, so
    t's tokens appear consecutively in m's token stream -- except the token of a Latin run open on
    the left (m's run may be longer on that side: dropped), and that of a Latin run open on the
    right (m's run may continue: prefix). CJK edge runs need nothing special: their bigrams are
    bigrams of m's run, and head/tail tokens only arise between two runs of t.
    """
    runs = _runs(t)
    if not runs:
        return ""
    toks = _tokens_of(runs)
    prefix = _kind(t[-1]) is not None and runs[-1][0] == "L"
    if _kind(t[0]) is not None and runs[0][0] == "L":
        toks = toks[1:]
        if len(runs) == 1:
            prefix = False
    if all(tok == SEP for tok in toks):
        return ""
    return _phrase(toks, prefix)


def build_fts_query(query: str) -> Query:
    """Translate a user query into an index expression plus literal checks (see module docs)."""
    terms = _split_terms(query)
    if not terms:
        raise QueryError("empty query")
    if terms[0][0] == "NOT":
        raise QueryError("a query cannot start with an excluded (-) term")
    has_or = any(op == "OR" for op, _, _ in terms)
    groups = [[]]       # OR-separated groups of AND-ed exact phrases
    narrow = []         # necessary-condition phrases of literal terms (never with OR)
    verify, exclude, weak, marks, exclude_phrases = [], [], [], [], []
    for op, is_phrase, text in terms:
        if not is_phrase and _COLUMN_FILTER.match(text) and "://" not in text:
            col = text.split(":", 1)[0]
            raise QueryError(
                f"{text!r} looks like an FTS column filter, which this query language does not "
                f"support and which would silently match nothing. Filter with the flags instead: "
                f"--{'sender' if col == 'sender' else 'room / --channel / --sender'}.")
        # A trailing '*' asks for prefix matching on a trailing Latin run ("api*" -> apis).
        # Korean rarely needs it: bigrams already match substrings.
        prefix = not is_phrase and text.endswith("*")
        if prefix:
            text = text[:-1]
        t = norm(text)
        if not any(_kind(c) for c in t):
            raise QueryError(f"term {text!r} contains no indexable characters")
        literal = is_phrase or _loses_characters(text) or is_weak_term(text)
        if not literal and any(kind == "L" and len(run) > MAX_TERM_RUN for kind, run, _gap in _runs(t)):
            raise QueryError(f"a Latin/digit run longer than {MAX_TERM_RUN} characters cannot be matched "
                             f"exactly (SQLite FTS5 cuts longer tokens) -- search for a shorter part of it")
        if literal and has_or:
            raise QueryError(
                f"term {text!r} can only be matched literally, and this query uses OR. "
                f"Literal terms cannot be combined with OR without returning a wrong count -- "
                f"run the alternatives as separate searches.")
        if literal:
            if op == "NOT":
                exclude.append(t)
                continue
            marks.append(text)
            (weak if is_weak_term(text) else verify).append(t)
            ph = _literal_phrase(t)
            if ph and ph not in narrow:
                narrow.append(ph)
            continue
        ph = _phrase(ng_tokens(t), prefix and t[-1] in _LATIN)
        if op == "NOT":
            exclude_phrases.append(ph)
            continue
        marks.append(text)
        if op == "OR" and groups[-1]:
            groups.append([])
        groups[-1].append(ph)
    groups = [g for g in groups if g]
    if not groups and not verify and not weak:
        raise QueryError("query has no positive terms")
    if len(groups) > 1:
        fts = " OR ".join(g[0] if len(g) == 1 else "(" + " AND ".join(g) + ")" for g in groups)
    else:
        fts = " AND ".join((groups[0] if groups else []) + narrow)
    return Query(fts, verify, exclude, weak, marks, " OR ".join(exclude_phrases))


def like_pattern(term: str) -> str:
    """A LIKE '%term%' pattern with the wildcards in `term` escaped (pair with ESCAPE '\\').

    For metadata filters (room, sender, channel). Message text is matched with rs_contains().
    """
    for ch in ("\\", "%", "_"):
        term = term.replace(ch, "\\" + ch)
    return f"%{term}%"


def contains(text, needle) -> int:
    """rs_contains(text, needle): 1 if the (already normalised) needle occurs in norm(text)."""
    if text is None or needle is None:
        return 0
    return 1 if needle in norm(text) else 0


def register_functions(con) -> None:
    """Install rs_contains() on a sqlite3 connection. Required before running sql_parts() SQL."""
    con.create_function(CONTAINS_FN, 2, contains, deterministic=True)


def fts_ddl(content_table: str, fts_table: str = "", rowid_col: str = "id") -> str:
    """CREATE statement for the external-content FTS5 index over `content_table`.ng."""
    fts_table = fts_table or content_table + "_fts"
    return (f"CREATE VIRTUAL TABLE {fts_table} USING fts5(ng, content='{content_table}', "
            f"content_rowid='{rowid_col}', tokenize='{FTS_TOKENIZE}')")


def sql_parts(q: Query, content_table: str, fts_table: str = "", *, alias: str = "t",
              rowid_col: str = "id", text_col: str = "message"):
    """(from_sql, where_list, params) selecting exactly the rows that match `q`.

    from_sql drives from the FTS table when q.fts is set (so ORDER BY bm25(<fts_table>) works),
    else it is the bare content table; either way the content table is aliased `alias`. The caller
    appends its own filters to where_list/params and runs
        SELECT ... FROM {from_sql} WHERE {' AND '.join(where_list) or '1'}
    on a connection that went through register_functions().
    """
    fts_table = fts_table or content_table + "_fts"
    t = alias
    where, params = [], []
    if q.fts:
        frm = f"{fts_table} JOIN {content_table} {t} ON {t}.{rowid_col} = {fts_table}.rowid"
        where.append(f"{fts_table} MATCH ?")
        params.append(q.fts)
    else:
        frm = f"{content_table} {t}"
    for needle in list(q.verify) + list(q.weak):
        where.append(f"{CONTAINS_FN}({t}.{text_col}, ?)")
        params.append(needle)
    for needle in q.exclude:
        where.append(f"NOT {CONTAINS_FN}({t}.{text_col}, ?)")
        params.append(needle)
    if q.exclude_fts:
        where.append(f"{t}.{rowid_col} NOT IN "
                     f"(SELECT rowid FROM {fts_table} WHERE {fts_table} MATCH ?)")
        params.append(q.exclude_fts)
    return frm, where, params


def _fold_map(s: str):
    """(casefold(s), owner) where owner[k] is the index in s of the character that produced folded
    character k ("ß" -> "ss" gives two folded characters owned by one original). Matching uses
    casefold (norm()), so snippets must find what the search found: "STRASSE" for "straße"."""
    if s.isascii():
        return s.lower(), range(len(s))
    out, owner = [], []
    for i, c in enumerate(s):
        f = c.casefold()
        out.append(f)
        owner.extend([i] * len(f))
    return "".join(out), owner


def highlight(message: str, phrases, width: int = 140, mark=("[", "]")) -> str:
    """Build a snippet around the first matched phrase, marking every match.

    Rendered from the original message rather than FTS5's snippet(), which would return the
    bigram-mangled shadow column. Message and phrases are NFC-normalised first and compared
    casefolded, through a map back onto the original characters. Longer phrases are marked first;
    a match overlapping one already marked is left unmarked (no nested marks).
    """
    flat = " ".join(unicodedata.normalize("NFC", message or "").split())
    if not flat:
        return ""
    phrases = [unicodedata.normalize("NFC", p) for p in (phrases or []) if p]
    folded, owner = _fold_map(flat)
    spans = []
    for p in sorted(set(phrases), key=len, reverse=True):
        fp = _fold_map(p)[0]
        if not fp:
            continue
        k = folded.find(fp)
        while k >= 0:
            a, b = owner[k], owner[k + len(fp) - 1] + 1
            if all(b <= x or a >= y for x, y in spans):
                spans.append((a, b))
            k = folded.find(fp, k + len(fp))
    lo, hi = 0, min(len(flat), width)
    if spans:
        lo = max(0, min(a for a, _b in spans) - width // 3)
        hi = min(len(flat), lo + width)
    out, pos = [], lo
    for a, b in sorted(sp for sp in spans if sp[0] >= lo and sp[1] <= hi):
        out += [flat[pos:a], mark[0], flat[a:b], mark[1]]
        pos = b
    out.append(flat[pos:hi])
    return ("…" if lo > 0 else "") + "".join(out) + ("…" if hi < len(flat) else "")
