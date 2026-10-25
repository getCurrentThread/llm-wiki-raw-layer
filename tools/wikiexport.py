# -*- coding: utf-8 -*-
"""python -s -B <PROJECT>/tools/wikiexport.py <app> [--config CFG] [--project DIR] [--include S ...] [--exclude S ...]
       [--since YYYY-MM] [--dry-run] [--prune]

Feeds an app's raw ledger to an LLM Wiki project (nashsu/llm_wiki, llm.md 13): renders the app's search index as
Markdown sources under <project>/raw/sources/<app>/, which the desktop app watches and ingests. Standard library
only; the index is read as `search` reads it, through the driver's chunk hooks (SearchDriver.changed_containers and
chunk_units: the units dense.py embeds, without deleted, system or empty rows).

Files
  chat (discord, mattermost,  per channel  <app>/<label> (<h8>)/<YYYY-MM>.md, a "## <day>" heading per day
        kakaotalk)              or room
  mail (gmail)                             gmail/<YYYY-MM>.md, a "## <subject / sender> (<date>)" section per mail
  page (notion)               per page     notion/<breadcrumb> (<h8>).md, headings kept
<h8> is a hash of the container id; the frontmatter holds the id itself. A chat or mail month is written once it is
over (+09:00): the open month would change, and be ingested again in full, on every collect. Closed months, oldest
first, fill a file up to PACK characters, and <YYYY-MM> is the file's first month: the newest file of a stream grows
(and is ingested again) as months close, until it is full; a file before it changes only when its records do.

The config's <p>_wiki_include and <p>_wiki_exclude (substrings of the container label, casefolded; an empty include
keeps all, an exclude wins) and <p>_wiki_since (YYYY-MM: a chat or mail file stays when its newest month is not
before it, a page by its newest unit) narrow what is written. --project, --include, --exclude and --since replace
them, and are the only way to give kakaotalk's (its app folder declares no wiki keys).

Every run renders everything and rewrites only the files whose bytes changed (tmp + replace), so the wiki's
SHA-256 ingest cache stays valid. The tool owns <project>/raw/sources/<app>/: a file there that the run did not
produce (a renamed channel, a narrowed include, a purge) is counted as an orphan and kept, because deleting a source
deletes its wiki pages; only --prune removes them. Output is counts only, never a label, a path or a text.

Exit codes: 0 ok | 2 refused (config, project, app, index) | 75 busy | 1 error."""
import argparse
import hashlib
import json
import os
import re
import sys
import time
import uuid
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
if str(PROJECT) not in sys.path:
    sys.path.insert(0, str(PROJECT))

from rawlayer.registry import Option  # noqa: E402
from rawlayer.search import common  # noqa: E402
from tools.archiveio import identifiers, inside, text  # noqa: E402

# app -> (config key prefix, layout); kakaotalk/ is not edited, so it declares no keys: its settings come from the
# command line only
APPS = {"discord": ("dc", "chat"), "mattermost": ("mm", "chat"), "gmail": ("gm", "mail"), "notion": ("nt", "page"),
        "kakaotalk": (None, "chat")}
TZ = "+09:00"
NAME_MAX = 80
# characters of closed months per chat/mail file: LLM Wiki pays an analysis and a generation pass per file, and its
# long-source analysis reads 12k-60k character chunks; most channel months alone are far smaller (median ~800)
PACK = 16000
_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')
_MONTH = re.compile(r"[0-9]{4}-(0[1-9]|1[0-2])")


class Refused(Exception):
    """exit 2"""


def _project(value):
    if text(value) and not os.path.isabs(value):
        raise ValueError("wiki project: an absolute path")
    return value


def _since(value):
    if text(value) and not _MONTH.fullmatch(value):
        raise ValueError("wiki since: YYYY-MM")
    return value


def options(prefix):
    """The config keys an app declares for its export (AppConfig.options): the project root ('' = off), the label
    substrings to keep and to leave out, and the first month."""
    return (Option(f"{prefix}_wiki_project", "", _project), Option(f"{prefix}_wiki_include", [], identifiers),
            Option(f"{prefix}_wiki_exclude", [], identifiers), Option(f"{prefix}_wiki_since", "", _since))


def settings(cfg, prefix, project=None, include=None, exclude=None, since=None):
    """(project, include, exclude, since): each from the command line when given, else from the config's
    <prefix>_wiki_* keys (none for an app without a prefix)."""
    def get(key, default):
        return cfg.get(f"{prefix}_wiki_{key}", default) if prefix else default
    try:
        return (_project(get("project", "") if project is None else project),
                identifiers(get("include", []) if include is None else include),
                identifiers(get("exclude", []) if exclude is None else exclude),
                _since(get("since", "") if since is None else since))
    except ValueError as e:
        raise Refused(f"wiki settings: {e}") from None


def name(label, container):
    """A file or folder name: the label made safe for Windows and cut, plus a hash of the container id (no two
    containers share a name, and no name is a reserved device name)."""
    safe = " ".join(_UNSAFE.sub("_", label).split())[:NAME_MAX].rstrip(" .") or "untitled"
    return f"{safe} ({hashlib.sha256(str(container).encode('utf-8')).hexdigest()[:8]})"


def target(project, data_parent, key="<p>_wiki_project"):
    """The project root: set, under the protected data parent, and an LLM Wiki project (schema.md and wiki/)."""
    if not project:
        raise Refused(f"{key} is not set (llm.md 13)")
    p = Path(project)
    if not p.resolve().is_relative_to(Path(data_parent).resolve()):
        raise Refused("the wiki project is not under the data parent")
    if not ((p / "schema.md").is_file() and (p / "wiki").is_dir()):
        raise Refused("the wiki project has no schema.md and wiki/: create it in LLM Wiki first")
    return p


def plan(kind, items, tz, include, since, now_month, exclude=()):
    """({relative path: [(container, label, units)]}, records held in open months, records filtered out)."""
    files, streams, held, filtered = {}, {}, 0, 0
    for c, label, units in items:
        if not units:
            continue
        folded = label.casefold()
        keep = (not include or any(s.casefold() in folded for s in include)) and \
            not any(s.casefold() in folded for s in exclude)
        if kind == "page":
            parts = {common.fmt_ts(max(u[1] for u in units), tz, "%Y-%m"): units}
        else:
            parts = {}
            for u in units:
                parts.setdefault(common.fmt_ts(u[1], tz, "%Y-%m"), []).append(u)
        for period, part in parts.items():
            if not keep or (kind == "page" and since and period < since):
                filtered += len(part)
            elif kind == "page":
                files[f"{name(label, c)}.md"] = [(c, label, part)]
            elif period >= now_month:
                held += len(part)
            else:               # a stream: one channel (chat), or every mail (mail)
                stream = name(label, c) + "/" if kind == "chat" else ""
                streams.setdefault(stream, {}).setdefault(period, []).append((c, label, part))
    # a stream's closed months, oldest first, fill a file until it holds PACK characters; the file is named after its
    # first month, so a file keeps its name while the months after it close. since drops whole files (those whose
    # newest month is before it), so moving it never re-packs the files it keeps.
    for stream, months in streams.items():
        packs, size = [], PACK
        for period in sorted(months):
            if size >= PACK:
                packs.append([period, period, []])
                size = 0
            packs[-1][1] = period
            packs[-1][2].extend(months[period])
            size += sum(len(u[2]) for _, _, us in months[period] for u in us)
        for first, last, entries in packs:
            if since and last < since:
                filtered += sum(len(us) for _, _, us in entries)
            else:
                files[f"{stream}{first}.md"] = entries
    return files, held, filtered


def _period(units, tz):
    first, last = (common.fmt_ts(f(u[1] for u in units), tz, "%Y-%m") for f in (min, max))
    return first if first == last else f"{first}..{last}"


def _front(**kv):
    # json.dumps of a str is a valid YAML double-quoted scalar
    return "---\n" + "".join(f"{k}: {json.dumps(v, ensure_ascii=False)}\n" for k, v in kv.items()) + "---\n\n"


def render(app, kind, key, entries, tz):
    """One source file's Markdown."""
    units = [u for _, _, us in entries for u in us]
    span = dict(records=len({u[0] for u in units}), first=common.fmt_ts(min(u[1] for u in units), tz),
                last=common.fmt_ts(max(u[1] for u in units), tz))
    period = _period(units, tz)
    if kind == "mail":
        out = [_front(title=f"{app} {period}", source_app=app, period=period, **span), f"# {app} {period}\n"]
        for _, label, us in sorted(entries, key=lambda e: (e[2][0][1], str(e[0]))):
            out.append(f"\n## {' '.join(label.split())} ({common.fmt_ts(us[0][1], tz)})\n")
            out += [f"\n{u[2]}\n" for u in us]
        return "".join(out)
    c, label = entries[0][0], entries[-1][1]        # a chat file's months: one channel, its newest label
    if kind == "page":
        period = common.fmt_ts(max(u[1] for u in units), tz, "%Y-%m")
        out = [_front(title=label, source_app=app, container=str(c), period=period, **span), f"# {label}\n"]
        out += [f"\n## {' '.join(t.split())}\n" if brk else f"\n{t}\n" for _, _, t, brk in units]
        return "".join(out)
    out, day = [_front(title=f"{label} {period}", source_app=app, container=str(c), period=period, **span),
                f"# {label} {period}\n"], None
    for _, ts, t, _ in units:
        if common.fmt_ts(ts, tz, "%Y-%m-%d") != day:
            day = common.fmt_ts(ts, tz, "%Y-%m-%d")
            out.append(f"\n## {day}\n\n")
        out.append(f"- {common.fmt_ts(ts, tz, '%H:%M')} {t.strip().replace(chr(10), chr(10) + '  ')}\n")
    return "".join(out)


def write(root, path, data, dry_run=False):
    """True when the file's bytes change. tmp + replace, so the wiki's watcher never reads a half-written source;
    the tmp name starts with a dot, which the watcher skips."""
    path = inside(root, path)
    if path.is_file() and path.read_bytes() == data:
        return False
    if dry_run:
        return True
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with tmp.open("xb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        for attempt in range(10):
            try:
                os.replace(tmp, path)
                break
            except PermissionError:     # the wiki app may hold it open for a moment
                if attempt == 9:
                    raise
                time.sleep(0.2)
    finally:
        tmp.unlink(missing_ok=True)
    return True


def export(app, drv, con, project, include=(), since="", dry_run=False, prune=False, now=None, exclude=()):
    """Render every container of the app's index into <project>/raw/sources/<app>/ -> the report (counts)."""
    kind = APPS[app][1]
    tz = common.parse_tz(TZ)
    now_month = common.fmt_ts(time.time() if now is None else now, tz, "%Y-%m")
    items = ((c, *drv.chunk_units(con, c)) for c in drv.changed_containers(con, 0))
    files, held, filtered = plan(kind, items, tz, include, since, now_month, exclude)
    root = Path(project, "raw", "sources", app)
    rep = {"files": len(files), "written": 0, "unchanged": 0, "held": held, "filtered": filtered}
    for key, entries in sorted(files.items()):
        changed = write(root, root / key, render(app, kind, key, entries, tz).encode("utf-8"), dry_run)
        rep["written" if changed else "unchanged"] += 1
    produced = {(root / k).resolve() for k in files}
    orphans = [p for p in root.rglob("*.md") if p.resolve() not in produced] if root.is_dir() else []
    rep["orphans"] = len(orphans)
    if prune and not dry_run:
        for p in orphans:
            p.unlink()
            try:
                p.parent.rmdir()        # a renamed channel's folder, once empty
            except OSError:
                pass
        rep["pruned"] = len(orphans)
    return rep


def main(argv=None):
    from rawlayer import registry  # noqa: PLC0415
    from rawlayer.capture import paths  # noqa: PLC0415
    from rawlayer.management.base import default_config  # noqa: PLC0415
    ap = argparse.ArgumentParser(prog="tools/wikiexport.py", description=__doc__.split("\n\n")[1],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("app", choices=sorted(APPS))
    ap.add_argument("--config", help="the app's config (default <DATA_PARENT>/<app>/config.json)")
    ap.add_argument("--project", help="the LLM Wiki project root, instead of the config's <p>_wiki_project")
    ap.add_argument("--include", action="append", help="a label substring to keep (repeat), instead of <p>_wiki_include")
    ap.add_argument("--exclude", action="append",
                    help="a label substring to leave out (repeat), instead of <p>_wiki_exclude; it wins over --include")
    ap.add_argument("--since", help="the first month, YYYY-MM, instead of <p>_wiki_since")
    ap.add_argument("--dry-run", action="store_true", help="count what would be written, write nothing")
    ap.add_argument("--prune", action="store_true", help="delete the orphans (LLM Wiki deletes their pages too)")
    a = ap.parse_args(argv)
    common.setup_stdio()
    prefix = APPS[a.app][0]
    try:
        registry.select(a.app)
        config = a.config or default_config(a.app)
        data_root = common.load_data_root(config)
        with open(config, encoding="utf-8") as fh:
            cfg = json.load(fh)
        project, include, exclude, since = settings(cfg, prefix, a.project, a.include, a.exclude, a.since)
        root = target(project, paths.DATA_PARENT, f"{prefix}_wiki_project" if prefix else "--project")
        drv = dict(registry.search_modules())[a.app].DRIVER
        con, _meta, fresh = common.open_search(data_root, drv.spec)
        try:
            common.warn_if_stale(drv.spec, fresh)
            rep = export(a.app, drv, con, root, include=include, since=since, dry_run=a.dry_run, prune=a.prune,
                         exclude=exclude)
        finally:
            con.close()
    except (Refused, common.UsageError, registry.RegistryError) as e:
        print(f"wikiexport: {e}", file=sys.stderr)
        return common.EXIT_USAGE
    except common.Busy:
        print("wikiexport: the index is busy (being rebuilt); run again later", file=sys.stderr)
        return common.EXIT_BUSY
    except Exception as e:  # noqa: BLE001 -- the type only: a message can carry a path holding a label
        print(f"wikiexport: error {type(e).__name__}", file=sys.stderr)
        return common.EXIT_ERR
    print(f"wikiexport: {a.app}{' dry-run' if a.dry_run else ''} " + " ".join(f"{k}={v}" for k, v in rep.items()))
    return common.EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
