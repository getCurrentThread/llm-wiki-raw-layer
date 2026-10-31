# -*- coding: utf-8 -*-
"""The framework (rawlayer/) stays source-agnostic: no origin, source or app name in its code (names and string
literals; docstrings and comments may cite history), the project's apps keep the app contract."""
import ast
import re
import subprocess
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
SOURCE_WORDS = re.compile(r"kakao|edb|mm_archive|merged_out|live_out|backup_decrypted|decrypted_live|mattermost|"
                          r"mattertok|kakatok|chatlog|keystore|mm:", re.I)


def py_files(top):
    """The *.py files at or below `top` that glob.glob(<top>/**/*.py, recursive=True) finds, sorted: Path.rglob also
    lists names starting with '.' and what lies below such a folder, glob.glob does not."""
    return sorted(p for p in top.rglob("*.py") if not any(n.startswith(".") for n in p.relative_to(top).parts))


def source_words_in(path):
    path = Path(path)
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docs = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.body:
            b = node.body[0]
            if isinstance(b, ast.Expr) and isinstance(b.value, ast.Constant) and isinstance(b.value.value, str):
                docs.add(id(b.value))
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docs:
            vals = [node.value]
        elif isinstance(node, ast.Name):
            vals = [node.id]
        elif isinstance(node, ast.Attribute):
            vals = [node.attr]
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            vals = [node.name]
        elif isinstance(node, ast.arg):
            vals = [node.arg]
        elif isinstance(node, ast.alias):
            vals = [node.name, node.asname or ""]
        elif isinstance(node, ast.ImportFrom):
            vals = [node.module or ""]
        else:
            continue
        # walk_up: a file outside the project (the scan's own test) is named with '..' parts, as os.path.relpath did
        hits += [(str(path.relative_to(REPO, walk_up=True)), getattr(node, "lineno", 0))
                 for v in vals if SOURCE_WORDS.search(v)]
    return hits


class TestSourceAgnosticCore(unittest.TestCase):
    def test_no_source_names_in_the_framework(self):
        files = py_files(REPO / "rawlayer")
        self.assertTrue(files)
        self.assertEqual([h for f in files for h in source_words_in(f)], [])

    def test_no_source_names_in_the_core_tests(self):
        """tests/core travels with the framework into every project: it tests the core on the toy app alone."""
        here = Path(__file__).resolve()
        files = [f for f in py_files(REPO / "tests" / "core") if f != here]
        self.assertTrue(files)
        self.assertEqual([h for f in files for h in source_words_in(f)], [])

    def test_the_scan_sees_names_and_literals(self):
        import tempfile  # noqa: PLC0415
        src = ('"""kakao in a docstring is fine."""\n'
               'import apps.kakao\n'
               'def f(live_out):\n'
               '    """edb here too."""\n'
               '    return "mm_archive", x.edb_dir\n')
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, encoding="utf-8") as fh:
            fh.write(src)
        try:
            self.assertEqual(len(source_words_in(fh.name)), 4)
        finally:
            Path(fh.name).unlink()


class TestProjectApps(unittest.TestCase):
    """This project's apps (whatever discover() finds next to manage.py; none is a project too)."""

    def test_check_passes(self):
        """manage.py check: every app keeps the app contract, none collides with another, the core is the listed
        one. Run as the operator runs it, in a process of its own."""
        from rawlayer import registry  # noqa: PLC0415
        p = subprocess.run([sys.executable, "-s", "-B", REPO / "manage.py", "check"], cwd=REPO,
                           capture_output=True, text=True, timeout=300)
        self.assertEqual(p.returncode, 0, p.stderr[-2000:])
        for app in registry.discover():
            self.assertIn(f"{app}: origins=", p.stdout)

    def test_every_app_has_the_same_modules(self):
        from rawlayer import registry  # noqa: PLC0415
        for name in registry.discover():
            with self.subTest(app=name):
                for mod in ("__init__", "apps", "origins", "inputs", "vocab"):
                    self.assertTrue((REPO / name / (mod + ".py")).is_file(), mod)
                app = registry.app_registry(name).apps[0]       # loaded on its own, never installed here
                self.assertEqual((app.name, app.label), (name, name))
                self.assertTrue(app.origins)
                self.assertTrue(callable(app.inputs.resolve) and callable(app.inputs.static_roots))
                self.assertTrue((REPO / name / "tests" / "__init__.py").is_file())


if __name__ == "__main__":
    unittest.main()
