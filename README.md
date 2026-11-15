# llm-wiki-raw-layer

The raw layer of an LLM wiki built from private conversations: each app reads its sources read-only and keeps
its own append-only, crash-safe ledger of what they say. Capture records changes; an SQLite FTS5 index makes
committed ledger records searchable. The apps share one root project and framework, but have separate data
roots, ledgers, indexes, configs and scheduled tasks.

| app (folder name) | origins, in ledger order |
|---|---|
| `kakaotalk` | backup_decrypted, edb |
| `mattermost` | mm_archive |
| `discord` | dc_archive |
| `notion` | nt_archive |
| `gmail` | gm_archive |
| `claudecode` | cc_archive |

Python standard library is used throughout, with pycryptodome for KakaoTalk `.edb` decryption. Message data and
machine configs belong outside the repository. The record format, keys, canonical form, hashes, change detection
and commit protocol come from `SPEC.md` and `SPEC-CLARIFICATIONS.md` in the `llm-wiki` checkout.

[AGENTS.md](AGENTS.md) holds the rules for coding agents working here, [llm.md](llm.md) is the gated runbook an
agent follows to bring an app into live operation, keep it healthy and recover it, and [CORE.md](CORE.md) is the
framework's contract. This file holds what every app shares; each `<app>/README.md` holds that app's sources,
archive contract and search rows.

## Quick start

1. Prerequisites on Windows: Python 3.12 or newer from python.org (a real interpreter, not the Microsoft Store
   alias), Git for Windows installed machine-wide (the SYSTEM task runs `git`), and PowerShell 7.3 or newer for
   the runbook's commands. Nothing is installed with pip for the five apps that use the standard library only.
2. Clone the repository under your user profile (`%USERPROFILE%\...`): the scheduled tasks run as SYSTEM and
   find the data parent through the profile that holds the checkout.
3. From the checkout, run `python -s -B manage.py check`; it ends with `check: ok errors=0 refused=0`.
4. `python -s -B tools/bootstrap.py doctor` shows the machine's state and, for each app, the next runbook step:
   `5.1` for an app not set up yet, `8` for one that is live (llm.md section 4).
5. Give a coding agent [llm.md](llm.md) and name the app to bring up, e.g. "llm.md를 따라 gmail 수집을 켜줘".
   It runs the steps, gates each one and hands back only the steps a person must do (sign-ins, consent, UAC).
6. Optional, for Claude Code from any folder: `python -s -B tools/bootstrap.py agents install --dry-run`, then,
   after reviewing the diff, `agents install`. It adds a block to the global CLAUDE.md, the `raw-search` and
   `llm-wiki-setup` skills, the `raw-retriever` subagent and the `llm_wiki_raw` MCP search server (llm.md
   section 11).

## Layout

```
<PROJECT>/
  manage.py                    the single entry point
  config/settings.py           project settings
  rawlayer/                    shared capture, ledger, search, registry, scheduling and wiki-export (collect) framework
    management/base.py         BaseCommand contract
    management/commands/       core commands
  tests/core/                  framework tests with toyapp and toyapp2
  <app>/                       one package per app (claudecode, discord, gmail, kakaotalk, mattermost, notion): its
                               README, apps.py, origins.py, readers/, search.py, tests/, config.example.json
  appkit/archive.py            the apps' shared library: the fetch steps' file, lock, token and exit-code helpers and
                               `ArchiveOrigin`; appkit/setup.py: the shared setup defaults (AppConfig.setup_defaults)
  tools/bootstrap.py           setup helper for agents: doctor, config get/set/defaults, checkpoints (llm.md 5.0)
  tools/agentkit.py            the agent integration (bootstrap.py agents): global install
  tools/agent_templates/       the one source of the skills, the subagent and the global CLAUDE.md block
  tests/tools/                 tests of tools/ (run with the core suite)
  AGENTS.md, CLAUDE.md         rules for coding agents (CLAUDE.md imports AGENTS.md)
  llm.md                       the operations runbook for agents
  CORE.md                      core contract
```

CORE.md, "The app contract", lists the modules of an app folder.
The core consists of `manage.py`, `CORE.md`, `rawlayer/`, `tests/__init__.py` and `tests/core/`.

## Discovery, selection and settings

`registry.discover()` finds apps without importing them: every top-level folder beside `manage.py` containing
both `__init__.py` and `apps.py`, with a valid lower-case app name, is discovered in name order unless reserved
or listed in `settings.EXCLUDE_APPS`. The folder name is the package, `AppConfig.label`, command argument and
index filename. `registry.select(app)` loads that app before a data-root command starts. One process selects
one app and works on one ledger; selecting another app in that process is refused. Required app arguments may
be omitted when the project has only one app. Cross-app validation is done separately by `check`.

`config/settings.py` holds versioned project settings:

| setting | meaning |
|---|---|
| `BASE_DIR` | `<PROJECT>`, the folder holding `manage.py` |
| `HOME_DIR` | `<HOME>`, the owning user's profile home determined from the checkout, including when running as SYSTEM |
| `IMPL_NAME` | implementation name recorded in manifests; the config's `impl` must match (currently `claude`) |
| `DATA_PARENT` | `<HOME>/llm-wiki-data`, the protected, Search-excluded parent of every data root; the data roots sit in its `raw/` folder |
| `SQLITE_VEC_DIR` | `C:\Program Files\sqlite-vec`, where `search --hybrid` loads sqlite-vec's `<version>\vec0.dll` (llm.md 12.2): outside the data parent, changeable only by an administrator |
| `EXCLUDE_APPS` | app folders to leave out of discovery (currently empty); do not exclude an app while its scheduled task remains registered |

Machine-specific values live in `<DATA_PARENT>/raw/<app>/config.json`, never in versioned settings. The config has
`v`, `impl`, `data_root`, `sources` and the app's options; see each `config.example.json` and app
README. `<DATA_ROOT>` denotes the selected app's data root (by convention `<DATA_PARENT>/raw/<app>`). It must sit
inside `DATA_PARENT`, outside git work trees, with the required ACL and Windows Search exclusion.

Every data root has the same layout:

```
<DATA_ROOT>/                     <DATA_PARENT>/raw/<app>
  config.json                    the machine config
  archive/                       what the app's fetch step keeps (the apps that have one)
  raw/ledger/<origin>/<YYYY>/    committed records, as gzipped JSONL segments
  raw/manifests/                 one manifest per capture commit
  raw/.state/state.sqlite        capture's fold of the ledger
  raw/log.md                     one line per committed capture
  index/<app>.sqlite             the search index (<app>.vec.sqlite: the optional dense sidecar, llm.md 12)
  reports/task.log               the step log of `capture` (llm.md section 3)
```

## Commands

Run from `<PROJECT>`, always as `python -s -B manage.py <command> [<app>] ...`. `help` lists commands;
`help <command>` gives a synopsis, and a command's `--help` gives its options. `--config` is optional at this
entry point and defaults to `<DATA_PARENT>/raw/<app>/config.json`. Eight commands are the whole surface: the steps
between them (capture, index, replay, the per-app input derivations) run inside `capture`, `check`
and `setup`, not as commands of their own.

| invocation after `manage.py` | purpose and options |
|---|---|
| `setup <app> [--from CFG\|-] [--force] [--provision] [--scan] [--root HEXFILE] [--restart-client]` | prepare the data parent, app data root and config; `-` reads config from stdin; force replaces an existing config; the app fills its own config defaults and runs its provisioning (kakaotalk: the root key and keystore); `--provision` reruns only that provisioning — the recovery path when a credential rotated — with `--scan` (memory fallback), `--root` (a root file to install, verified first) and `--restart-client` |
| `capture <app> [--config CFG] [--origins a,b,...] [--rebuild-index]` | the capture+index pipeline: append source changes, then bring the index up to the committed ledger after every capture exit code; log steps in `<DATA_ROOT>/reports/task.log` and return capture's code; the body of each app's scheduled task |
| `collect <app> [--config CFG] [--project DIR] [--include S ...] [--exclude S ...] [--since YYYY-MM] [--dry-run] [--prune]` | the owner-only export step: render the app's index as Markdown sources of the wiki vault, whose headless ingest sends them to its LLM (llm.md 13). Needs `AppConfig.wiki` in the app's `apps.py`; refuses to run as LocalSystem; `--prune` deletes orphaned source files (their wiki pages stay) and runs only on the owner's explicit yes; prints counts only; not part of any scheduled task |
| `search <app> [QUERY] [--config CFG] [options]` | search the app's index ("Search" below); `search <app> --help` lists the options |
| `embed <app> [--config CFG] [--model M] [--dims N] [--url U] [--limit N] [--dry-run] [--rebuild]` | build or refresh the optional dense sidecar that `search --hybrid` reads; needs Ollama on a loopback address and sqlite-vec's windows-x86_64 build (llm.md 12) |
| `check [<app>] [--config CFG] [--rebuild-state] [--traceback]` | validate settings, app contracts and collisions; with an app, load its config and verify the ledger's hash chain and segments and the folded state against the ledger; `--rebuild-state` rebuilds the state from the ledger |
| `test [core\|<app> ...] [-k PATTERN] [-v] [-f]` | run core and app suites, each in its own process; defaults to all suites |
| `schedule install\|status\|run\|remove [<app>] [--config CFG] [--python EXE] [--run-now\|--dry-run]` | register, inspect, start once or unregister the app's Windows task; status compares the registered definition, reports the last result and the task log, and returns 3 when attention is needed |

`setup` creates a protected data parent when missing; it checks an existing parent's permissions and Windows
Search exclusion rather than changing them. It names the source placeholders it could not resolve: fill them in
before using the config.

Discord, Notion, Gmail, Mattermost and Claude Code have an app-owned fetch step that fills the archive capture
reads: `python -s -B -m <app>.fetch [--auth-check] [--max-items N] [--full] [--capture] [--config CFG]`. With the
app's sync option on (`dc_sync`, `nt_sync`, `gm_sync`, `mm_sync`, `cc_sync`), capture runs it first. The app READMEs define the options, archive contracts and authentication boundaries.

Capture exit codes are 0 ok, 3 suspect, 75 busy, below the disk floor or skipped, 2 refused, and 1 error. Suspect
origins commit nothing that run; other origins may commit. Search returns 0 even for zero hits, 2 for
usage/query/index problems, 75 for a busy index lock, and 1 for an unexpected error. Consult command output for
other commands' results.

## Search

`manage.py search <app> --help` lists an app's query syntax, modes, filters and cursors; each app README says what
a row is and where its labels come from; AGENTS.md says how to search. How the index follows the committed ledger
(incremental, rebuilt, crash-safe, one writer) is `rawlayer/search/index.py`'s header, and how text matches (Korean
as a substring, the n-gram rules and why each exists) is `rawlayer/search/fts.py`'s, checked against an independent
model by a property test in `tests/core/test_fts.py`. `--json` prints one envelope: the query as given and as run,
`total`, `rows` and `index{seq, ledger_seq, stale}`, plus `page` when browsing; large ids are strings. Output
is not masked: whoever uses it applies their own privacy rules (AGENTS.md).

## Scheduled tasks

Each app's `AppConfig.schedule` in `<app>/apps.py` defines one Windows SYSTEM task, `\llm-wiki\<app>-capture`
(its start, interval and run limit are there). Its action runs, in `<PROJECT>`:

```
<python> -s -B <PROJECT>\manage.py capture <app> --config <cfg>
```

`capture` runs the capture step, then the index step after every capture exit code (only committed manifests enter
the index), logs both in `<DATA_ROOT>/reports/task.log` and returns the capture step's code.
`rawlayer/schedule.py`'s header says what `schedule install|status|run|remove` check and report; llm.md 5.8 is the
runbook.

## Tests

AGENTS.md gives the test and check commands to run before a commit. `tests/core/` tests the framework on `toyapp`
and `toyapp2` without importing the real apps; each app's `tests/` builds synthetic sources and ledgers in temporary
directories, and no real conversation data is committed. See CORE.md for the ledger guarantees.
