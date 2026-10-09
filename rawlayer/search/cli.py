# -*- coding: utf-8 -*-
"""The internal index/search CLI -- collect's index step runs `index`, and the search command runs `search`,
both in-process (rawlayer.collect, rawlayer.management.commands.search); it is not a manage.py command surface.

  index <app>     --config CFG [--rebuild] [--status] [--json]
                  bring <data_root>/index/<app>.sqlite up to the committed ledger (incremental; a full rebuild
                  when the index is missing, outdated, or the ledger was reset)
  search <app>    --config CFG [QUERY] ...    search the app's index (<app>/search.py; --help for its options)

The selected app's search.py is its source (rawlayer.registry); an app without one has no index. A data root
whose ledger belongs to another app is refused (exit 2). Exit codes: 0 ok (0 hits included) |
2 usage, bad query, missing or outdated index | 75 index lock busy | 1 unexpected error (also: a committed ledger
file the index needs cannot be read).
"""
import argparse
import sys

from .. import registry
from . import common
from .common import Busy, LedgerDefect, UsageError

_SEVERITY = {0: 0, 2: 1, 75: 2, 1: 3}


def _parser(mods):
    ap = common.Parser(prog="manage.py", description="Full-text search over the raw ledger, one index per app.")
    sub = ap.add_subparsers(dest="command", required=True, metavar="{index,search}")
    ix = sub.add_parser("index", help="build or catch up the search indexes")
    ix.add_argument("--config", required=True, help="config file (only data_root is read)")
    # one app per process, so its one index; several installed apps (the core tests' toy apps) index one by one
    ix.add_argument("--source", choices=tuple(mods) + ("all",), default="all", help=argparse.SUPPRESS)
    ix.add_argument("--rebuild", action="store_true", help="rebuild from the whole ledger")
    ix.add_argument("--status", action="store_true", help="report index state only (writes nothing)")
    ix.add_argument("--json", action="store_true")
    se = sub.add_parser("search", help="search one app's index")
    by_label = se.add_subparsers(dest="source", required=True, metavar="{" + ",".join(mods) + "}")
    for name, mod in mods.items():
        p = by_label.add_parser(name, help=mod.HELP, description=(mod.__doc__ or "").split("\n\n")[0],
                                formatter_class=argparse.RawDescriptionHelpFormatter)
        mod.add_arguments(p)
    return ap


def _worst(a, b):
    return a if _SEVERITY.get(a, 3) >= _SEVERITY.get(b, 3) else b


def _fmt_report(name, r):
    if "action" not in r:          # --status
        keys = ("state", "seq", "ledger_seq", "stale", "rows", "rooms", "channels", "writer_busy", "built_at",
                "updated_at")
        return f"{name}: " + " ".join(f"{k}={r[k]}" for k in keys if k in r)
    parts = [f"{name}: {r['action']}"]
    if r.get("reason"):
        parts.append(f"reason={r['reason']}")
    for k in ("seq", "ledger_seq", "manifests", "records", "rows", "deleted", "system", "gone", "rooms", "channels",
              "seconds"):
        if k in r:
            parts.append(f"{k}={r[k]}")
    if r.get("blocked") == "mid_commit":
        parts.append(f"behind: manifest {r['blocked_seq']} is still being committed by capture")
    elif r.get("blocked") == "defect":
        parts.append(f"STOPPED: manifest {r['blocked_seq']} or one of its segments cannot be read")
    elif r.get("behind"):
        parts.append("behind")
    return " ".join(parts)


def cmd_index(a, mods):
    data_root = common.load_data_root(a.config)
    names = tuple(mods) if a.source == "all" else (a.source,)
    reports, rc = {}, common.EXIT_OK
    if not names and not a.json:
        print("index: the app has no search source")
    for name in names:
        mod = mods[name]
        try:
            if a.status:
                r = common.status(data_root, mod.SPEC)
            else:
                r = common.update(data_root, mod.SPEC, rebuild=a.rebuild)
        except Busy:
            r = {"source": name, "action": "busy", "reason": "another index update holds the lock"}
            rc = _worst(rc, common.EXIT_BUSY)
        except LedgerDefect:
            r = {"source": name, "action": "failed", "reason": "a committed ledger file cannot be read"}
            rc = _worst(rc, common.EXIT_ERR)
        except Exception as e:  # noqa: BLE001 -- one source's failure must not keep the other from catching up
            r = {"source": name, "action": "failed", "reason": f"error {type(e).__name__}: {e}"}
            print(f"search: {name}: error {type(e).__name__}: {e}", file=sys.stderr)
            rc = _worst(rc, common.EXIT_ERR)
        if r.get("blocked") == "defect":
            rc = _worst(rc, common.EXIT_ERR)
        reports[name] = r
    if a.json:
        print(common.dumps(reports))
    else:
        for name, r in reports.items():
            print(_fmt_report(name, r))
    return rc


def main(argv=None):
    """argv: index ... | search <label> ..."""
    common.setup_stdio()
    try:
        mods = dict(registry.search_modules())
        a = _parser(mods).parse_args(argv)
        if a.command == "index":
            return cmd_index(a, mods)
        return mods[a.source].run(a)
    except SystemExit as e:                 # --help
        return e.code if isinstance(e.code, int) else common.EXIT_USAGE
    except UsageError as e:
        print(f"search: {e}", file=sys.stderr)
        return common.EXIT_USAGE
    except Busy:
        print("search: the index is being updated by another process (lock busy)", file=sys.stderr)
        return common.EXIT_BUSY
    except LedgerDefect as e:
        print(f"search: ledger defect: {e}", file=sys.stderr)
        return common.EXIT_ERR
    except KeyboardInterrupt:
        return 130
    except Exception as e:  # noqa: BLE001
        print(f"search: error {type(e).__name__}: {e}", file=sys.stderr)
        return common.EXIT_ERR
