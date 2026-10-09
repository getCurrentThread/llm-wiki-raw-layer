# -*- coding: utf-8 -*-
"""python -s -B <PROJECT>/tools/bootstrap.py agents <command> ... -- this checkout's agent integration, from one
source (tools/agent_templates/) rendered into Claude Code's global configuration and into the repository's own agent
folders. Standard library only.

  status [--json]
      Each item and its state: absent, current, stale (an older version this tool wrote), modified (changed by hand
      since this tool wrote it), foreign (there, but not written by this tool), legacy (a hand-made copy from before
      this tool), unreadable (a global CLAUDE.md that is not UTF-8 or whose markers are broken); and
      project_mismatch when the integration was installed from another checkout. Exit 0 only when every item is
      current.
  install [--dry-run] [--adopt-legacy] [--force] [--python EXE]
      For Claude Code (the owner's profile):
        .claude/CLAUDE.md                     a marked block; the rest of the file is kept byte for byte
        .claude/skills/raw-search/SKILL.md, .claude/skills/llm-wiki-setup/SKILL.md, .claude/agents/raw-retriever.md
        the user-scope MCP server llm_wiki_raw (tools/mcp_rawsearch.py, through `claude mcp add-json`)
      A hand-made "# llm-wiki-raw-layer" section of the global CLAUDE.md and .claude/agents/knowledge-retriever.md
      are legacy: install refuses while they are there, and --adopt-legacy replaces them. A modified or foreign item
      is replaced only with --force. Whatever is replaced or removed is backed up first. --dry-run prints the diff
      (paths masked) and changes nothing. The MCP server runs with --python, else the interpreter it was installed
      with (while that is still there), else the one running this command.
  uninstall [--dry-run] [--restore] [--force]
      Removes what install wrote and nobody changed since (a modified item is kept and named; --force removes it
      too, saving the hand-changed copy first). --restore also puts back what install replaced or removed, from the
      backups; without it, the backups of the removed items are forgotten (their files stay in the state folder).
  sync-repo [--check]
      Renders the templates into this checkout (.claude/skills, .claude/agents, .agents/skills), with commands that
      run from the checkout, and removes the files they replace. --check changes nothing and exits 3 on a difference.

The manifest and the backups live in %LOCALAPPDATA%\\llm-wiki-raw-layer\\agentkit, never in the checkout.

Exit codes: 0 ok / current | 3 needs attention | 2 refused or usage | 1 error or interrupted."""
import argparse
import datetime
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
_LEGACY = re.compile(r"# llm-wiki-raw-layer[ \t]*")
_H1 = re.compile(r"# \S")
BOM = b"\xef\xbb\xbf"
CLAUDE_MD = ".claude/CLAUDE.md"
MCP_NAME = "llm_wiki_raw"
MCP_TOOLS = ("raw_status", "raw_aggregate", "raw_search", "raw_read", "raw_help")
MCP_SERVER = PROJECT / "tools" / "mcp_rawsearch.py"
# (template, kind, path): kind picks the frontmatter keys the file keeps (FRONT)
GLOBAL_FILES = (("raw-search.md", "skill", ".claude/skills/raw-search/SKILL.md"),
                ("llm-wiki-setup.md", "skill", ".claude/skills/llm-wiki-setup/SKILL.md"),
                ("raw-retriever.md", "agent", ".claude/agents/raw-retriever.md"))
REPO_FILES = GLOBAL_FILES + (("raw-search.md", "plain", ".agents/skills/raw-search/SKILL.md"),
                             ("raw-retriever.md", "plain", ".agents/skills/raw-retriever/SKILL.md"),
                             ("llm-wiki-setup.md", "plain", ".agents/skills/llm-wiki-setup/SKILL.md"))
FRONT = {"skill": ("name", "description", "argument-hint", "allowed-tools"),
         "agent": ("name", "description", "tools", "model"),
         "plain": ("name", "description")}
OBSOLETE_REPO = (".claude/commands/raw-search.md", ".claude/agents/knowledge-retriever.md",
                 ".agents/skills/knowledge-retriever/SKILL.md")
LEGACY_FILES = (".claude/agents/knowledge-retriever.md",)
# Backup keys. A key without EDITED holds an original from before install (a foreign or legacy item), which
# --restore puts back; key + EDITED holds a copy of this tool's own item as changed by hand, kept for reference only.
EDITED = "#edited"
SECTION_KEY = CLAUDE_MD + "#section"
BLOCK_KEY = CLAUDE_MD + "#block"
BLOCK_KEYS = (SECTION_KEY, BLOCK_KEY, BLOCK_KEY + EDITED)
MCP_KEYS = ("mcp", "mcp" + EDITED)
_VAR = re.compile(r"\{\{(include:)?([A-Za-z0-9_.-]+)\}\}")


class Refused(Exception):
    """A usage or precondition refusal: exit 2."""


def _mask(text):
    from tools import bootstrap  # noqa: PLC0415 -- one masking for both tools
    return bootstrap.mask(text)


def _out(text=""):
    print(_mask(text))


def _err(text):
    print(_mask(f"agents: {text}"), file=sys.stderr)


def _sha(text):
    # newlines are not content: CRLF or LF, or a final newline an editor dropped (the block often ends the file)
    return hashlib.sha256(text.replace("\r\n", "\n").rstrip("\n").encode("utf-8")).hexdigest()


def _stamp():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


# ---------------------------------------------------------------------------------------------------- rendering

def apps():
    from rawlayer import registry  # noqa: PLC0415
    return list(registry.discover())


def values(target, found):
    """The template variables of a target: "global" (absolute paths, any folder) or "repo" (the checkout)."""
    mcp = ", ".join(f"mcp__{MCP_NAME}__{t}" for t in MCP_TOOLS)
    if target == "global":
        posix = PROJECT.as_posix()
        run = f'python -s -B "{posix}/manage.py"'
        return {"PROJECT": str(PROJECT), "APPS": ", ".join(found), "RUN": run, "MCP_TOOLS": mcp,
                "BOOT": f'python -s -B "{posix}/tools/bootstrap.py"', "DOC": f"{posix}/", "WHERE": "from any folder",
                "BASH_SEARCH": f"Bash({run} search:*)"}
    run = "python -s -B manage.py"
    return {"APPS": ", ".join(found), "RUN": run, "MCP_TOOLS": mcp, "BOOT": "python -s -B tools/bootstrap.py",
            "DOC": "", "WHERE": "from the repository root (the folder holding manage.py)",
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


def find_legacy(text, block_span=None):
    """(start, end) of a hand-made "# llm-wiki-raw-layer" section: from that heading to the next level-1 heading
    outside a code fence, the marked block, or the end. None when there is none."""
    pos, start, fence = 0, None, False
    for ln in text.splitlines(keepends=True):
        bare = ln.rstrip("\r\n")
        inside = block_span is not None and block_span[0] <= pos < block_span[1]
        if start is not None and (inside or (not fence and _H1.match(bare))):
            return start, pos
        if inside:
            pos += len(ln)
            continue
        if bare.lstrip().startswith("```"):
            fence = not fence
        elif not fence and start is None and _LEGACY.fullmatch(bare):
            start = pos
        pos += len(ln)
    return (start, len(text)) if start is not None else None


# ---------------------------------------------------------------------------------------------------- the target

def state_dir():
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "llm-wiki-raw-layer" / "agentkit"


def _same_mcp(a, b):
    def norm(s):
        return (s.get("type", "stdio"), os.path.normcase(str(s.get("command", ""))),
                [str(x) for x in s.get("args") or []])
    return isinstance(a, dict) and isinstance(b, dict) and norm(a) == norm(b)


def _points_here(s):
    """True when an MCP entry runs this checkout's server."""
    here = os.path.normcase(str(MCP_SERVER))
    return isinstance(s, dict) and any(os.path.normcase(str(x)) == here for x in s.get("args") or [])


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


def _named_only(found):
    """The apps the MCP server searches only when a call names them: the off-limits ones (kakaotalk)."""
    from tools import bootstrap  # noqa: PLC0415
    return [a for a in found if a in bootstrap.OFF_LIMITS]


class Kit:
    """Claude Code's configuration in `home` (its .claude/ folder and .claude.json), with the manifest and backups
    in `state`. `claude(argv) -> rc` runs the claude CLI; `found` are the apps the MCP server may search; `python`
    is the MCP server's interpreter (None: the one it was installed with while that is still there, else this one)."""

    def __init__(self, home=None, state=None, python=None, claude=None, found=None):
        self.home = Path(home) if home else Path.home()
        self.state = Path(state) if state else state_dir()
        self.claude = claude or _claude_cli
        self.found = list(found if found is not None else apps())
        self.vals = values("global", self.found)
        self.manifest = self._load()
        self._settle_pending()
        self.python = self._interpreter(python)
        self.problems = []              # status: why an item is unreadable
        self.notes = []                 # uninstall: where saved copies are, what was not put back
        self.unrestored = []

    def _interpreter(self, python):
        if python:
            return os.path.abspath(python)
        recorded = (self.manifest.get("mcp") or {}).get("command")
        if isinstance(recorded, str) and recorded:
            from rawlayer import schedule  # noqa: PLC0415
            if schedule.python_problem(recorded) is None:
                return recorded
        return os.path.abspath(sys.executable)

    def _settle_pending(self):
        """An add-json recorded as pending was cut off (an interrupt, or a kill before it could record the outcome):
        its entry is this tool's only when it landed."""
        pending = self.manifest.get("mcp_pending")
        if pending is None:
            return
        if _same_mcp(self.mcp_now(), pending):
            self.manifest["mcp"] = pending
        self.manifest["mcp_pending"] = None
        self._save()

    # ---- the manifest
    def _load(self):
        try:
            m = json.loads((self.state / "manifest.json").read_text(encoding="utf-8"))
            if isinstance(m, dict) and m.get("v") == 1:
                return {**self._empty(), **m}
        except (OSError, ValueError):
            pass
        return self._empty()

    @staticmethod
    def _empty():
        # files: path -> sha256 written; block: {sha256, before, after, created, replaces}; mcp: the entry registered;
        # mcp_pending: an entry being registered; backups: key -> backup file; dirs: folders install created
        return {"v": 1, "project": None, "files": {}, "block": None, "mcp": None, "mcp_pending": None,
                "backups": {}, "dirs": []}

    def _save(self):
        self.state.mkdir(parents=True, exist_ok=True)
        tmp = self.state / "manifest.json.part"
        tmp.write_text(json.dumps(self.manifest, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        tmp.replace(self.state / "manifest.json")

    def _backup(self, key, data, stamp):
        """Save `data` (bytes) in a backup file of its own, never overwritten, and point `key` at it. Install backs up
        only what it does not own under a restorable key, so the newest such backup is the original to put back."""
        folder = self.state / "backups" / stamp
        folder.mkdir(parents=True, exist_ok=True)
        base, n = re.sub(r"[^A-Za-z0-9._-]", "_", key), 0
        while True:
            p = folder / (base if n == 0 else f"{base}.{n}")
            try:
                with open(p, "xb") as f:
                    f.write(data)
                break
            except FileExistsError:
                n += 1
        self.manifest["backups"][key] = str(p)
        self._save()
        return p

    def _restored(self, key):
        """The backup of `key` as bytes, or None."""
        p = self.manifest["backups"].get(key)
        try:
            return Path(p).read_bytes() if p else None
        except (OSError, ValueError):
            return None

    def _restored_text(self, key):
        data = self._restored(key)
        return None if data is None else data.decode("utf-8", "replace")

    def _used(self, key):
        """`key`'s backup was put back: it is done with."""
        self.manifest["backups"].pop(key, None)
        self._save()

    def _forget(self, *keys):
        """Drop the backups of an item that left the manifest; an original that was not put back is counted."""
        for key in keys:
            p = self.manifest["backups"].pop(key, None)
            if p and not key.endswith(EDITED):
                self.unrestored.append(p)
        self._save()

    def _cli(self, argv):
        rc = self.claude(argv)
        if rc != 0:
            raise RuntimeError(f"claude {' '.join(argv[:2])} {MCP_NAME} failed (rc={rc})")

    # ---- the wanted state
    def files(self):
        return {rel: artifact(t, kind, self.vals) for t, kind, rel in GLOBAL_FILES}

    def mcp_spec(self):
        args = ["-s", "-B", str(MCP_SERVER), "--apps", ",".join(self.found)]
        named = _named_only(self.found)
        if named:
            args += ["--named-only", ",".join(named)]
        return {"type": "stdio", "command": self.python, "args": args}

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

    def mcp_state(self):
        now = self.mcp_now()
        if now is None:
            return "absent"
        if _same_mcp(now, self.mcp_spec()):
            return "current"
        if self.manifest["mcp"] is None:
            return "foreign"
        return "stale" if _same_mcp(now, self.manifest["mcp"]) else "modified"

    def status(self):
        self.problems = []
        try:
            st, text, span, _bom = self.block_state()
        except Refused as e:
            st, text, span = "unreadable", None, None
            self.problems.append(str(e))
        rows = {f"{CLAUDE_MD} (block)": st}
        if text is not None and find_legacy(text, span):
            rows[f"{CLAUDE_MD} (hand-made section)"] = "legacy"
        for rel, want in self.files().items():
            rows[rel] = self.file_state(rel, want)
        for rel in LEGACY_FILES:
            if (self.home / rel).is_file():
                rows[rel] = "legacy"
        rows[f"mcp {MCP_NAME}"] = self.mcp_state()
        project = self.manifest["project"]
        mismatch = project is not None and os.path.normcase(project) != os.path.normcase(str(PROJECT))
        return rows, mismatch

    # ---- install
    def plan_install(self, adopt=False, force=False):
        """[(label, old, new, apply)] -- old/new texts for the diff (None: no such file), apply(stamp) does it."""
        plan, refusals = [], []
        st, text, span, bom = self.block_state()
        legacy = find_legacy(text, span) if text is not None else None
        nl = newline_of(text)
        want = block(self.vals, nl)
        if legacy and not adopt:
            refusals.append(f"{CLAUDE_MD}: a hand-made '# llm-wiki-raw-layer' section is there; review "
                            "`install --dry-run --adopt-legacy`, then install with --adopt-legacy")
        elif legacy and span is None:
            # the section becomes the block, in its place
            start, end = legacy
            after = nl if text[end:] else ""
            new = text[:start] + want + after + text[end:]

            def apply_section(stamp, start=start, end=end, after=after, new=new):
                self._backup(SECTION_KEY, text[start:end].encode("utf-8"), stamp)
                self._write_block(new, bom, before="", after=after, created=False, replaces=SECTION_KEY)
            plan.append((CLAUDE_MD, text, new, apply_section))
        elif legacy:
            refusals.append(f"{CLAUDE_MD}: both the block and a hand-made section are there; remove the section by "
                            "hand")
        if not legacy and st == "absent":
            created = text is None
            before = "" if not text else ("" if text.endswith(("\n", "\r")) else nl) + nl
            new = (text or "") + before + want

            def apply_append(stamp, new=new, before=before, created=created):
                # a backup of what an earlier block replaced is left only when that block was removed by hand: its
                # place is gone, so --restore no longer puts it back
                for key in (SECTION_KEY, BLOCK_KEY):
                    if key in self.manifest["backups"]:
                        self.notes.append(f"the block had been removed from {CLAUDE_MD} by hand, so uninstall --restore "
                                          f"no longer puts back what it first replaced; that is saved at "
                                          f"{self.manifest['backups'][key]}")
                for key in BLOCK_KEYS:
                    self.manifest["backups"].pop(key, None)
                self._write_block(new, bom, before=before, after="", created=created)
            plan.append((CLAUDE_MD, text, new, apply_append))
        elif not legacy and (st == "stale" or (st in ("modified", "foreign") and force)):
            old_block = text[span[0]:span[1]]
            new = text[:span[0]] + want + text[span[1]:]
            prev = self.manifest["block"] or {}

            def apply_replace(stamp, new=new, old_block=old_block, st=st, prev=prev):
                if st != "stale":       # foreign: the original; modified: this tool's block as changed by hand
                    self._backup(BLOCK_KEY if st == "foreign" else BLOCK_KEY + EDITED, old_block.encode("utf-8"), stamp)
                self._write_block(new, bom, before=prev.get("before", ""), after=prev.get("after", ""),
                                  created=prev.get("created", False),
                                  replaces=BLOCK_KEY if st == "foreign" else prev.get("replaces"))
            plan.append((CLAUDE_MD, text, new, apply_replace))
        elif not legacy and st in ("modified", "foreign"):
            refusals.append(f"{CLAUDE_MD}: the block is {st}; --force replaces it (backed up)")

        for rel, body in self.files().items():
            fst = self.file_state(rel, body)
            if fst in ("absent", "stale") or (fst in ("modified", "foreign") and force):
                plan.append((rel, _shown(self.home / rel), body,
                             lambda stamp, rel=rel, body=body, fst=fst: self._write_file(rel, body, fst, stamp)))
            elif fst in ("modified", "foreign"):
                refusals.append(f"{rel}: {fst}; --force replaces it (backed up)")
        for rel in LEGACY_FILES:
            if (self.home / rel).is_file():
                if adopt:
                    plan.append((rel, _shown(self.home / rel), None,
                                 lambda stamp, rel=rel: self._remove_legacy(rel, stamp)))
                else:
                    refusals.append(f"{rel}: legacy (a hand-made copy); --adopt-legacy removes it (backed up)")

        ms = self.mcp_state()
        now = self.mcp_now()
        if ms in ("absent", "stale") or (ms in ("modified", "foreign") and force):
            plan.append((f"mcp {MCP_NAME} (claude mcp add-json --scope user)",
                         json.dumps(now, indent=1) + "\n" if now else None,
                         json.dumps(self.mcp_spec(), indent=1) + "\n",
                         lambda stamp, ms=ms, now=now: self._add_mcp(ms, now, stamp)))
        elif ms == "modified":
            refusals.append(f"mcp {MCP_NAME}: changed by hand; --force replaces it (backed up)")
        elif ms == "foreign":
            refusals.append(f"mcp {MCP_NAME}: registered with another command; --force replaces it (backed up)")
        if refusals:
            raise Refused("; ".join(refusals))
        return plan

    def install(self, dry_run=False, adopt=False, force=False):
        self.notes = []
        if not dry_run and self.mcp_state() != "current" and not MCP_SERVER.is_file():
            raise Refused("tools/mcp_rawsearch.py is not in this checkout")
        plan = self.plan_install(adopt=adopt, force=force)
        if dry_run:
            return plan
        if plan:
            self.manifest["project"] = str(PROJECT)
            self._save()
        stamp = _stamp()
        for _label, _old, _new, apply in plan:
            apply(stamp)
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

    def _write_block(self, new, bom, before, after, created, replaces=None):
        """`replaces`: the backup key of what the block was written in place of (a section, a foreign block), the
        only one uninstall --restore puts back in its place."""
        self._mkdirs(CLAUDE_MD)
        write_doc(self.home / CLAUDE_MD, new, bom)
        self.manifest["block"] = {"sha256": _sha(block(self.vals)), "before": before, "after": after,
                                  "created": created, "replaces": replaces}
        self._save()

    def _write_file(self, rel, body, fst, stamp):
        p = self.home / rel
        if fst in ("modified", "foreign"):
            self._backup(rel if fst == "foreign" else rel + EDITED, p.read_bytes(), stamp)
        self._mkdirs(rel)
        write_doc(p, body)
        self.manifest["files"][rel] = _sha(body)
        self._save()

    def _remove_legacy(self, rel, stamp):
        p = self.home / rel
        self._backup(rel, p.read_bytes(), stamp)
        p.unlink()

    def _add_mcp(self, ms, now, stamp):
        spec = self.mcp_spec()
        add = ["mcp", "add-json", "--scope", "user", MCP_NAME]
        backups = dict(self.manifest["backups"])
        if now is not None and ms in ("foreign", "modified"):
            # an unrecorded entry of this server's never replaces an original already backed up
            ours = ms == "modified" or ("mcp" in backups and _points_here(now))
            self._backup("mcp" + EDITED if ours else "mcp", json.dumps(now, ensure_ascii=False).encode("utf-8"), stamp)
        self.manifest["mcp_pending"] = spec     # a kill during the add: the next run records it only if it landed
        self._save()
        try:
            if now is not None:
                self._cli(["mcp", "remove", "--scope", "user", MCP_NAME])
            self._cli([*add, json.dumps(spec, ensure_ascii=False)])
        except BaseException:
            self._settle_pending()              # an add that landed before an interrupt is recorded
            if self.manifest["mcp"] is not spec and now is not None:
                if self.mcp_now() is None:      # removed, not added: put it back
                    try:
                        self.claude([*add, json.dumps(now, ensure_ascii=False)])
                    except Exception:  # noqa: BLE001 -- the first failure is the one to report
                        pass
                if _same_mcp(self.mcp_now(), now):  # back as it was: so are the backups (a failed add changes nothing)
                    self.manifest["backups"] = backups
                    self._save()
            raise
        self.manifest["mcp"], self.manifest["mcp_pending"] = spec, None
        self._save()

    # ---- uninstall
    def plan_uninstall(self, restore=False, force=False):
        """([(label, old, new, apply)], kept, tidy): apply(stamp) does a step; tidy steps only update the manifest
        for items already gone by hand."""
        plan, kept, tidy = [], [], []
        backups = self.manifest["backups"]
        st, text, span, bom = self.block_state()
        if span is not None and (st in ("current", "stale") or force):
            info = self.manifest["block"] or {}
            start, end = span
            # only what this very block was written in place of goes back in its place
            section = self._restored_text(SECTION_KEY) if restore and info.get("replaces") == SECTION_KEY else None
            old_block = self._restored_text(BLOCK_KEY) if restore and info.get("replaces") == BLOCK_KEY else None
            if old_block is not None:
                new = text[:start] + old_block + text[end:]
            else:
                before, after = info.get("before", ""), info.get("after", "")
                if before and text[max(0, start - len(before)):start] == before:
                    start -= len(before)
                if after and text[end:end + len(after)] == after:
                    end += len(after)
                new = text[:start] + (section or "") + text[end:]
            gone = new == "" and info.get("created")
            used = SECTION_KEY if section is not None else BLOCK_KEY if old_block is not None else None
            mine = None if st in ("current", "stale") else text[span[0]:span[1]]

            def apply_block(stamp, new=new, gone=gone, used=used, mine=mine, st=st):
                if mine is not None:
                    saved = self._backup(BLOCK_KEY + EDITED, mine.encode("utf-8"), stamp)
                    self.notes.append(f"the block removed from {CLAUDE_MD} ({st}) is saved at {saved}")
                p = self.home / CLAUDE_MD
                if gone:
                    p.unlink()
                else:
                    write_doc(p, new, bom)
                self.manifest["block"] = None
                if used:
                    self._used(used)
                self._forget(*BLOCK_KEYS)
            plan.append((CLAUDE_MD, text, None if gone else new, apply_block))
        elif span is not None:
            kept.append(f"{CLAUDE_MD} (block {st})")
        elif self.manifest["block"] is not None or any(k in backups for k in BLOCK_KEYS):
            # the block was removed by hand: its original place is unknown, so a section is not put back blindly
            lost = backups.get(SECTION_KEY) or backups.get(BLOCK_KEY)

            def tidy_block(lost=lost):
                if restore and lost:
                    self.notes.append(f"the block is no longer in {CLAUDE_MD}, so what it replaced was not put back; "
                                      f"it is saved at {lost}")
                self.manifest["block"] = None
                self._forget(*BLOCK_KEYS)
            tidy.append(tidy_block)

        for rel, body in self.files().items():
            fst = self.file_state(rel, body)
            back = self._restored(rel) if restore else None
            shown = None if back is None else back.decode("utf-8", "replace")
            if fst in ("current", "stale") or (fst == "modified" and force):
                def apply_file(stamp, rel=rel, back=back, edited=fst == "modified"):
                    p = self.home / rel
                    if edited:
                        saved = self._backup(rel + EDITED, p.read_bytes(), stamp)
                        self.notes.append(f"{rel} (changed by hand) is saved at {saved}")
                    if back is None:
                        p.unlink()
                    else:
                        write_raw(p, back)
                        self._used(rel)
                    self.manifest["files"].pop(rel, None)
                    self._forget(rel, rel + EDITED)
                plan.append((rel, _shown(self.home / rel), shown, apply_file))
            elif fst == "modified":
                kept.append(f"{rel} (modified)")
            elif fst == "absent" and back is not None:
                def apply_back(stamp, rel=rel, back=back):
                    p = self.home / rel
                    p.parent.mkdir(parents=True, exist_ok=True)
                    write_raw(p, back)
                    self._used(rel)
                    self.manifest["files"].pop(rel, None)
                    self._forget(rel + EDITED)
                plan.append((rel, None, shown, apply_back))
            elif fst == "absent" and (rel in self.manifest["files"] or rel in backups):
                def tidy_file(rel=rel):
                    self.manifest["files"].pop(rel, None)
                    self._forget(rel, rel + EDITED)
                tidy.append(tidy_file)
        if restore:
            for rel in LEGACY_FILES:
                back = self._restored(rel)
                if back is not None and not (self.home / rel).exists():
                    def apply_legacy(stamp, rel=rel, back=back):
                        (self.home / rel).parent.mkdir(parents=True, exist_ok=True)
                        write_raw(self.home / rel, back)
                        self._used(rel)
                    plan.append((rel, None, back.decode("utf-8", "replace"), apply_legacy))

        ms, now = self.mcp_state(), self.mcp_now()
        back, shown = self._restored("mcp") if restore else None, None
        if back is not None:
            if _is_json(back):
                shown = json.dumps(json.loads(back), indent=1) + "\n"
            else:
                back = None             # an unreadable backup is never handed to the CLI
        if ms in ("current", "stale") or (ms == "modified" and force):
            plan.append((f"mcp {MCP_NAME} (claude mcp remove --scope user)", json.dumps(now, indent=1) + "\n",
                         shown, lambda stamp, back=back, edited=ms == "modified": self._remove_mcp(now, back, edited,
                                                                                                  stamp)))
        elif ms == "modified":
            kept.append(f"mcp {MCP_NAME} (modified)")
        elif ms == "absent" and back is not None:
            plan.append((f"mcp {MCP_NAME} (claude mcp add-json --scope user: the entry install replaced)", None, shown,
                         lambda stamp, back=back: self._readd_mcp(back)))
        elif ms == "absent" and (self.manifest["mcp"] is not None or "mcp" in backups):
            def tidy_mcp():
                self.manifest["mcp"] = None
                self._forget(*MCP_KEYS)
            tidy.append(tidy_mcp)
        return plan, kept, tidy

    def uninstall(self, dry_run=False, restore=False, force=False):
        plan, kept, tidy = self.plan_uninstall(restore=restore, force=force)
        if dry_run:
            return plan, kept
        self.notes, self.unrestored = [], []
        stamp = _stamp()
        try:
            for _label, _old, _new, apply in plan:
                apply(stamp)
            for step in tidy:
                step()
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
                # nothing of this tool's is left: the install cycle is over, and so are its backups
                self.manifest["project"] = None
                self._forget(*list(self.manifest["backups"]))
        finally:                        # a failed step still reports what the steps before it saved or forgot
            if self.unrestored:
                self.notes.append(f"{len(self.unrestored)} backup(s) of what install replaced were not put back"
                                  f"{'' if restore else ' (--restore was not given)'} and are forgotten; their files "
                                  f"stay under {self.state / 'backups'}")
            self._save()
        return plan, kept

    def _remove_mcp(self, now, back, edited, stamp):
        if edited:
            saved = self._backup("mcp" + EDITED, json.dumps(now, ensure_ascii=False).encode("utf-8"), stamp)
            self.notes.append(f"the {MCP_NAME} entry (changed by hand) is saved at {saved}")
        self._cli(["mcp", "remove", "--scope", "user", MCP_NAME])
        self.manifest["mcp"] = None
        self._save()
        if back is not None:            # on a failure the backup stays recorded: the next --restore puts it back
            self._readd_mcp(back)
        else:
            self._forget(*MCP_KEYS)

    def _readd_mcp(self, back):
        self._cli(["mcp", "add-json", "--scope", "user", MCP_NAME, back.decode("utf-8")])
        self.manifest["mcp"] = None
        self._used("mcp")
        self._forget(*MCP_KEYS)


def _is_json(data):
    try:
        return isinstance(json.loads(data.decode("utf-8")), dict)
    except ValueError:
        return False


# ---------------------------------------------------------------------------------------------------- the checkout

def repo_plan(found=None):
    """[(action, path, text)]: what sync-repo writes ("write") or removes ("remove") in the checkout."""
    vals = values("repo", found if found is not None else apps())
    plan = []
    for template, kind, rel in REPO_FILES:
        want = artifact(template, kind, vals)
        p = PROJECT / rel
        cur = p.read_bytes().decode("utf-8", "replace").replace("\r\n", "\n") if p.is_file() else None
        if cur != want:
            plan.append(("write", rel, want))
    for rel in OBSOLETE_REPO:
        if (PROJECT / rel).exists():
            plan.append(("remove", rel, None))
    return plan


def sync_repo(check):
    plan = repo_plan()
    for action, rel, _text in plan:
        _out(f"agents: sync-repo: {'would ' if check else ''}{action} {rel}")
    if check:
        if not plan:
            _out("agents: sync-repo: the checkout's agent files match the templates")
        return 3 if plan else 0
    for action, rel, text in plan:
        p = PROJECT / rel
        if action == "write":
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(text.encode("utf-8"))
        else:
            p.unlink()
            for d in (p.parent, p.parent.parent):
                if d != PROJECT and d.is_dir() and not any(d.iterdir()):
                    d.rmdir()
    _out(f"agents: sync-repo: {len(plan)} change(s)" if plan else "agents: sync-repo: nothing to change")
    return 0


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
    i.add_argument("--adopt-legacy", action="store_true")
    i.add_argument("--force", action="store_true")
    i.add_argument("--python", help="the interpreter the MCP server runs with (default: the one it was installed "
                                    "with, while that is still there, else this one)")
    u =sub.add_parser("uninstall")
    u.add_argument("--dry-run", action="store_true")
    u.add_argument("--restore", action="store_true")
    u.add_argument("--force", action="store_true")
    r = sub.add_parser("sync-repo")
    r.add_argument("--check", action="store_true")
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
    k = None
    try:
        if a.command == "sync-repo":
            return sync_repo(a.check)
        k = kit or Kit(python=getattr(a, "python", None))
        if a.command == "install" and kit is None:
            from rawlayer import schedule  # noqa: PLC0415 -- the MCP server's interpreter, as the task's is checked
            problem = schedule.python_problem(k.python)
            if problem:
                raise Refused(f"the MCP server's interpreter: {problem}")
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
            plan = k.install(dry_run=a.dry_run, adopt=a.adopt_legacy, force=a.force)
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
        plan, kept = k.uninstall(dry_run=a.dry_run, restore=a.restore, force=a.force)
        if a.dry_run:
            _diff(plan)
        _show(plan, "would" if a.dry_run else "did")
        for x in kept:
            _out(f"agents: kept {x}: changed by hand or not written by this tool (`uninstall"
                 f"{' --restore' if a.restore else ''} --force` removes it, saving it first)")
        if not plan and not kept:
            _out("agents: nothing installed")
        return 3 if kept else 0
    except Refused as e:
        _err(f"refused: {e}")
        return 2
    except KeyboardInterrupt:
        _err("interrupted; `agents status` shows what was applied")
        return 1
    except Exception as e:  # noqa: BLE001 -- never a traceback: it would print unmasked paths
        _err(f"error {type(e).__name__}: {e}")
        return 1
    finally:                            # also after a failed step: where saved copies are, what was forgotten
        for n in (k.notes if k is not None and a.command in ("install", "uninstall") else []):
            _out(f"agents: {n}")
