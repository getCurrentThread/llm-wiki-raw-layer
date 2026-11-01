# -*- coding: utf-8 -*-
"""python -s -B manage.py collect <app> [--config CFG] [--project DIR] [--include S ...] [--exclude S ...]
       [--since YYYY-MM] [--dry-run] [--prune]

The third pipeline step, source --fetch--> archive --capture--> ledger + index --collect--> LLM Wiki sources: renders
the app's index as Markdown sources of an LLM Wiki project (the layout and the settings are the app's AppConfig.wiki;
rawlayer/collect.py holds the logic and the output spec, llm.md 13 the runbook). It is where private data leaves the
raw layer (the wiki app may send sources to a cloud LLM): run it only when the owner asks, never from a scheduled
task, and never as LocalSystem (refused, exit 2, nothing read or written). --prune deletes wiki pages: only on the
owner's explicit yes.

Exit codes: 0 ok | 2 refused (config, project, app, index, LocalSystem) | 75 busy | 1 error."""
import argparse
import json
import sys

from ... import collect as C
from ... import registry
from ...capture import paths, publicout
from ...capture import run as capture_run
from ...capture.publicout import lit
from ...search import index
from ..base import BaseCommand, default_config


def main(argv=None):
    """argv: <app> [--config CFG] [--project DIR] [--include S ...] [--exclude S ...] [--since YYYY-MM] [--dry-run]
    [--prune] (the dispatcher has selected the app)."""
    if capture_run.is_system():     # a task registered with the pre-capture arguments (`collect <app> --config ...`)
        publicout.say(lit("collect refused: running as LocalSystem; run it as the owner's account (llm.md 13)"))
        return index.EXIT_USAGE
    ap = argparse.ArgumentParser(prog="manage.py collect", description=C.__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("app")
    ap.add_argument("--config", help="the app's config (default <DATA_PARENT>/<app>/config.json)")
    ap.add_argument("--project", help="the LLM Wiki project root, instead of the config's <p>_wiki_project")
    ap.add_argument("--include", action="append", help="a label substring to keep (repeat), instead of <p>_wiki_include")
    ap.add_argument("--exclude", action="append",
                    help="a label substring to leave out (repeat), instead of <p>_wiki_exclude; it wins over --include")
    ap.add_argument("--since", help="the first month, YYYY-MM, instead of <p>_wiki_since")
    ap.add_argument("--dry-run", action="store_true", help="count what would be written, write nothing")
    ap.add_argument("--prune", action="store_true", help="delete the orphans (LLM Wiki deletes their pages too)")
    a = ap.parse_args(argv)
    index.setup_stdio()
    try:
        app = registry.get().app(a.app)
        if app is None or app.wiki is None:
            raise C.Refused(f"{a.app} declares no wiki export (AppConfig.wiki in its apps.py)")
        prefix, kind = app.wiki
        drv = getattr(registry.search_modules().get(a.app), "DRIVER", None)
        if not hasattr(drv, "chunk_units"):
            raise C.Refused(f"{a.app} has no search driver that chunks its index (search.DRIVER.chunk_units)")
        config = a.config or default_config(a.app)
        data_root = index.load_data_root(config)
        with open(config, encoding="utf-8") as fh:
            cfg = json.load(fh)
        project, include, exclude, since = C.settings(cfg, prefix, a.project, a.include, a.exclude, a.since)
        root = C.target(project, paths.DATA_PARENT, f"{prefix}_wiki_project" if prefix else "--project")
        con, _meta, fresh = index.open_search(data_root, drv.spec)
        try:
            index.warn_if_stale(drv.spec, fresh)
            rep = C.export(a.app, kind, drv, con, root, include=include, since=since, dry_run=a.dry_run,
                           prune=a.prune, exclude=exclude, stale=fresh["stale"])
        finally:
            con.close()
    except (C.Refused, index.UsageError, registry.RegistryError) as e:
        print(f"collect: {e}", file=sys.stderr)
        return index.EXIT_USAGE
    except index.Busy:
        print("collect: the index is busy (being rebuilt); run again later", file=sys.stderr)
        return index.EXIT_BUSY
    except Exception as e:  # noqa: BLE001 -- the type only: a message can carry a path holding a label
        print(f"collect: error {type(e).__name__}", file=sys.stderr)
        return index.EXIT_ERR
    print(f"collect: {a.app}{' dry-run' if a.dry_run else ''} " + " ".join(f"{k}={v}" for k, v in rep.items()))
    return index.EXIT_OK


class Command(BaseCommand):
    help = "export an app's index as Markdown sources of an LLM Wiki project (owner only; --prune deletes wiki pages)"
    args = "[--config CFG] [--project DIR] [--include S ...] [--exclude S ...] [--since YYYY-MM] [--dry-run] [--prune]"
    app = "required"
    config = True

    def run(self, app, argv):
        return main([app] + argv)
