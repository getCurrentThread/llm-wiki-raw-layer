# llm-wiki-raw-layer

The raw layer of an LLM wiki built from private conversations: each app reads its sources read-only and keeps
its own append-only, crash-safe ledger of what they say. Capture records changes; an SQLite FTS5 index makes
committed ledger records searchable. The apps share one root project and framework, but have separate data
roots, ledgers, indexes, configs and scheduled tasks.

| app (folder name) | origins, in ledger order | index under its data root |
|---|---|---|
| `kakaotalk` | backup_decrypted, edb, macdb | `index/kakaotalk.sqlite` |
| `mattermost` | mm_archive | `index/mattermost.sqlite` |
| `discord` | dc_archive | `index/discord.sqlite` |
| `notion` | nt_archive | `index/notion.sqlite` |
| `gmail` | gm_archive | `index/gmail.sqlite` |
| `claudecode` | cc_archive | `index/claudecode.sqlite` |

Python standard library is used throughout, with pycryptodome for KakaoTalk `.edb` decryption. Message data and
machine configs belong outside the repository. The record format, keys, canonical form, hashes, change detection
and commit protocol come from `SPEC.md` and `SPEC-CLARIFICATIONS.md` in the `llm-wiki` checkout.

[AGENTS.md](AGENTS.md) holds the rules for coding agents working here, and [llm.md](llm.md) is the gated
runbook an agent follows to bring an app into live operation, keep it healthy and recover it.

## Quick start

1. Prerequisites on Windows: Python 3.12 or newer from python.org (a real interpreter, not the Microsoft Store
   alias), Git for Windows installed machine-wide (the SYSTEM task runs `git`), and PowerShell 7.3 or newer for
   the runbook's commands. Nothing is installed with pip for the five apps that use the standard library only.
2. Clone the repository under your user profile (`%USERPROFILE%\...`): the scheduled tasks run as SYSTEM and
   find the data parent through the profile that holds the checkout.
3. From the checkout, run `python -s -B manage.py check`; it ends with `check: ok errors=0 refused=0`.
4. `python -s -B tools/bootstrap.py doctor` shows the machine's state and each app's state. For each app it names
   the next runbook step: `5.1` for an app not set up yet, `8` for one that is live. Without elevation it cannot
   read the scheduled tasks, so it shows `8` only for an app whose live check the runbook recorded (llm.md 5.10;
   for an app that went live before the tool, the health routine in llm.md section 8 records it).
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
  rawlayer/                    shared capture, ledger, search, registry and scheduling framework
    management/base.py         BaseCommand contract
    management/commands/       core commands
  tests/core/                  framework tests with toyapp and toyapp2
  kakaotalk/                   app package (see kakaotalk/README.md)
  mattermost/                  app package (see mattermost/README.md)
  discord/                     DCE JSON archive reader and message/thread search (see discord/README.md)
  notion/                      page snapshot reader and block search (see notion/README.md)
  gmail/                       Gmail API archive reader and mail/thread search (see gmail/README.md)
  claudecode/                  Claude Code conversation archive and session/project search (see claudecode/README.md)
    apps.py                    AppConfig: label, source keys, options, schedule and task checks
    origins.py                 origins in processing and commit order
    inputs.py                  live and frozen input paths
    readers/                   source readers
    vocab.py                   public output vocabulary
    search.py                  app index and search interface
    tests/                     app tests with synthetic inputs
    config.example.json        machine config shape
  tools/coresums.py            developer script: check/rewrite CORE_SHA256SUMS
  tools/archiveio.py           the collectors' shared file, lock and exit-code helpers
  tools/bootstrap.py           setup helper for agents: doctor, config get/set/defaults, checkpoints (llm.md 5.0)
  tools/appsetup.py            the apps' shared setup defaults (AppConfig.setup_defaults)
  tools/agentkit.py            the agent integration (bootstrap.py agents): global install, sync-repo
  tools/wikiexport.py          an app's index as Markdown sources of an LLM Wiki project (llm.md 13)
  tools/agent_templates/       the one source of the skills, the subagent and the global CLAUDE.md block
  tests/tools/                 tests of tools/ (run with the core suite)
  .claude/skills/, .claude/agents/, .agents/skills/
                               raw-search, llm-wiki-setup and raw-retriever, generated from the templates
                               (`bootstrap.py agents sync-repo`; edit the templates, not these)
  AGENTS.md, CLAUDE.md         rules for coding agents (CLAUDE.md imports AGENTS.md)
  llm.md                       the operations runbook for agents
  CORE.md, CORE_SHA256SUMS     core contract and checksums
```

The app modules shown above also apply to `kakaotalk/`, `mattermost/`, `discord/`, `notion/` and `claudecode/`.
The core consists of `manage.py`, `CORE.md`, `rawlayer/`, `tests/__init__.py` and `tests/core/`, as defined by `rawlayer/coreset.py`.
`CORE_SHA256SUMS` pins those files; line endings do not affect the checksums.

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
| `DATA_PARENT` | `<HOME>/llm-wiki-data`, the protected, Search-excluded parent of every data root |
| `EXCLUDE_APPS` | app folders to leave out of discovery (currently empty); do not exclude an app while its scheduled task remains registered |

Machine-specific values live in `<DATA_PARENT>/<app>/config.json`, never in versioned settings. The config has
`v`, `impl`, `data_root`, `inputs_root`, `sources` and the app's options; see each `config.example.json` and app
README. `<DATA_ROOT>` denotes the selected app's data root (by convention `<DATA_PARENT>/<app>`). It must sit
inside `DATA_PARENT`, outside git work trees, with the required ACL and Windows Search exclusion. Frozen `T1`
and `T2` inputs require `inputs_root`; live capture does not require it to exist.

## Commands

Run from `<PROJECT>`, always as `python -s -B manage.py <command> [<app>] ...`. `help` lists commands;
`help <command>` gives a synopsis, and a command's `--help` gives its options. `--config` is optional at this
entry point and defaults to `<DATA_PARENT>/<app>/config.json`. Seven commands are the whole surface: the steps
between them (capture, index, replay, export-state, the per-app input derivations) run inside `collect`, `check`
and `setup`, not as commands of their own.

| invocation after `manage.py` | purpose and options |
|---|---|
| `setup <app> [--from CFG\|-] [--force] [--provision] [--scan] [--root HEXFILE] [--restart-client]` | prepare the data parent, app data root and config; `-` reads config from stdin; force replaces an existing config; the app fills its own config defaults and runs its provisioning (kakaotalk: the root key, keystore, decrypted backup, and on a Mac the client database); `--provision` reruns only that provisioning — the recovery path when a credential rotated — with `--scan` (memory fallback), `--root` (a root file to install, verified first) and `--restart-client` |
| `collect <app> [--config CFG] [--input-set live\|T1\|T2] [--origins a,b,...] [--rebuild-index]` | the capture+index pipeline: append source changes, then bring the index up to the committed ledger after every capture exit code; log steps in `<DATA_ROOT>/reports/task.log` and return capture's code; the body of each app's scheduled task |
| `search <app> [QUERY] [--config CFG] [options]` | search the app's index; query options are in the app README and `search <app> --help` |
| `embed <app> [--config CFG] [--model M] [--dims N] [--url U] [--limit N] [--dry-run] [--rebuild]` | build or refresh the optional dense sidecar that `search --hybrid` reads; needs Ollama on a loopback address and sqlite-vec's windows-x86_64 build (llm.md 12) |
| `check [<app>] [--config CFG] [--export-state PATH] [--rebuild-state] [--traceback]` | validate settings, app contracts, collisions and core checksums; with an app, load its config and verify the folded state against the ledger (the old replay --check); `--export-state` also dumps every record's (origin, key, rev, op, h) (the old export-state), `--rebuild-state` rebuilds the state from the ledger (the old replay) |
| `test [core\|<app> ...] [-k PATTERN] [-v] [-f]` | run core and app suites, each in its own process; defaults to all suites |
| `schedule install\|status\|run\|remove [<app>] [--config CFG] [--python EXE] [--run-now\|--dry-run]` | register, inspect, start once or unregister the app's Windows task; status compares the registered definition, reports the last result and the task log, and returns 3 when attention is needed |

`setup` creates a protected data parent when missing, checks an existing parent's permissions and Search
exclusion, and writes the app config from its example or the supplied config. The app then fills the config
placeholders it can resolve itself and runs its own provisioning before the config is validated (kakaotalk:
default source paths, the client's user directory, and the root key derived from the research tree's copy or
recovered from the running client's memory — see the app README). Fill in any remaining source placeholders
before using it. It checks Windows Search exclusion rather than changing the indexer's settings.

Capture exit codes are 0 ok, 3 suspect, 75 busy or skipped, 2 refused, and 1 error. Suspect origins commit nothing
that run; other origins may commit. Search returns 0 even for zero hits, 2 for usage/query/index problems, 75
for a busy index lock, and 1 for an unexpected error. Consult command output for other commands' results.

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
-s -B <PROJECT>\manage.py collect <app> --config <cfg>
```

Paths containing spaces are quoted by the generated definition. `install` checks the config, data root,
interpreter and app task checks before registration. Install, run and remove require elevation; status and dry-run do not. Without an app, install and status
operate on every app that has a schedule, using default configs; an explicit `--config` belongs to one app.
Run and remove require an app when several are discovered.

A new start is ignored while a task runs; a missed start runs when the machine returns. Collect runs its steps
in this one process — the capture step with the live input set, then the index step after every capture exit
code (a failed, refused or busy capture included), because only committed manifests enter the index. Steps go to
`<DATA_ROOT>/reports/task.log`; collect returns capture's code. `status` reports the newest run that finished a
step, marks an incomplete run and returns 3 when the task, definition, last result or logged step needs
attention.

## Tests and core maintenance

```
python -s -B manage.py test
python -s -B manage.py test core
python -s -B manage.py test core claudecode discord gmail mattermost notion
python -s -B manage.py check
```

`tests/core/` tests the framework on `toyapp` and `toyapp2` without importing the real apps. Each app's `tests/`
selects that app and builds synthetic source data and ledgers in temporary directories; no binary fixtures or
real conversation data are committed. The default test command runs every suite and ends with
`core ok; claudecode ok; discord ok; gmail ok; kakaotalk ok; mattermost ok; notion ok`
when all suites pass. Agents run the named form above, which leaves kakaotalk out (AGENTS.md).

Discord, Notion, Gmail, Mattermost and Claude Code include app-owned collectors; enable `dc_sync` / `nt_sync` /
`gm_sync` / `mm_sync` / `cc_sync` to fetch before live capture. Claude Code reads local conversation files from
`cc_claude_dir` (setup fills `<HOME>\.claude`) without credentials:
`python -s -B -m claudecode.collector [--max-items N] [--capture]`.
Discord and Notion support token-free desktop-cache sources; their API sources use Discord credentials or the official `ntn` keychain. Desktop caches provide partial coverage and never
treat eviction as deletion. Gmail reads the Gmail API with a read-only OAuth token, and Mattermost's collector (a
port of the mattertok archiver, REST API v4 with a session token) writes mattertok's archive format. Their READMEs
define the options, archive contracts and authentication boundaries. Core files remain unchanged.

After changing a core file, run `python -s -B tools/coresums.py --write`, then validate with `tools/coresums.py`,
`test` and `check`. App and non-core documentation edits do not require rewriting the core checksums. See
`CORE.md` for ledger guarantees; the layout and command examples above describe the current root project.
