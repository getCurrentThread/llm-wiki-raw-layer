# -*- coding: utf-8 -*-
"""python -s -B <PROJECT>/tools/bootstrap.py agents <command> ... -- this checkout's agent integration (llm.md 11):
tools/agent_templates/ rendered into Claude Code's global configuration, i.e. a marked block in .claude/CLAUDE.md (the
rest of the file kept byte for byte), the raw-search and llm-wiki-setup skills and the raw-retriever subagent. The
user-scope MCP server llm_wiki_raw that an earlier version registered is removed by install and uninstall. Standard
library only.

  status [--json]                     each item absent, current, stale (an older version this tool wrote), modified
                                      (changed by hand since), foreign (not written by this tool) or unreadable,
                                      and project_mismatch; exit 0 only when every item is current
  install [--dry-run]                 writes what is absent or stale, refusing a modified or foreign item
  uninstall [--dry-run]               removes what install wrote and nobody changed since; the rest is kept, named

--dry-run prints the diff (paths masked). The manifest lives in %LOCALAPPDATA%\\llm-wiki-raw-layer\\agentkit.

Exit codes: 0 ok / current | 3 needs attention | 2 refused or usage | 1 error or interrupted."""
import argparse
import difflib
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
if str(PROJECT) not in sys.path:
    sys.path.insert(0, str(PROJECT))

TEMPLATES = PROJECT / "tools" / "agent_templates"
BEGIN = "<!-- llm-wiki-raw-layer:begin (managed by tools/bootstrap.py agents; edit tools/agent_templates/rules.md) -->"
END = "<!-- llm-wiki-raw-layer:end -->"
_BEGIN = re.compile(r"^<!-- llm-wiki-raw-layer:begin\b[^\n]*-->[ \t]*\r?$", re.M)
_END = re.compile(r"^<!-- llm-wiki-raw-layer:end -->[ \t]*(?:\r?\n|\Z)", re.M)
BOM = b"\xef\xbb\xbf"
CLAUDE_MD = ".claude/CLAUDE.md"
MCP_NAME = "llm_wiki_raw"           # the MCP server an earlier version registered: removed when it is ours
# (template, kind, path): kind picks the frontmatter keys the file keeps (FRONT)
GLOBAL_FILES = (("raw-search.md", "skill", ".claude/skills/raw-search/SKILL.md"),
                ("llm-wiki-setup.md", "skill", ".claude/skills/llm-wiki-setup/SKILL.md"),
                ("raw-retriever.md", "agent", ".claude/agents/raw-retriever.md"))
FRONT = {"skill": ("name", "description", "argument-hint", "allowed-tools"),
         "agent": ("name", "description", "tools", "model", "effort")}
_VAR = re.compile(r"\{\{(include:)?([A-Za-z0-9_.-]+)\}\}")


from tools.bootstrap import Refused, _out, guarded  # noqa: E402 -- one masking and exit-code handling for both tools


def _sha(text):
    # newlines are not content: CRLF or LF, or a final newline an editor dropped (the block often ends the file)
    return hashlib.sha256(text.replace("\r\n", "\n").rstrip("\n").encode("utf-8")).hexdigest()



# ---------------------------------------------------------------------------------------------------- rendering

def apps():
    from rawlayer import registry  # noqa: PLC0415
    return list(registry.discover())


def values(found):
    """The template variables: absolute paths, so the files work from any folder."""
    posix = PROJECT.as_posix()
    run = f'python -s -B "{posix}/manage.py"'
    return {"PROJECT": str(PROJECT), "APPS": ", ".join(found), "RUN": run,
            "BOOT": f'python -s -B "{posix}/tools/bootstrap.py"', "DOC": f"{posix}/", "WHERE": "from any folder",
            "BASH_SEARCH": f"Bash({run} search:*)"}


def render(name, vals, _depth=0):
    """Template `name` with {{VAR}} and {{include:file}} substituted; a variable without a value is an error."""
    text = (TEMPLATES / name).read_text(encoding="utf-8").replace("\r\n", "\n")

    def sub(m):
        if m.group(1):
            if _depth >= 2:
                raise ValueError(f"template {name}: includes nest too deep")
            return render(m.group(2), vals, _depth + 1).rstrip("\n")
        if vals.get(m.group(2)) is None:
            raise ValueError(f"template {name}: {{{{{m.group(2)}}}}} has no value for this target")
        return vals[m.group(2)]
    return _VAR.sub(sub, text)


def artifact(template, kind, vals):
    """A rendered skill or agent file: the template's frontmatter cut to the keys `kind` keeps, then its body."""
    text = render(template, vals)
    front, body = {}, text
    m = re.match(r"---\n(.*?)\n---\n", text, re.S)
    if m:
        for ln in m.group(1).splitlines():
            key, _sep, value = ln.partition(":")
            front[key.strip()] = value.strip()
        body = text[m.end():]
    head = "".join(f"{k}: {front[k]}\n" for k in FRONT[kind] if k in front)
    return f"---\n{head}---\n{body}"


def block(vals, nl="\n"):
    """The global CLAUDE.md block, markers included, ending with a newline."""
    body = render("rules.md", vals).rstrip("\n")
    return f"{BEGIN}\n{body}\n{END}\n".replace("\n", nl)


# ---------------------------------------------------------------------------------------------------- files

def read_doc(path):
    """(text, bom) of the global CLAUDE.md as it is, newlines kept; (None, False) when there is none. Refused when it
    is not UTF-8: the rest of the file is rewritten around the block, so it is never decoded loosely."""
    if not path.is_file():
        return None, False
    raw = path.read_bytes()
    bom = raw.startswith(BOM)
    try:
        return raw[3 if bom else 0:].decode("utf-8"), bom
    except UnicodeDecodeError:
        raise Refused(f"{CLAUDE_MD} is not UTF-8 (UTF-16 or ANSI?): save it as UTF-8 and run again") from None


def _shown(path):
    """A file's text for a diff only (never written back), or None when there is none."""
    return path.read_bytes().decode("utf-8", "replace") if path.is_file() else None


def write_raw(path, data):
    tmp = path.with_name(path.name + ".agentkit-part")
    tmp.write_bytes(data)
    tmp.replace(path)


def write_doc(path, text, bom=False):
    write_raw(path, (BOM if bom else b"") + text.encode("utf-8"))


def newline_of(text):
    return "\r\n" if text and "\r\n" in text else "\n"


def find_block(text):
    """(start, end) of the marked block (its last newline included); None when there is none; Refused when the
    markers are duplicated or out of order."""
    begins, ends = list(_BEGIN.finditer(text)), list(_END.finditer(text))
    if not begins and not ends:
        return None
    if len(begins) != 1 or len(ends) != 1 or ends[0].start() < begins[0].end():
        raise Refused(f"{CLAUDE_MD}: the llm-wiki-raw-layer markers are duplicated or out of order; fix them by hand")
    return begins[0].start(), ends[0].end()


# ---------------------------------------------------------------------------------------------------- the target

def state_dir():
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "llm-wiki-raw-layer" / "agentkit"


def _claude_cli(argv):
    exe = shutil.which("claude")
    if not exe:
        raise RuntimeError("the claude CLI is not on PATH")
    p = subprocess.run([exe, *argv], stdin=subprocess.DEVNULL, capture_output=True, timeout=120)
    if p.returncode:                    # the CLI's own reason, in the error of the call it belongs to
        why = (p.stderr or p.stdout).decode("utf-8", "replace").strip().splitlines()
        raise RuntimeError(f"claude {' '.join(argv[:2])} {MCP_NAME} failed (rc={p.returncode})"
                           + (f": {why[-1].strip()}" if why else ""))
    return 0


class Kit:
    """Claude Code's configuration in `home` (its .claude/ folder and .claude.json), with the manifest in `state`.
    `claude(argv) -> rc` runs the claude CLI; `found` are the apps the templates name."""

    def __init__(self, home=None, state=None, claude=None, found=None):
        self.home = Path(home) if home else Path.home()
        self.state = Path(state) if state else state_dir()
        self.claude = claude or _claude_cli
        self.found = list(found if found is not None else apps())
        self.vals = values(self.found)
        self.manifest = self._load()
        self.problems = []              # status: why an item is unreadable

    # ---- the manifest
    def _load(self):
        empty = self._empty()
        try:
            m = json.loads((self.state / "manifest.json").read_text(encoding="utf-8"))
            if isinstance(m, dict) and m.get("v") == 1:
                # an older version's keys (backups, mcp_pending) are dropped: nothing reads them any more
                return {k: m.get(k, v) for k, v in empty.items()}
        except (OSError, ValueError):
            pass
        return empty

    @staticmethod
    def _empty():
        # files: path -> sha256 written; block: {sha256, before, after, created}; mcp: the entry an earlier version
        # registered; dirs: folders install created
        return {"v": 1, "project": None, "files": {}, "block": None, "mcp": None, "dirs": []}

    def _save(self):
        self.state.mkdir(parents=True, exist_ok=True)
        tmp = self.state / "manifest.json.part"
        tmp.write_text(json.dumps(self.manifest, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        tmp.replace(self.state / "manifest.json")

    def _cli(self, argv):
        rc = self.claude(argv)
        if rc != 0:
            raise RuntimeError(f"claude {' '.join(argv[:2])} {MCP_NAME} failed (rc={rc})")

    # ---- the wanted state
    def files(self):
        return {rel: artifact(t, kind, self.vals) for t, kind, rel in GLOBAL_FILES}

    def mcp_now(self):
        """The user-scope llm_wiki_raw entry of .claude.json, or None."""
        try:
            d = json.loads((self.home / ".claude.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        s = (d.get("mcpServers") or {}).get(MCP_NAME) if isinstance(d, dict) else None
        return s if isinstance(s, dict) else None

    # ---- states
    def file_state(self, rel, want):
        p = self.home / rel
        if not p.is_file():
            return "absent"
        cur = _sha(p.read_bytes().decode("utf-8", "replace"))
        if cur == _sha(want):
            return "current"
        known = self.manifest["files"].get(rel)
        if known is None:
            return "foreign"
        return "stale" if cur == known else "modified"

    def block_state(self):
        """(state, text, span, bom) of the global CLAUDE.md's block; Refused when the file cannot be worked on."""
        text, bom = read_doc(self.home / CLAUDE_MD)
        if text is None:
            return "absent", None, None, False
        span = find_block(text)
        if span is None:
            return "absent", text, None, bom
        cur = _sha(text[span[0]:span[1]])
        if cur == _sha(block(self.vals)):
            return "current", text, span, bom
        known = (self.manifest["block"] or {}).get("sha256")
        return ("stale" if cur == known else "modified" if known else "foreign"), text, span, bom

    def mcp_retired(self):
        """The llm_wiki_raw entry when an earlier version of this tool registered it (recorded in the manifest, or
        running tools/mcp_rawsearch.py), else None: someone else's server of that name is left alone."""
        now = self.mcp_now()
        if now is not None and (self.manifest["mcp"] is not None or "mcp_rawsearch.py" in json.dumps(now)):
            return now
        return None

    def status(self):
        self.problems = []
        try:
            st = self.block_state()[0]
        except Refused as e:
            st = "unreadable"
            self.problems.append(str(e))
        rows = {f"{CLAUDE_MD} (block)": st}
        for rel, want in self.files().items():
            rows[rel] = self.file_state(rel, want)
        if self.mcp_retired() is not None:
            rows[f"mcp {MCP_NAME}"] = "retired"
        project = self.manifest["project"]
        mismatch = project is not None and os.path.normcase(project) != os.path.normcase(str(PROJECT))
        return rows, mismatch

    # ---- install
    def plan_install(self):
        """[(label, old, new, apply)] -- old/new texts for the diff (None: no such file), apply() does it. Only what
        is absent or this tool's own unchanged copy (stale) is written: anything else is refused, never replaced."""
        plan, refusals = [], []
        st, text, span, bom = self.block_state()
        nl = newline_of(text)
        want = block(self.vals, nl)
        if st == "absent":
            before = "" if not text else ("" if text.endswith(("\n", "\r")) else nl) + nl
            new = (text or "") + before + want
            plan.append((CLAUDE_MD, text, new, lambda new=new, before=before, created=text is None:
                         self._write_block(new, bom, before, "", created)))
        elif st == "stale":
            prev = self.manifest["block"] or {}
            new = text[:span[0]] + want + text[span[1]:]
            plan.append((CLAUDE_MD, text, new, lambda new=new, prev=prev: self._write_block(
                new, bom, prev.get("before", ""), prev.get("after", ""), prev.get("created", False))))
        elif st in ("modified", "foreign"):
            refusals.append(f"{CLAUDE_MD}: the block is {st}; change it back or take it out by hand, then install")
        for rel, body in self.files().items():
            fst = self.file_state(rel, body)
            if fst in ("absent", "stale"):
                plan.append((rel, _shown(self.home / rel), body, lambda rel=rel, body=body: self._write_file(rel, body)))
            elif fst in ("modified", "foreign"):
                refusals.append(f"{rel}: {fst}; move it away by hand, then install")
        self._plan_mcp_removal(plan)
        if refusals:
            raise Refused("; ".join(refusals))
        return plan

    def install(self, dry_run=False):
        plan = self.plan_install()
        if dry_run:
            return plan
        if plan:
            self.manifest["project"] = str(PROJECT)
            self._save()
        for _label, _old, _new, apply in plan:
            apply()
        return plan

    def _mkdirs(self, rel):
        """Create the missing folders of `rel` under home, recording them for uninstall."""
        missing, d = [], (self.home / rel).parent
        while not d.exists() and d != self.home:
            missing.append(d)
            d = d.parent
        for d in reversed(missing):
            d.mkdir()
            r = d.relative_to(self.home).as_posix()
            if r not in self.manifest["dirs"]:
                self.manifest["dirs"].append(r)
        self._save()

    def _write_block(self, new, bom, before, after, created):
        self._mkdirs(CLAUDE_MD)
        write_doc(self.home / CLAUDE_MD, new, bom)
        self.manifest["block"] = {"sha256": _sha(block(self.vals)), "before": before, "after": after,
                                  "created": created}
        self._save()

    def _write_file(self, rel, body):
        self._mkdirs(rel)
        write_doc(self.home / rel, body)
        self.manifest["files"][rel] = _sha(body)
        self._save()

    def _plan_mcp_removal(self, plan):
        now = self.mcp_retired()
        if now is not None:
            def apply_mcp():
                self._cli(["mcp", "remove", "--scope", "user", MCP_NAME])
                self.manifest["mcp"] = None
                self._save()
            plan.append((f"mcp {MCP_NAME} (claude mcp remove --scope user)", json.dumps(now, indent=1) + "\n", None,
                         apply_mcp))

    # ---- uninstall
    def plan_uninstall(self):
        """([(label, old, new, apply)], kept): what this tool wrote and nobody changed since is removed; an item
        changed by hand is kept and named."""
        plan, kept = [], []
        st, text, span, bom = self.block_state()
        if span is not None and st in ("current", "stale"):
            info = self.manifest["block"] or {}
            start, end = span
            before, after = info.get("before", ""), info.get("after", "")
            if before and text[max(0, start - len(before)):start] == before:
                start -= len(before)
            if after and text[end:end + len(after)] == after:
                end += len(after)
            new = text[:start] + text[end:]
            gone = new == "" and info.get("created")

            def apply_block(new=new, gone=gone):
                p = self.home / CLAUDE_MD
                if gone:
                    p.unlink()
                else:
                    write_doc(p, new, bom)
                self.manifest["block"] = None
                self._save()
            plan.append((CLAUDE_MD, text, None if gone else new, apply_block))
        elif span is not None:
            kept.append(f"{CLAUDE_MD} (block {st})")
        for rel, body in self.files().items():
            fst = self.file_state(rel, body)
            if fst in ("current", "stale"):
                def apply_file(rel=rel):
                    (self.home / rel).unlink()
                    self.manifest["files"].pop(rel, None)
                    self._save()
                plan.append((rel, _shown(self.home / rel), None, apply_file))
            elif fst == "modified":
                kept.append(f"{rel} (modified)")
        self._plan_mcp_removal(plan)
        return plan, kept

    def uninstall(self, dry_run=False):
        plan, kept = self.plan_uninstall()
        if dry_run:
            return plan, kept
        try:
            for _label, _old, _new, apply in plan:
                apply()
            # what was already gone by hand leaves the manifest too
            if self.block_state()[2] is None:
                self.manifest["block"] = None
            for rel in [r for r in self.manifest["files"] if not (self.home / r).is_file()]:
                self.manifest["files"].pop(rel)
            if self.mcp_now() is None:
                self.manifest["mcp"] = None
            for rel in sorted(self.manifest["dirs"], key=len, reverse=True):
                d = self.home / rel
                try:
                    if d.is_dir() and not any(d.iterdir()):
                        d.rmdir()
                    if not d.exists():
                        self.manifest["dirs"].remove(rel)
                except OSError:
                    pass
            if not self.manifest["files"] and not self.manifest["block"] and not self.manifest["mcp"]:
                self.manifest["project"] = None
        finally:
            self._save()
        return plan, kept


# ---------------------------------------------------------------------------------------------------- main

def _show(plan, verb):
    for label, old, new, _apply in plan:
        what = "remove" if new is None else "create" if old is None else "update"
        _out(f"agents: {verb} {what} {label}")


def _diff(plan):
    for label, old, new, _apply in plan:
        a = (old or "").replace("\r\n", "\n").splitlines(keepends=True)
        b = (new or "").replace("\r\n", "\n").splitlines(keepends=True)
        name = label if label.startswith("mcp ") else f"<HOME>/{label}"
        for ln in difflib.unified_diff(a, b, fromfile=name if old is not None else "/dev/null",
                                       tofile=name if new is not None else "/dev/null", n=2):
            _out(ln.rstrip("\n"))


def parser():
    ap = argparse.ArgumentParser(prog="tools/bootstrap.py agents", description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="command", required=True)
    s = sub.add_parser("status")
    s.add_argument("--json", action="store_true")
    i = sub.add_parser("install")
    i.add_argument("--dry-run", action="store_true")
    u = sub.add_parser("uninstall")
    u.add_argument("--dry-run", action="store_true")
    return ap


def _status_lines(k, rows, mismatch):
    for label, st in rows.items():
        _out(f"agents: {label}: {st}")
    for p in k.problems:
        _out(f"agents: {p}")
    if mismatch:
        _out("agents: project_mismatch: installed from another checkout; install from this one to point the "
             "integration here")


def main(argv=None, kit=None):
    a = parser().parse_args(argv)
    return guarded(lambda: _run(a, kit), "agents", "interrupted; `agents status` shows what was applied")


def _run(a, kit):
    k = kit or Kit()
    if a.command == "status":
        rows, mismatch = k.status()
        rc = 0 if set(rows.values()) == {"current"} and not mismatch else 3
        if a.json:
            _out(json.dumps({"items": rows, "project_mismatch": mismatch, "problems": k.problems, "rc": rc},
                            indent=1))
        else:
            _status_lines(k, rows, mismatch)
        return rc
    if a.command == "install":
        plan = k.install(dry_run=a.dry_run)
        if not plan:
            rows, mismatch = k.status()
            if set(rows.values()) == {"current"} and not mismatch:
                _out("agents: everything is current")
                return 0
            _status_lines(k, rows, mismatch)
            _out("agents: nothing to install, but not everything is current")
            return 3
        if a.dry_run:
            _diff(plan)
        _show(plan, "would" if a.dry_run else "did")
        return 0
    plan, kept = k.uninstall(dry_run=a.dry_run)
    if a.dry_run:
        _diff(plan)
    _show(plan, "would" if a.dry_run else "did")
    for x in kept:
        _out(f"agents: kept {x}: changed by hand; remove it by hand if it should go")
    if not plan and not kept:
        _out("agents: nothing installed")
    return 3 if kept else 0
