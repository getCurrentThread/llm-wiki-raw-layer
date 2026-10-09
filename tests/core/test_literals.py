# -*- coding: utf-8 -*-
"""The capture pipeline's public outputs (SPEC 6, 11): canon (the record hash input), publicout's vocabulary and
leak refusal, the lit() literal contract over every capture source file of the project (the core and each installed
app's capture side), and every note / status literal being a registered public word."""
import ast
import base64
import json
import math
import subprocess
import sys
import unittest
from pathlib import Path

from rawlayer import registry
from rawlayer.capture import canon, publicout
from tests.core import toyfix as T

BIG = 2 ** 53 + 1           # an integer a float would round
PUBLICOUT = Path("rawlayer", "capture", "publicout.py")     # the one module allowed to build Lit


def capture_sources(apps=None):
    """The capture pipeline's source files: the core (rawlayer/capture) and the capture side of every discovered app
    (or of `apps`): its folder without tests/, management/ and search.py (the search side)."""
    out = T.py_files(T.REPO / "rawlayer" / "capture", "*.py")
    for app in registry.discover() if apps is None else apps:
        top = T.REPO / app
        for f in T.py_files(top):
            rel = f.relative_to(top).as_posix()
            if rel != "search.py" and not rel.startswith(("tests/", "management/")):
                out.append(f)
    return out


def spec_enc(x):
    """SPEC 6 reference, verbatim."""
    if isinstance(x, (bytes, bytearray, memoryview)):
        return {"$b64": base64.b64encode(bytes(x)).decode("ascii")}
    if isinstance(x, dict):
        return {str(k): spec_enc(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [spec_enc(v) for v in x]
    return x


def spec_canon(x):
    return json.dumps(spec_enc(x), sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


class TestCanon(unittest.TestCase):
    def test_matches_spec_reference(self):
        cases = [None, True, 0, -1, BIG, -BIG, 1.5, "", "a bé\U0001f600\x00\x1f\"\\",
                 b"", b"\x00\xff", bytearray(b"ab"), memoryview(b"cd"),
                 {"z": 1, "a": [1, (2, b"x"), {"k": None}], "m": {"b": b"\x01", "a": "x"}},
                 [(), [], {}], {"$b64": "not bytes"}]
        for c in cases:
            self.assertEqual(canon.canon(c), spec_canon(c))
        with self.assertRaises(TypeError):
            canon.canon({"x": object()})

    def test_bytes_null_bigint(self):
        s = canon.canon({"b": b"\x00\xff", "n": None, "i": BIG})
        self.assertEqual(s, '{"b":{"$b64":"AP8="},"i":9007199254740993,"n":null}')

    def test_nan_rejected(self):
        with self.assertRaises(ValueError):
            canon.canon({"x": math.nan})
        with self.assertRaises(ValueError):
            canon.record_h({"x": math.inf}, {})

    def test_kst_day(self):
        self.assertEqual(canon.kst_day((1_600_000_000 - 3600 * 9) * 1000), "2020-09-13")
        self.assertEqual(canon.kst_day(None), "none")

    def test_parse_default(self):
        self.assertEqual(canon.parse_default("0"), 0)
        self.assertEqual(canon.parse_default("'it''s'"), "it's")
        self.assertIsNone(canon.parse_default("NULL"))
        self.assertIs(canon.parse_default("CURRENT_TIMESTAMP"), canon.NO_DEFAULT)
        self.assertFalse(canon.default_equal(0.0, "0"))

    def test_source_id_and_digest(self):
        self.assertEqual(len(canon.source_id("a/c1.jsonl")), 12)
        self.assertEqual(canon.source_id("a/c1.jsonl"), canon.source_id("a/c1.jsonl"))
        self.assertNotEqual(canon.source_id("a/c1.jsonl"), canon.source_id("b/c1.jsonl"))
        self.assertEqual(canon.facts_digest(["b\t2", "a\t1"]), canon.facts_digest(["a\t1", "b\t2"]))


class TestPublicout(T.ToyCase):
    STANDARD = False

    def test_rejects_unregistered_and_non_ascii(self):
        with self.assertRaises(publicout.LeakError):
            publicout.render_json({"status": "somebody"})
        with self.assertRaises(publicout.LeakError):
            publicout.lit("가")
        with self.assertRaises(publicout.LeakError):
            publicout.render_json({"pending": {"add": ["toy:toy_b:Odd"]}})
        with self.assertRaises(publicout.LeakError):
            publicout.render_json({"status": "ok\n"})
        publicout.render_json({"status": "unreadable", "n": 3, "path": "toy:toy_b:12", "origin": "toy_m",
                               "notes": {"bad_line": 1}, "pending": {"add": ["rec"]}})

    def test_installed_words_follow_the_registry(self):
        self.assertTrue(publicout.ok_str("toy_a"))
        with registry.use([T.TOY2]):
            self.assertFalse(publicout.ok_str("toy_a"))
            self.assertFalse(publicout.ok_str("toy:toy_b:12"))
        self.assertTrue(publicout.ok_str("toy:toy_b:12"))


def _callee(call):
    fn = call.func
    return fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", None)


def literal_contract_violations(sources):
    """(file, line, rule) for every break of the publicout literal contract in {file name: source}.
    - lit() wraps exactly one str constant; lit is never used as a value (map(lit, xs), f = lit);
    - Lit(...) is built only inside publicout.py (cell() trusts any Lit);
    - a keyword mapping (**x) reaches say()/append_line() only from a wrapper handing on its own **kwargs
      untouched (the name appears nowhere else in the wrapper), and such a wrapper is itself a sink: its
      callers are held to the same rule (a Report.add-style method)."""
    trees = {f: ast.parse(src) for f, src in sources.items()}
    enclosing = {}

    def visit(node, fn):
        for ch in ast.iter_child_nodes(node):
            enclosing[id(ch)] = fn
            visit(ch, ch if isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef)) else fn)
    for t in trees.values():
        visit(t, None)
    sinks = {"say", "append_line"}
    grew = True
    while grew:                             # wrappers of wrappers
        grew = False
        for f, t in trees.items():
            for fn in ast.walk(t):
                if not isinstance(fn, ast.FunctionDef) or fn.args.kwarg is None or fn.name in sinks \
                        or f == PUBLICOUT:
                    continue
                kw = fn.args.kwarg.arg
                if any(isinstance(c, ast.Call) and _callee(c) in sinks and enclosing.get(id(c)) is fn
                       and any(k.arg is None and isinstance(k.value, ast.Name) and k.value.id == kw
                               for k in c.keywords) for c in ast.walk(fn)):
                    sinks.add(fn.name)
                    grew = True
    bad = []
    guarded = {"lit", "Lit"} | sinks
    for f, t in trees.items():
        callees = {id(c.func) for c in ast.walk(t) if isinstance(c, ast.Call)}
        forwarded = set()
        for node in ast.walk(t):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                for a in node.names:            # `import lit as L` would hide every call from the rules below
                    if a.name.rsplit(".", 1)[-1] in guarded and a.asname not in (None, a.name):
                        bad.append((f, node.lineno, "alias"))
            used = getattr(node, "id", None) or getattr(node, "attr", None)
            # a wrapper name (Report.add) is guarded as an attribute only: a local list called `add` is not it
            if isinstance(node, (ast.Name, ast.Attribute)) and used in guarded and isinstance(node.ctx, ast.Load) \
                    and (isinstance(node, ast.Attribute) or used in ("lit", "Lit", "say", "append_line")) \
                    and id(node) not in callees and not (f == PUBLICOUT and used == "Lit"):
                bad.append((f, node.lineno, "lit_as_value" if used == "lit" else "sink_as_value"))
            if not isinstance(node, ast.Call):
                continue
            name = _callee(node)
            if name == "lit" and not (len(node.args) == 1 and not node.keywords
                                      and isinstance(node.args[0], ast.Constant)
                                      and isinstance(node.args[0].value, str)):
                bad.append((f, node.lineno, "lit"))
            if name == "Lit" and f != PUBLICOUT:
                bad.append((f, node.lineno, "Lit"))
            if name in sinks:
                for k in node.keywords:
                    if k.arg is not None:
                        continue
                    fn = enclosing.get(id(node))
                    if isinstance(k.value, ast.Name) and fn is not None and fn.args.kwarg is not None \
                            and fn.args.kwarg.arg == k.value.id:
                        forwarded.add(id(k.value))
                    else:
                        bad.append((f, node.lineno, name))
        for fn in ast.walk(t):              # a forwarded **kwargs name is used for nothing else
            if isinstance(fn, ast.FunctionDef) and fn.args.kwarg is not None and fn.name in sinks \
                    and f != PUBLICOUT:
                kw = fn.args.kwarg.arg
                for n in ast.walk(fn):
                    if isinstance(n, ast.Name) and n.id == kw and id(n) not in forwarded:
                        bad.append((f, n.lineno, "kwargs_reused"))
    return sorted(bad)


class TestLiteralContract(unittest.TestCase):
    def sources(self):
        return {f.relative_to(T.REPO): f.read_text(encoding="utf-8") for f in capture_sources()}

    def test_package_keeps_the_contract(self):
        self.assertIn(PUBLICOUT, self.sources())
        self.assertEqual(literal_contract_violations(self.sources()), [])

    def test_contract_catches_known_escapes(self):
        """The shapes the review showed slipping past the earlier test: each one must be reported."""
        cases = {
            "mapping into a wrapper": "class R:\n def add(s, n, **kv):\n  say(n, **kv)\n"
                                      "def f(r, t):\n r.add('x', **{k: 1 for k in t})\n",
            "wrapper rebinding": "def add(n, **kv):\n kv = dict(rows)\n say(n, **kv)\n",
            "wrapper mutation": "def add(n, **kv):\n kv[name] = 1\n say(n, **kv)\n",
            "Lit built directly": "def f(x):\n say(Lit(x))\n",
            "lit as a value": "def f(xs):\n say(*map(lit, xs))\n",
            "lit on a variable": "def f(x):\n say(lit(x))\n",
            "lit imported under another name": "from .publicout import lit as L\ndef f(x):\n say(L(x))\n",
            "Lit imported under another name": "from .publicout import Lit as P\ndef f(x):\n say(P(x))\n",
            "sink imported under another name": "from .publicout import say as emit\ndef f(d):\n emit('x', **d)\n",
            "sink bound to a variable": "def f(row):\n out = publicout.say\n out(lit('x'), **row)\n",
            "wrapper bound to a variable": "class R:\n def add(s, n, **kv):\n  say(n, **kv)\n"
                                           "def f(r, c):\n emit = r.add\n emit('x', **c)\n",
            "mapping into a sink": "def f(d):\n append_line('p', **d)\n",
        }
        for label, src in cases.items():
            with self.subTest(case=label):
                self.assertNotEqual(literal_contract_violations({"m.py": src}), [])
        ok = "class R:\n def add(s, n, **kv):\n  say(n, **kv)\ndef f(r):\n r.add('x', n=1)\n say(lit('a'), k=2)\n"
        self.assertEqual(literal_contract_violations({"m.py": ok}), [])


class TestNoteLiterals(unittest.TestCase):
    def test_note_and_status_literals_are_registered(self):
        """Every ContainerError(status, note) literal and notes['...'] key of the capture pipeline is a public word,
        so a note can never stop a commit with LeakError: the core's and each app's, judged with that app selected
        (with the toy app in a project that has none)."""
        for app in registry.discover() or [None]:
            with self.subTest(app=app):
                lits = note_literals(capture_sources([] if app is None else [app]))
                self.assertTrue(lits)
                # judged in a process that imports only what capture imports, with the app selected
                use = f"registry.select({app!r})\n" if app else f"registry.use([{T.TOY!r}]).__enter__()\n"
                code = ("import json, sys\n"
                        f"sys.path.insert(0, {str(T.REPO)!r})\n"
                        "from rawlayer import registry\n"
                        "from rawlayer.capture import publicout\n"
                        + use +
                        "print(json.dumps(sorted(x for x in json.loads(sys.stdin.read()) if not publicout.ok_str(x))))\n")
                p = subprocess.run([sys.executable, "-s", "-B", "-c", code], input=json.dumps(sorted(map(str, lits))),
                                   capture_output=True, text=True, cwd=T.REPO, timeout=120)
                self.assertEqual((p.returncode, json.loads(p.stdout or "null")), (0, []))


def note_literals(sources):
    lits = set()
    for f in sources:
        tree = ast.parse(Path(f).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "ContainerError":
                lits.update(a.value for a in node.args if isinstance(a, ast.Constant))
            elif isinstance(node, ast.Subscript) and getattr(node.value, "attr", getattr(node.value, "id", None)) \
                    == "notes" and isinstance(node.slice, ast.Constant):
                lits.add(node.slice.value)
    return lits


if __name__ == "__main__":
    unittest.main()
