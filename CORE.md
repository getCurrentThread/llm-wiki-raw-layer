# The rawlayer core

This project is built on **rawlayer**, a small framework that keeps an append-only raw ledger of what one family
of sources says, and a full-text index over it. The framework is the same in every project built on it; what a
project reads is its **app**: a folder next to `manage.py` that holds `__init__.py` and `apps.py`
(`rawlayer/registry.py` finds it; nothing is listed in settings).

## The core files

The framework is these files:

```
manage.py           the single entry point: setup | capture | collect | search | embed | check | test | schedule
CORE.md             this file
rawlayer/           the framework: capture pipeline (rawlayer/capture), search driver (rawlayer/search), registry,
                    schedule (registers the task), collect (the index as LLM Wiki sources: rawlayer/collect.py),
                    management commands (capture is the scheduled task's body; collect is owner-only, never scheduled)
tests/__init__.py
tests/core/         the framework's tests, on a toy app (tests/core/toyapp, toyapp2); they import no real app
```

`tests/core/test_core_contract.py` keeps the framework source-agnostic (no app or source name in `rawlayer/`) and
every app on the contract below. After changing a core file, run every suite: `python -s -B manage.py test`.

## What the core guarantees

- **The ledger is append-only.** A capture run reads every installed origin, diffs it against `state.sqlite`
  (the fold of the ledger), and commits one manifest with gzipped JSONL segments, chained by `prev_sha256`. The
  commit is crash-safe (`inflight.json`, recovered on the next run) and never rewrites a committed file.
- **Records and manifests have one format** (SPEC 6, 9.3): `canon` is the hash input, `publicout` refuses any
  string that is not a registered public word (no message text, name or id ever reaches a manifest, a log or the
  console).
- **Exit codes:** 0 ok, 3 suspect (something vanished or a guard tripped: nothing of that origin is committed), 75
  busy or skipped (another run holds the lock, or an origin / container could not be read: what was read is
  committed), 2 refused (config or precondition), 1 error.
- **The index never gets ahead of the ledger**: it reads committed manifests only, rebuilds when the ledger was
  reset or its version changed, and stops at a defect.
- **The scheduled task is registered by the project itself**: `manage.py schedule install` builds the Task
  Scheduler definition from the app's `AppConfig.schedule` (capture as SYSTEM, IgnoreNew, a time limit) and
  registers it with Windows' `schtasks.exe` after the checks capture would otherwise fail on (config, data root,
  interpreter, the apps' `task_checks`); `status` reads it back with the last run and the step lines of
  `task.log`. The task needs no script file: it is registered, inspected, started and removed with
  `manage.py schedule`.

## The app contract

An app is a folder next to `manage.py` (its name is the app's one name) with the same modules (checked when the
registry loads, and by `manage.py check`):

| module | what it holds |
|---|---|
| `apps.py` | exactly one `AppConfig` subclass: `label`, `source_keys` (config `"sources"` entries, absolute paths that must exist), `archive` (a prefix `p`: the app reads the archive folder its own fetch step keeps; the registry adds the source key `<p>_archive` and, without an `inputs.py`, the input attribute of that name), `options` (top-level config keys: `Option(key, default, check, attr)`), optionally `schedule` (the scheduled task), `task_checks(cfg)` (a list of short reasons the config is unfit for the scheduled task; `schedule install` refuses while there is one) and `wiki = (prefix, layout)` (the app can be exported by `manage.py collect`: `layout` is `chat`, `mail` or `page`, what the containers of its index are; `prefix` is written once and the registry adds `<prefix>_wiki_project`, `_include`, `_exclude` and `_since` to `options`, or `None` for an app whose settings come from the command line only; an app without `wiki` is refused by `collect`) |
| `origins.py` | `ORIGINS`: the app's `Origin` subclasses, in processing and commit order |
| `inputs.py` | optional: `resolve(cfg, input_set, frozen_root)` -> {input attribute: path}; `static_roots(inputs)` -> {freeze label: (root, closed)}. Without it the app's inputs are the one `archive` input (the config's path live, `<inputs_root>/<set>/<p>/archive` frozen), or none |
| `vocab.py` | optional: `WORDS`, `PATTERNS`, the strings its readers put in manifests and console lines (none without it) |
| `search.py` | optional: a search source (`SPEC` a `SourceIndex`, `HELP`, `add_arguments`, `run`) for `manage.py search <label>`. The index half of the usual shape (one content table folded from one origin) subclasses `rawlayer/search/index.py`'s `SimpleIndex` and `SimpleApplier`; the query half subclasses `rawlayer/search/driver.py`'s `SearchDriver`, which derives `--mode` from its `group_modes` (a `Group` per mode: what it groups by and how a group is named) and owns `--before` / `--after`, so `add_arguments` and `run` just delegate to the driver. A `DRIVER` that sets `key_col`, `container_col` and implements `chunk_units` can be embedded (`manage.py embed <label>`, `search --hybrid`: `rawlayer/search/dense.py`) and exported (`manage.py collect <label>`, with `wiki` in `apps.py`) |

An origin is a class (`rawlayer/capture/origins.py`). It sets `name`, `kinds` and the optional `day_kinds`,
`ok_statuses`, `complete_status`, `statuses`, `state_tables`, `fail_statuses`, `fail_default`, and
implements `read(ctx)` -> containers of observations. What differs between sources is a hook with a core default
(`fail_status`, `on_missing`, `finish`, `blocks_run`, `apply_put`, `estimate_peak`, `requires_system`); the rules
the ledger depends on are core mixins an origin combines:

- `Origin`: a key that was there and is not now is *vanished*: the origin is suspect and commits nothing this run.
- `PendingAbsentMixin`: a key missing from a container that was read completely becomes *pending*, and *absent*
  when it is still missing next time, behind a shrink guard (`Guard(min_obs_ratio, max_new_abs, max_new_frac)`).
- `MultiVersionMixin`: the origin is read in one stable pass (`read_once`, retried when it raises `Unstable`) and
  every version of a key seen in the pass is recorded once.

Writing the ledger (records, the commit, the manifest) is never a hook, and `run()` is the core's: the registry
refuses an app that overrides it, a duplicate or malformed origin name, kinds outside `kinds`, a
`complete_status` that is not an ok status, two apps claiming the same source key, option, state table or input
attribute.

## Settings and machine config

`settings.py` (versioned): `BASE_DIR` (the project directory), `HOME_DIR` (the owning user's profile home, also
under SYSTEM), `IMPL_NAME` (recorded in every manifest), `DATA_PARENT` (the ACL'd, Search-excluded parent every
data root must sit in) and optionally `EXCLUDE_APPS` (app folders to leave out of discovery). Never remove an app
from a project whose ledger already holds its origins: every manifest naming them would stop validating.

`config.json` (per machine, never committed; `config.example.json` shows the shape): `v`, `impl`, `data_root`,
`inputs_root` (frozen input sets only), `sources` and the apps' options. Keys nobody reads are accepted and ignored.

## Adding a source

A source that fits the same ledger rules becomes a new app (a folder next to `manage.py` with the modules above,
and an empty ledger: one ledger holds one app's origins). A source that needs a rule no mixin expresses needs a
new core mixin: that is a core change (above).
