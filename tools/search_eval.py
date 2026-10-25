# -*- coding: utf-8 -*-
"""python -s -B <PROJECT>/tools/search_eval.py generate <app> [--n 100] [--seed 42] [--pool 200000]
       python -s -B <PROJECT>/tools/search_eval.py paraphrase <app> [--n 30] [--model qwen3:4b-instruct]
       python -s -B <PROJECT>/tools/search_eval.py run <app> [--modes default,literal,hybrid] [--label TEXT]

Retrieval quality of `manage.py search` on an app's own ledger: Hit@k, Recall@k and MRR for k = 1, 3, 5, 10, with
latency p50/p95 (per call, Python start-up included). A developer tool, not a manage.py command; standard library
only. Every search runs as `manage.py search <app> --json` through tools/mcp_rawsearch.py's Runner, on the app's
default config.

The eval set is <data_root>/eval/search.json: {app, seed, items: [{id, source, kind, query, gold: [key, ...]}]}.
A gold is a hit's `key` (stable across index rebuilds; row ids are not). `generate` writes the auto items from one
browse of the newest --pool rows:
  title      gmail subjects and notion page titles, gold = every row with that title (they favour keyword matching);
  inflected  1-2 words of one message, the last a bare Hangul word given a particle as a user would type it
             ("일정" -> "일정을"), kept only when the words as written find exactly that one row: synthetic, it
             measures what query normalization undoes;
  phrase     2-3 consecutive words of one message, kept only when the search finds exactly that one row.
Items whose source is not "auto" (hand-written: {"source": "manual", "kind": "manual", "query": ..., "gold": [...]})
are kept as they are. `paraphrase` replaces the "paraphrase" items: a local generative model (Ollama on a loopback
IP, as the embedder) writes a question about a random row in other words, kept when it is short and few of its
bigrams are the row's (PARA_MAX_*; gold = that row): the case semantic search is for. `run` searches every item once per mode and prints
aggregates only, by mode and by kind, and appends them to <data_root>/eval/results.jsonl; mode hybrid also scores
the fused list with each hit's chunk rows (window) and the keyword and semantic lists alone. No query, text or error
message is ever printed: they are private.

Exit codes: 0 ok | 2 usage or no eval set | 1 error."""
import argparse
import datetime
import json
import os
import random
import re
import sys
import time
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
if str(PROJECT) not in sys.path:
    sys.path.insert(0, str(PROJECT))

from tools import mcp_rawsearch as M  # noqa: E402

KS = (1, 3, 5, 10)
TIMEOUT = 120
# a word the query language reads as a plain term: no quotes, exclusion, OR, `name:` or prefix `*`
WORD = re.compile(r"[0-9A-Za-z가-힣]{2,}")
EDGE = re.compile(r"^[^0-9A-Za-z가-힣]+|[^0-9A-Za-z가-힣]+$")
# extra arguments per mode
MODE_ARGS = {"default": [], "literal": ["--literal"], "hybrid": ["--hybrid"]}
HYBRID_LIMIT = 60                    # >= dense.KW_N + dense.SEM_N: every fused unit
LLM_MODEL, LLM_URL = "qwen3:4b-instruct", "http://127.0.0.1:11434"
LLM_PROMPT = ("검색 평가용 질문을 만든다. 아래 글을 나중에 다시 찾으려는 사람이 검색창에 입력할, 이 글이 답이 되는 "
              "질문을 한국어로 하나 써라. 조건: 15단어 이하의 짧은 질문. 글을 옮겨 쓰지 말고 무엇에 관한 글인지 묻는다. "
              "글에 나온 단어는 되도록 쓰지 말고 다른 말로 바꾼다. 질문 한 줄만 출력한다.\n\n글:\n")
PARA_MAX_LEN, PARA_MAX_SHARED = 60, 0.35     # a paraphrase: short, and at most this share of its bigrams in the row


class EvalError(Exception):
    """A refusal: exit 2."""


def words(text):
    """The plain words of `text`, punctuation cut from their edges, in order."""
    out = []
    for w in text.split():
        w = EDGE.sub("", w)
        if WORD.fullmatch(w) and w.upper() != "OR":
            out.append(w)
    return out


def title_query(title):
    q = " ".join(words(title or ""))
    return q if len(q) >= 6 else None


def phrase_windows(text):
    """Every run of 2 or 3 consecutive plain words of `text` (whitespace-separated, nothing cut in between)."""
    toks = (text or "").split()
    out = []
    for n in (2, 3):
        for i in range(len(toks) - n + 1):
            win = toks[i:i + n]
            if all(WORD.fullmatch(w) and w.upper() != "OR" for w in win):
                out.append(" ".join(win))
    return out


def inflectable(text):
    """(window, word) pairs of `text`: a run of 1-2 plain words ending in a bare Hangul word of 2-4 syllables (one
    normalize.stem leaves as it is), which inflect() turns into a query the text does not hold as typed."""
    from rawlayer.search import normalize  # noqa: PLC0415
    toks = (text or "").split()
    out = []
    for i, w in enumerate(toks):
        if not (re.fullmatch("[가-힣]{2,4}", w) and normalize.stem(w) == w):
            continue
        out.append((w, w))
        if i and WORD.fullmatch(toks[i - 1]) and toks[i - 1].upper() != "OR":
            out.append((f"{toks[i - 1]} {w}", w))
    return out


def inflect(window, word, rng):
    """`window` with a particle on its last word, as a user would type it (을/를 by the final consonant)."""
    final = (ord(word[-1]) - 0xAC00) % 28
    return window + rng.choice(("을", "이", "은", "에서", "으로") if final else ("를", "가", "는", "에서", "로"))


def _keys(x):
    return x if isinstance(x, frozenset) else {x}


def score(ranking, gold, ks=KS):
    """{hit@k, recall@k, rr} of one ranked key list against the gold keys (rr: 1/rank of the first gold in the top
    max(ks), else 0). An element may be a frozenset of keys (a hit with its chunk's rows): it holds them all."""
    gold = set(gold)
    out = {}
    for k in ks:
        top = set().union(*(_keys(x) for x in ranking[:k]))
        out[f"hit@{k}"] = 1.0 if top & gold else 0.0
        out[f"recall@{k}"] = len(top & gold) / len(gold) if gold else 0.0
    out["rr"] = next((1.0 / (i + 1) for i, x in enumerate(ranking[:max(ks)]) if _keys(x) & gold), 0.0)
    return out


def hybrid_rankings(rows):
    """What one --hybrid result says about its retrievers: the fused order with each hit's chunk rows (window), and
    the keyword and semantic lists alone (by their `sources` ranks), as keys and as windows."""
    def window(r):
        return frozenset([r.get("key")] + [c.get("key") for c in r.get("context") or []])
    out = {"fused_window": [window(r) for r in rows]}
    for src in ("kw", "sem"):
        mine = sorted((r for r in rows if (r.get("sources") or {}).get(src)), key=lambda r: r["sources"][src])
        out[f"{src}_only"] = [r.get("key") for r in mine]
        out[f"{src}_window"] = [window(r) for r in mine]
    return out


def pct(values, p):
    if not values:
        return None
    s = sorted(values)
    return round(s[min(len(s) - 1, int(round(p / 100 * (len(s) - 1))))], 1)


def summarize(results):
    """Averages of [{scores..., ms, error, zero}] (errors count as misses)."""
    n = len(results)
    if not n:
        return {"n": 0}
    out = {"n": n, "errors": sum(r["error"] for r in results), "zero_hit": sum(r["zero"] for r in results)}
    for k in KS:
        for m in ("hit", "recall"):
            out[f"{m}@{k}"] = round(sum(r[f"{m}@{k}"] for r in results) / n, 4)
    out["mrr"] = round(sum(r["rr"] for r in results) / n, 4)
    ms = [r["ms"] for r in results if not r["error"]]
    out["p50_ms"], out["p95_ms"] = pct(ms, 50), pct(ms, 95)
    return out


def merge_items(old, auto):
    """The new auto items, then every item of `old` that is not auto (hand-written ones survive a regeneration)."""
    kept = [it for it in old if it.get("source") != "auto"]
    return auto + kept


# ---------------------------------------------------------------------------------------------------- the store

def data_root(app):
    from rawlayer import registry  # noqa: PLC0415
    from rawlayer.management.base import default_config  # noqa: PLC0415
    from rawlayer.search import common  # noqa: PLC0415
    try:
        registry.select(app)
        return common.load_data_root(default_config(app))
    except (common.UsageError, registry.RegistryError) as e:
        raise EvalError(f"{app}: {e}") from None


def write_json(path, obj):
    path.parent.mkdir(exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def load_set(path):
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except ValueError:
        raise EvalError("eval set unreadable (not JSON)") from None
    items = d.get("items") if isinstance(d, dict) else None
    if not isinstance(items, list) or not all(isinstance(it, dict) and isinstance(it.get("query"), str)
                                              and isinstance(it.get("gold"), list) for it in items):
        raise EvalError("eval set: items must be [{query, gold: [key, ...]}, ...]")
    return d


# ---------------------------------------------------------------------------------------------------- commands

def search(runner, app, argv):
    try:
        return M.search(runner, "eval", app, argv)[0]
    except M.ToolError:
        return None


def generate(app, n, seed, pool, runner):
    root = data_root(app)
    env = search(runner, app, ["--since=1970-01-01", f"--limit={pool}", "--width=20"])
    if env is None:
        raise EvalError(f"{app}: the browse failed (is there an index?)")
    rows = [r for r in env.get("rows", []) if isinstance(r, dict) and r.get("key")]
    rng = random.Random(seed)
    auto = []

    titles = {}
    for r in rows:
        t = r.get("subject") if "subject" in r else (r.get("page_title") if r.get("kind") == "page" else None)
        q = title_query(t)
        if q:
            titles.setdefault(q, []).append(r["key"])
    tq = sorted(titles)
    rng.shuffle(tq)
    for q in tq[:n // 2]:
        auto.append({"source": "auto", "kind": "title", "query": q, "gold": titles[q]})

    texts = [r for r in rows if isinstance(r.get("text"), str)]
    seen = {it["query"] for it in auto}
    tries = 0
    want = len(auto) + (n - len(auto)) // 2
    while texts and len(auto) < want and tries < n * 8:
        tries += 1
        r = rng.choice(texts)
        cands = inflectable(r["text"])
        if not cands:
            continue
        window, word = rng.choice(cands)
        q = inflect(window, word, rng)
        if q in seen:
            continue
        seen.add(q)
        c = search(runner, app, ["--mode=count", "--literal", f"--query={window}"])
        if c is not None and c.get("total") == 1:
            auto.append({"source": "auto", "kind": "inflected", "query": q, "gold": [r["key"]]})

    tries = 0
    while texts and len(auto) < n and tries < n * 8:
        tries += 1
        r = rng.choice(texts)
        wins = phrase_windows(r["text"])
        if not wins:
            continue
        q = rng.choice(wins)
        if q in seen:
            continue
        seen.add(q)
        c = search(runner, app, ["--mode=count", f"--query={q}"])
        if c is not None and c.get("total") == 1:
            auto.append({"source": "auto", "kind": "phrase", "query": q, "gold": [r["key"]]})

    for i, it in enumerate(auto, 1):
        it["id"] = f"auto-{i:03d}"
    path = root / "eval" / "search.json"
    old = load_set(path)
    items = merge_items(old["items"] if old else [], auto)
    write_json(path, {"app": app, "seed": seed, "items": items})
    return {"app": app, "items": len(items), "by_kind": count_kinds(items), "pool": len(rows)}


def count_kinds(items):
    kinds = {}
    for it in items:
        kinds[it.get("kind", "?")] = kinds.get(it.get("kind", "?"), 0) + 1
    return kinds


def ask_llm(url, model, text, timeout=180):
    """A local generative model's answer to LLM_PROMPT + text (a loopback server only, as the embedder)."""
    import urllib.request  # noqa: PLC0415
    from rawlayer.search import common, dense  # noqa: PLC0415
    try:
        endpoint = dense.check_url(url) + "/api/chat"
    except common.UsageError as e:
        raise EvalError(str(e)) from None
    body = json.dumps({"model": model, "messages": [{"role": "user", "content": LLM_PROMPT + text}], "stream": False,
                       "think": False, "keep_alive": "10m", "options": {"temperature": 0.7, "num_predict": 80}})
    req = urllib.request.Request(endpoint, body.encode(), {"Content-Type": "application/json"}, method="POST")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), dense._NoRedirect)
    try:
        with opener.open(req, timeout=timeout) as r:
            out = json.loads(r.read())
    except (OSError, ValueError) as e:
        raise EvalError(f"the generative model failed: {type(e).__name__} (is {model} pulled?)") from None
    return re.sub(r"<think>.*?</think>", "", (out.get("message") or {}).get("content") or "", flags=re.S)


def paraphrase_query(answer, text):
    """The model's question as a plain query, or None when it is unusable or mostly the text's own bigrams."""
    from rawlayer.search import textindex as ti  # noqa: PLC0415
    lines = [ln for ln in answer.strip().splitlines() if ln.strip()]
    q = " ".join(w for w in re.sub(r"[^0-9A-Za-z가-힣\s]", " ", lines[0] if lines else "").split() if w.upper() != "OR")
    qt = {t for t in ti.ng_tokens(q) if t != ti.SEP}
    if not 4 <= len(q) <= PARA_MAX_LEN or len(qt) < 2:
        return None
    return q if len(qt & set(ti.ng_tokens(text))) / len(qt) <= PARA_MAX_SHARED else None


def paraphrase(app, n, seed, pool, model, url, runner):
    """Replace the eval set's paraphrase items: n questions a local model wrote about random rows, in other words
    than the row's (kind "paraphrase", gold = that row): what keyword search alone tends to miss."""
    root = data_root(app)
    path = root / "eval" / "search.json"
    old = load_set(path)
    if old is None:
        raise EvalError(f"{app}: no eval set: run `generate {app}` first")
    env = search(runner, app, ["--since=1970-01-01", f"--limit={pool}", "--width=20"])
    if env is None:
        raise EvalError(f"{app}: the browse failed (is there an index?)")
    texts = [(r["key"], t) for r in env.get("rows", []) if isinstance(r, dict) and r.get("key")
             for t in [r.get("body_text") or r.get("text")]
             if isinstance(t, str) and 40 <= len(t) <= 1500 and len(re.findall("[가-힣]", t)) >= 10]
    rng = random.Random(seed)
    rng.shuffle(texts)
    new, seen = [], set()
    for key, text in texts[:n * 3]:
        if len(new) == n:
            break
        q = paraphrase_query(ask_llm(url, model, text[:1200]), text)
        if q and q not in seen:
            seen.add(q)
            new.append({"id": f"para-{len(new) + 1:03d}", "source": "llm", "kind": "paraphrase", "query": q,
                        "gold": [key]})
    items = [it for it in old["items"] if it.get("kind") != "paraphrase"] + new
    write_json(path, dict(old, items=items))
    return {"app": app, "items": len(items), "by_kind": count_kinds(items), "asked": min(len(texts), n * 3),
            "model": model}


def run(app, modes, label, runner):
    root = data_root(app)
    d = load_set(root / "eval" / "search.json")
    if d is None or not d["items"]:
        raise EvalError(f"{app}: no eval set: run `generate {app}` first")
    out = {"app": app, "label": label, "at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
           "items": len(d["items"]), "index": None, "modes": {}}
    for mode in modes:
        results, by_kind, variants, fallback = [], {}, {}, 0
        for it in d["items"]:
            t0 = time.perf_counter()
            # hybrid: every fused unit (both lists), so the retrievers can be scored alone too
            limit = HYBRID_LIMIT if mode == "hybrid" else max(KS)
            env = search(runner, app, [f"--limit={limit}", *MODE_ARGS[mode], f"--query={it['query']}"])
            ms = (time.perf_counter() - t0) * 1000
            rows = env.get("rows", []) if env else []
            ranking = [r.get("key") for r in rows][:max(KS)]
            if env and out["index"] is None:
                out["index"] = dict(env.get("index") or {}, built_at=env.get("built_at"))
            res = dict(score(ranking, it["gold"]), ms=ms, error=env is None, zero=env is not None and not ranking)
            results.append(res)
            by_kind.setdefault(it.get("kind", "?"), []).append(res)
            if mode == "hybrid":
                fallback += bool(env) and not (env.get("hybrid") or {}).get("available")
                for name, rk in hybrid_rankings(rows).items():
                    variants.setdefault(name, {}).setdefault(it.get("kind", "?"), []).append(
                        dict(score(rk, it["gold"]), ms=ms, error=env is None, zero=env is not None and not rk))
        out["modes"][mode] = dict(summarize(results), by_kind={k: summarize(v) for k, v in sorted(by_kind.items())})
        if mode == "hybrid":
            out["modes"][mode].update(fallback=fallback, model=(env or {}).get("hybrid", {}).get("model"), variants={
                name: dict(summarize([r for v in kinds.values() for r in v]),
                           by_kind={k: summarize(v) for k, v in sorted(kinds.items())})
                for name, kinds in variants.items()})
    with open(root / "eval" / "results.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps(out, ensure_ascii=False) + "\n")
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(prog="search_eval.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("generate")
    g.add_argument("app")
    g.add_argument("--n", type=int, default=100)
    g.add_argument("--seed", type=int, default=42)
    g.add_argument("--pool", type=int, default=200000, help="newest rows browsed for candidates")
    p = sub.add_parser("paraphrase")
    p.add_argument("app")
    p.add_argument("--n", type=int, default=30)
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--pool", type=int, default=200000, help="newest rows browsed for candidates")
    p.add_argument("--model", default=LLM_MODEL, help=f"a local generative model in Ollama (default {LLM_MODEL})")
    p.add_argument("--url", default=LLM_URL, help="the Ollama server, a loopback IP only")
    r = sub.add_parser("run")
    r.add_argument("app")
    r.add_argument("--modes", default="default")
    r.add_argument("--label", default="")
    r.add_argument("--manage", help="the manage.py of another checkout to search with (a baseline of older code)")
    a = ap.parse_args(argv)
    runner = M.Runner(timeout=TIMEOUT, manage=getattr(a, "manage", None))
    try:
        if a.cmd in ("generate", "paraphrase"):
            if a.n < 1 or a.pool < 1:
                raise EvalError("--n and --pool must be at least 1")
            res = generate(a.app, a.n, a.seed, a.pool, runner) if a.cmd == "generate" else \
                paraphrase(a.app, a.n, a.seed, a.pool, a.model, a.url, runner)
        else:
            modes = [m for m in a.modes.split(",") if m]
            bad = [m for m in modes if m not in MODE_ARGS]
            if bad or not modes:
                raise EvalError(f"--modes: one or more of {', '.join(MODE_ARGS)}")
            if a.manage and not Path(a.manage).is_file():
                raise EvalError("--manage: no such file")
            res = run(a.app, modes, a.label, runner)
    except EvalError as e:
        print(f"search_eval: {e}", file=sys.stderr)
        return 2
    print(json.dumps(res, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
