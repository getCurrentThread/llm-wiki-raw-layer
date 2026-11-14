# -*- coding: utf-8 -*-
"""The capture-side steps, run in-process by `manage.py capture` (capture) and `manage.py check` (replay
[--check]); not a command surface of their own:
  <step> --config <path> [--origins a,b]   (SPEC 13)

step = capture | replay [--check]. --origins defaults to every
origin of the app. A data root whose ledger belongs to another app is refused (ledger.foreign_origins). Exit codes:
0 ok, 3 suspect, 75 busy or skipped, 2 refused, 1 error."""
import argparse
import sys
from pathlib import Path

from .. import registry
from . import publicout, run, state
from .config import Config, ConfigError
from .ledger import LedgerCorrupt, foreign_origins
from .publicout import lit

COMMANDS = ("capture", "replay")


def _parser():
    ap = argparse.ArgumentParser(prog="manage.py")
    ap.add_argument("command", choices=COMMANDS)
    ap.add_argument("--config", required=True)
    ap.add_argument("--origins", default=None, help="comma-separated (default: every installed origin)")
    ap.add_argument("--check", action="store_true")
    return ap


def main(argv=None):
    a = _parser().parse_args(argv)
    try:
        cfg = Config(a.config)
        installed = registry.origin_order()
        origins = list(installed) if a.origins is None else [o for o in a.origins.split(",") if o]
        if any(o not in installed for o in origins):
            raise ConfigError("unknown origin")
        if not origins:
            raise ConfigError("the app has no origin")
        if foreign_origins(cfg.raw):
            raise ConfigError("ledger belongs to another app")
        if a.command == "capture":
            return run.capture(cfg, origins)
        if a.command == "replay":
            return replay(cfg, a.check)
    except ConfigError:
        publicout.say(lit("refused: precondition or configuration"))
        return run.EXIT_CFG
    except run.Busy:
        publicout.say(lit("busy"))
        return run.EXIT_BUSY
    except LedgerCorrupt:
        publicout.say(lit("ledger corrupt"))
        return run.EXIT_ERR
    except Exception as e:  # noqa: BLE001 — type name and errno only (SPEC 11)
        en = getattr(e, "errno", None)
        print("error " + type(e).__name__ + (f" errno={en}" if isinstance(en, int) else ""), file=sys.stderr)
        return run.EXIT_ERR
    return run.EXIT_ERR


def replay(cfg, check_only):
    ledger, lock, con = run.open_session(cfg)
    try:
        seal = ledger.seal()            # first: a missing segment would stop the fold below
        publicout.say(lit("seal"), prev_chain=seal.get("prev_chain", 0),
                      segment_missing=seal.get("segment_missing", 0), segment_mismatch=seal.get("segment_mismatch", 0))
        if seal:
            return 1
        tmp = Path(ledger.work_root, "replay-state.sqlite")
        Path(ledger.work_root).mkdir(parents=True, exist_ok=True)
        rebuilt = state.rebuild(ledger, tmp)
        rebuilt.close()
        diff = state.compare(con, tmp)
        bad = sum(diff.values())
        publicout.say(lit("replay"), check=check_only, differing_rows=bad, head_seq=state.head_seq(con))
        if not check_only and bad:
            con.close()
            dst = Path(ledger.state_dir, "state.sqlite")
            tmp.replace(dst)
            con = state.open_state(dst)
        return 0 if (bad == 0 or not check_only) else 1
    finally:
        for p in (Path(ledger.work_root, "replay-state.sqlite"),):
            try:
                p.unlink()
            except OSError:
                pass
        con.close()
        lock.release()
