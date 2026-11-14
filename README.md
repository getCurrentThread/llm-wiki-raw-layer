# llm-wiki-raw-layer

The raw layer of an LLM wiki built from private conversations: each app reads its sources read-only and keeps
its own append-only, crash-safe ledger of what they say. Capture records changes; an SQLite FTS5 index makes
committed ledger records searchable. The apps share one root project and framework, but have separate data
roots, ledgers, indexes, configs and scheduled tasks.

| app (folder name) | origins, in ledger order |
|---|---|
| `kakaotalk` | backup_decrypted, edb, macdb |
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
  kakaotalk/                   app package (see kakaotalk/README.md)
  mattermost/                  app package (see mattermost/README.md)
  discord/                     DCE JSON archive reader and message/thread search (see discord/README.md)
  notion/                      page snapshot reader and block search (see notion/README.md)
  gmail/                       Gmail IMAP archive reader and mail/thread search (see gmail/README.md)
  claudecode/                  Claude Code conversation archive and session/project search (see claudecode/README.md)
    apps.py                    AppConfig: label, source keys, options, schedule and task checks
    origins.py                 origins in processing and commit order
    inputs.py                  live and frozen input paths (optional: `AppConfig.archive` gives the archive input)
    readers/                   source readers
    vocab.py                   public output vocabulary (optional)
    search.py                  app index and search interface
    tests/                     app tests with synthetic inputs
    config.example.json        machine config shape
  appkit/archive.py            the apps' shared library: the fetch steps' file, lock, token and exit-code helpers and
                               `ArchiveOrigin`; appkit/setup.py: the shared setup defaults (AppConfig.setup_defaults)
  tools/bootstrap.py           setup helper for agents: doctor, config get/set/defaults, checkpoints (llm.md 5.0)
  tools/agentkit.py            the agent integration (bootstrap.py agents): global install, sync-repo
  tools/agent_templates/       the one source of the skills, the subagent and the global CLAUDE.md block
  tests/tools/                 tests of tools/ (run with the core suite)
  .claude/skills/, .claude/agents/, .agents/skills/
                               raw-search, llm-wiki-setup and raw-retriever, generated from the templates
                               (`bootstrap.py agents sync-repo`; edit the templates, not these)
  AGENTS.md, CLAUDE.md         rules for coding agents (CLAUDE.md imports AGENTS.md)
  llm.md                       the operations runbook for agents
  CORE.md                      core contract
```

Every app folder holds the modules shown under `claudecode/` above (CORE.md, "The app contract").
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
`v`, `impl`, `data_root`, `inputs_root`, `sources` and the app's options; see each `config.example.json` and app
README. `<DATA_ROOT>` denotes the selected app's data root (by convention `<DATA_PARENT>/raw/<app>`). It must sit
inside `DATA_PARENT`, outside git work trees, with the required ACL and Windows Search exclusion. Frozen `T1`
and `T2` inputs require `inputs_root`; live capture does not require it to exist.

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
between them (capture, index, replay, export-state, the per-app input derivations) run inside `capture`, `check`
and `setup`, not as commands of their own.

| invocation after `manage.py` | purpose and options |
|---|---|
| `setup <app> [--from CFG\|-] [--force] [--provision] [--scan] [--root HEXFILE] [--restart-client]` | prepare the data parent, app data root and config; `-` reads config from stdin; force replaces an existing config; the app fills its own config defaults and runs its provisioning (kakaotalk: the root key, keystore, and on a Mac the client database); `--provision` reruns only that provisioning — the recovery path when a credential rotated — with `--scan` (memory fallback), `--root` (a root file to install, verified first) and `--restart-client` |
| `capture <app> [--config CFG] [--input-set live\|T1\|T2] [--origins a,b,...] [--rebuild-index]` | the capture+index pipeline: append source changes, then bring the index up to the committed ledger after every capture exit code; log steps in `<DATA_ROOT>/reports/task.log` and return capture's code; the body of each app's scheduled task |
| `collect <app> [--config CFG] [--project DIR] [--include S ...] [--exclude S ...] [--since YYYY-MM] [--dry-run] [--prune]` | the owner-only export step: render the app's index as Markdown sources of the wiki vault, whose headless ingest sends them to its LLM (llm.md 13). Needs `AppConfig.wiki` in the app's `apps.py`; refuses to run as LocalSystem; `--prune` deletes orphaned source files (their wiki pages stay) and runs only on the owner's explicit yes; prints counts only; not part of any scheduled task |
| `search <app> [QUERY] [--config CFG] [options]` | search the app's index ("Search" below); `search <app> --help` lists the options |
| `embed <app> [--config CFG] [--model M] [--dims N] [--url U] [--limit N] [--dry-run] [--rebuild]` | build or refresh the optional dense sidecar that `search --hybrid` reads; needs Ollama on a loopback address and sqlite-vec's windows-x86_64 build (llm.md 12) |
| `check [<app>] [--config CFG] [--export-state PATH] [--rebuild-state] [--traceback]` | validate settings, app contracts and collisions; with an app, load its config and verify the ledger's hash chain and segments and the folded state against the ledger; `--export-state` also dumps every record's (origin, key, rev, op, h), `--rebuild-state` rebuilds the state from the ledger |
| `test [core\|<app> ...] [-k PATTERN] [-v] [-f]` | run core and app suites, each in its own process; defaults to all suites |
| `schedule install\|status\|run\|remove [<app>] [--config CFG] [--python EXE] [--run-now\|--dry-run]` | register, inspect, start once or unregister the app's Windows task; status compares the registered definition, reports the last result and the task log, and returns 3 when attention is needed |

`setup` creates a protected data parent when missing; it checks an existing parent's permissions and Windows
Search exclusion rather than changing them. It names the source placeholders it could not resolve: fill them in
before using the config.

Discord, Notion, Gmail, Mattermost and Claude Code have an app-owned fetch step that fills the archive capture
reads: `python -s -B -m <app>.fetch [--auth-check] [--max-items N] [--full] [--capture] [--config CFG]`. With the
app's sync option on (`dc_sync`, `nt_sync`, `gm_sync`, `mm_sync`, `cc_sync`), live capture runs it first; frozen
inputs never do. The app READMEs define the options, archive contracts and authentication boundaries.

Capture exit codes are 0 ok, 3 suspect, 75 busy, below the disk floor or skipped, 2 refused, and 1 error. Suspect
origins commit nothing that run; other origins may commit. Search returns 0 even for zero hits, 2 for
usage/query/index problems, 75 for a busy index lock, and 1 for an unexpected error. Consult command output for
other commands' results.

## Search

`manage.py search <app> --help` lists an app's modes, filters and cursors, and the app README says what a row is
and where its text and labels come from. Every app shares the rest.

**The index.** `capture`'s index step brings `<DATA_ROOT>/index/<app>.sqlite` up to the committed ledger (search
reads only `data_root` from the config):

- **Incremental.** Manifests are applied in seq order, one transaction per manifest together with the
  index's watermark (head seq, plus the sha256 and run id of that manifest).
- **Full rebuild.** It runs on `capture --rebuild-index`, when the index is missing, unreadable or stamped with
  another schema/tokenizer/extractor version, when the watermark no longer matches the ledger (the ledger was
  reset), when a manifest is not a capture manifest, or when the catch-up is very large.
- **Rebuild and catch-up give the same index.** Both run the same record handling in ledger order.
- **Only committed manifests are read.** If a manifest's segments are still `*.part`, capture is
  mid-commit: the index stops before it and reports `behind` (exit 0). An unreadable committed file is
  a ledger defect (exit 1).
- **Crash safety.** A rebuild is written to `<name>.sqlite.new`, checked with FTS5 `integrity-check`
  and `quick_check`, then swapped in. A crash never loses the live index.
- **One writer.** The index has a writer lock (exit 75 when busy). Searches open the index read-only and are
  never blocked by it.

**The query language.**

| Query | Meaning |
|---|---|
| `배포 일정` | both terms (AND) |
| `"정기 회의"` | the exact phrase |
| `회의 OR 미팅` | either term |
| `배포 -롤백` | the first term, excluding the second |
| `api*` | prefix |

Text is normalised as NFC + casefold. Each row gets an n-gram shadow column: Korean/CJK runs become overlapping
bigrams and Latin/digit runs become one token. A query term is issued as an FTS5 phrase over the same tokens
(tokenizer `ngram-3`; `rawlayer/search/fts.py`'s docstring holds the rules and why each exists). As a result:

- Korean matches as a substring: `배포` finds `배포를` and `재배포`. Particles and common endings are also
  stripped from query terms unless `--literal` is given or the term is quoted (AGENTS.md).
- A lone space between Korean words may be left out: `회의실예약` finds `회의실 예약`. Two spaces, a line break
  or punctuation still separate, and `가나다` does not match `가나 나다`.
- A Latin/digit run at a term's edge must match a whole run: `api` finds `API 키` but not `rapid`, and
  `api*` finds `apis`.
- Quoted phrases and terms with punctuation (`C++`, `AES-256`) match literally after normalisation.
- Excluded terms use the same match rule as positive ones, and `OR` next to an excluded term is refused.
- An unquoted term with a Latin/digit run longer than 32,000 characters is refused: SQLite FTS5 cuts tokens at
  32,768 bytes, so such a run would also match longer runs that start with it.
- Snippets mark matches with the same case folding the search uses, so `straße` marks `STRASSE`.

A property test in `tests/core/test_fts.py` checks the index against an independent model of these rules on
thousands of random queries.

**Output.** `--json` prints one envelope: the query as given and as run, `total`, `rows` and
`index{seq, ledger_seq, stale}`, plus `page` when browsing; large ids are strings. `--explain` prints the FTS5
expression (the separator token is shown as `<S>`). A stderr note appears when the index is behind the ledger, and
when a query falls back to a full scan. There is no masking, pseudonymisation or redaction: names, titles and
texts are shown as they are, and whoever uses the output applies their own privacy rules (AGENTS.md).

## Scheduled tasks

Each app's `AppConfig.schedule` in `<app>/apps.py` defines one Windows SYSTEM task:

| app | Task Scheduler path | local start / interval | run limit |
|---|---|---|---|
| `kakaotalk` | `\llm-wiki\kakaotalk-capture` | 01:14 / 3 hours | 2 hours |
| `mattermost` | `\llm-wiki\mattermost-capture` | 02:14 / 3 hours | 1 hour |
| `discord` | `\llm-wiki\discord-capture` | 00:14 / 3 hours | 1 hour |
| `notion` | `\llm-wiki\notion-capture` | 03:29 / 6 hours | 1 hour |
| `gmail` | `\llm-wiki\gmail-capture` | 00:44 / 3 hours | 1 hour |
| `claudecode` | `\llm-wiki\claudecode-capture` | 02:44 / 3 hours | 1 hour |

The action's Command is the selected Python interpreter, WorkingDirectory is `<PROJECT>`, and Arguments are:

```
-s -B <PROJECT>\manage.py capture <app> --config <cfg>
```

Paths containing spaces are quoted by the generated definition. `install` checks the config, data root,
interpreter and app task checks before registration, replaces a task of the same name and compares the definition
read back; `--dry-run` prints the definition without registering it. Install, run and remove require elevation;
status and dry-run do not. Without an app, install and status operate on every app that has a schedule, using
default configs; an explicit `--config` belongs to one app. Run and remove require an app when several are
discovered.

A new start is ignored while a task runs; a missed start runs when the machine returns. `capture` runs its steps
in one process — the capture step with the live input set, then the index step after every capture exit
code (a failed, refused or busy capture included), because only committed manifests enter the index. Steps go to
`<DATA_ROOT>/reports/task.log`; the command returns the capture step's code. `status` reports the newest run that
finished a step, marks an incomplete run and returns 3 when the task, definition, last result or logged step
needs attention.

## Tests

```
python -s -B manage.py test
python -s -B manage.py test core
python -s -B manage.py test core claudecode discord gmail kakaotalk mattermost notion
python -s -B manage.py check
```

`tests/core/` tests the framework on `toyapp` and `toyapp2` without importing the real apps. Each app's `tests/`
selects that app and builds synthetic source data and ledgers in temporary directories; no binary fixtures or
real conversation data are committed. The default test command runs every suite and ends with
`core ok; claudecode ok; discord ok; gmail ok; kakaotalk ok; mattermost ok; notion ok`
when all suites pass. Agents run the named form above (AGENTS.md).

After changing a core file, run every suite (`test`) and `check`. See `CORE.md` for ledger guarantees.
