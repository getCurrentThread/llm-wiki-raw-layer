# mattermost: the Mattermost raw layer

This app captures private Mattermost conversations into an append-only, crash-safe raw ledger and builds an
SQLite FTS5 search index at `<DATA_ROOT>/index/mattermost.sqlite`. It shares the root project's **rawlayer**
framework (see `../README.md` and `../CORE.md`). Its app label and package are its folder name, `mattermost`.
Message data and machine config remain outside the repository.
The implementation uses the Python standard library.

## Layout

Run commands from `<PROJECT>`, the root containing `manage.py`, `config/settings.py`, and `rawlayer/`.
This app's folder contains `apps.py` (source keys, options and schedule), `origins.py`, `inputs.py`, `readers/`,
`collector.py`, `keys.py`, `vocab.py`, `search.py`, `tests/`, and `config.example.json`.
Core tests are at `<PROJECT>/tests/core/`;
app tests are at `<PROJECT>/mattermost/tests/`. The `/raw-search` skill is
`<PROJECT>/.claude/skills/raw-search/SKILL.md` (shared by every app).

## Config, origins and records

`mattermost/config.example.json` shows the machine config shape. The default config is
`<DATA_PARENT>/mattermost/config.json`. Common keys are `v` (1), `impl` (matching settings.IMPL_NAME), `data_root`,
`inputs_root`, and `sources`. Source paths must be absolute and exist; replace the example's placeholders with
machine-specific paths. `<DATA_ROOT>` conventionally means `<DATA_PARENT>/mattermost`. It must be inside
`DATA_PARENT`, outside git work trees, with the parent's required ACL and Search exclusion. Frozen input sets
need an existing `inputs_root`; live input resolution does not require it to exist.

Capture reads the archive directory read-only. Its one origin is `mm_archive`, with record kinds
`ch`, `post`, `file`, and `user`. The archive contains `manifest.json` channel records, `posts/**.jsonl`
post records (one file per channel), and `users.json`. It is read in one stable pass between two reads of the
manifest. A changed manifest retries the pass (at most three times); continued changes make the origin unstable.
Every version of a key seen in a stable pass is recorded once.

`sources.mm_archive` is the archive directory. `mm_exclude_patterns` is a list of non-empty strings (default
empty). A channel whose display name or name contains a pattern, casefolded, is excluded, using mattertok's rule.
mattertok excluded channels by default and this app does not: when continuing an archive mattertok wrote, copy
mattertok's effective list into `mm_exclude_patterns` — its `MM_EXCLUDE_CHANNELS` setting, or when that is unset
the built-in `DEFAULT_EXCLUDED_CHANNELS` of `mattertok/config.py` — or the collector fetches the channels mattertok
skipped. Frozen `T1`/`T2` inputs read `<inputs_root>/<set>/mm/archive`; live inputs read the configured archive.

## Collection and authentication

`collector.py` is a standard-library port of the mattertok archiver (REST API v4). It writes mattertok's archive
format, so an archive mattertok wrote continues from its cursors, and it needs no other program. Archive-only
configs stay read-only; `mm_sync: true` makes live `manage.py collect mattermost` update the archive before
capturing it. Frozen inputs never call the collector.

- `mm_url` is the server's `https://` address; `mm_token_file` is a private file under `<DATA_PARENT>` holding a
  session token or personal access token on one line. `MM_TOKEN` takes precedence over the file. The token is
  never printed or written into the archive, and requests never follow a redirect (a 3xx is an `api_error`), so
  it reaches no host but `mm_url`.
- `--login` asks for the login id, password (not echoed) and, if needed, an MFA code, and keeps only the session
  token Mattermost returns. The password is never stored. A 401 later means the session expired
  (`auth_required`): sign in again. A 3xx at sign-in is `api_error`: `mm_url` is not the server's address.
- Channels are discovered across every team, archived ones included (DMs and group DMs once), filtered by type,
  by `mm_exclude_patterns` (decided before anything is fetched; a known channel's recorded display name counts
  too) and by state: a new channel without messages, or archived before the archive held it, is skipped; a known
  channel archived since keeps syncing until the server refuses it.
- Each channel backfills with `?before=` from its newest page until a page comes back short, then follows new
  posts with `?after=<newest id>`. The server compares `create_at` strictly (its `prev_post_id`/`next_post_id`
  too, so they are not trusted), so each page's cursor, the one requested next and the one recorded, overlaps the
  posts that share its edge timestamp: none is skipped at a page boundary unless a whole page (200 posts) shares
  one millisecond. A recorded cursor post deleted for good empties its pages: a backfill then pages back again
  from the newest page, and a forward pass whose channel `last_post_at` moved on pages back from the newest post
  to the known ones. `first_inaccessible_post_time` (the server's retention cut) is kept as
  `inaccessible_before`. Requests are paced at four per second; 429 honours `Retry-After`, 5xx and transport
  failures (truncated replies included) back off.
- A channel's fetched posts are merged into its file by post id (oldest first, a torn or duplicate line is
  dropped) and the file is replaced atomically — every 5,000 posts and when the channel is done — before the
  manifest records the cursors that cover them. An interrupted run keeps every finished channel and fetches again
  at most what it had not published; a failed write keeps the channel's last published cursors. Known posts and
  users are never removed.
- HTTP 403/404 on a channel (left, archived, no permission) marks it `unavailable` and keeps its file; it is not
  an error unless every channel was refused. Other failures count as `errors`, keep the published part and its
  cursor, and with `mm_sync` make the origin commit nothing that run.
- `users.json` maps user ids to `{username, nickname, first_name, last_name}`; each run refreshes this run's
  authors and resolves every author still missing.
- The archive is bound to the account and server that first synced into it (`account_changed` otherwise).
  `--max-items N` bounds channels; a run also stops after 40 minutes with `"limited": true`. Edits and deletions
  of posts already archived are not fetched again, as in mattertok.

```
python -s -B -m mattermost.collector --login
python -s -B -m mattermost.collector --auth-check
python -s -B -m mattermost.collector --max-items 20 --capture
python -s -B manage.py collect mattermost
```

The collector prints one JSON line of counts and categories only (`channels`, `posts_written`,
`files_written`, `excluded`, `unavailable`, `errors`, `limited`, `users`, `requests`); `posts_written` counts posts
new to the archive or changed, `files_written` the channel files that changed. Not ported: mattertok's
interactive commands (read, search, send, watch), its WebSocket live mode (the scheduled task's cursor sync
replaces it), attachment downloads and Markdown rendering; capture reads none of them.

The record format, canonical hashes and commit protocol follow `SPEC.md` and `SPEC-CLARIFICATIONS.md` of the
`llm-wiki` checkout. Committed records are kept in `<DATA_ROOT>/raw/ledger/<origin>/<YYYY>/` as gzipped JSONL
segments. Each capture commit has a manifest in `<DATA_ROOT>/raw/manifests/`; capture's fold is
`<DATA_ROOT>/raw/.state/state.sqlite`, and its run log is `<DATA_ROOT>/raw/log.md`.

## Commands

All examples run from `<PROJECT>`. `--config <cfg>` is optional and overrides the default above.

```
python -s -B manage.py setup mattermost [--from <cfg>|-] [--force]
python -s -B manage.py collect mattermost [--config <cfg>] [--input-set live|T1|T2] [--origins a,b,...] [--rebuild-index]
python -s -B manage.py search mattermost [QUERY] [--config <cfg>] [options]
python -s -B manage.py check mattermost [--config <cfg>] [--export-state <DATA_ROOT>/raw/.verify/<file>] [--rebuild-state]
python -s -B manage.py test mattermost
python -s -B manage.py schedule install|status|run|remove mattermost [--config <cfg>] [--python <exe>] [--run-now|--dry-run]
```

Brackets show optional arguments, not literal shell text. Capture returns 0 ok, 3 suspect, 75 busy, below the
disk floor or skipped, 2 refused, and 1 error. A suspect origin commits nothing; other origins may commit.
`check` verifies the folded state against the ledger, and its `--export-state` / `--rebuild-state` dump or
rebuild that state (the old export-state and replay). The root README's command table describes the whole
shared surface.

## Search

### index (collect's index step)

`collect`'s index step brings `<DATA_ROOT>\index\mattermost.sqlite` up to the committed ledger (the search reads only
`data_root` from the config):

- **Incremental.** Manifests are applied in seq order, one transaction per manifest together with the
  index's watermark (head seq, plus the sha256 and run id of that manifest).
- **Full rebuild.** It runs on `collect --rebuild-index`, when the index is missing, unreadable or stamped with another
  schema/tokenizer/extractor version, when the watermark no longer matches the ledger (the ledger was reset),
  when a manifest is not a capture manifest, or when the catch-up is very large.
- **Rebuild and catch-up give the same index.** Both run the same record handling in ledger order.
- **Only committed manifests are read.** If a manifest's segments are still `*.part`, capture is
  mid-commit: the index stops before it and reports `behind` (exit 0). An unreadable committed file is
  a ledger defect (exit 1).
- **Crash safety.** A rebuild is written to `<name>.sqlite.new`, checked with FTS5 `integrity-check`
  and `quick_check`, then swapped in. A crash never loses the live index.
- **One writer.** The index has a writer lock (exit 75 when busy). Searches open the index read-only and are
  never blocked by it.

### search mattermost

```
python -s -B manage.py search mattermost "<query>" --json
python -s -B manage.py search mattermost "<query>" --channel "<title fragment>" --since <date> --context 3
python -s -B manage.py search mattermost "<query>" --mode timeline
python -s -B manage.py search mattermost --channel "<title fragment>" --before <cursor>
python -s -B manage.py search mattermost --list-channels
python -s -B manage.py search mattermost --thread <post_id>
```

- **One row per post key.** Each key `mm:post:<id>` has one row, and its latest version wins (an edit, a new
  reply count, a move to another channel file).
- **Text.** It follows the knowledge-base corpus rule: the message; else the attachment pretext, title, text,
  fallback and fields; else the file names. `body_from` says which, and file names are searchable. A deleted
  post whose message the server emptied keeps its last captured text.
- **Flags.** `edited` means `edit_at > 0` or the text changed (a new version with the same text is not an
  edit). Deleted posts (`delete_at > 0`) and system posts (`system_*`) are hidden unless
  `--include-deleted` / `--include-system`.
- **Labels.** The sender is the username from the user records (first/last name and nickname are kept too).
  The channel label and team come from the channel records. Renames relabel old rows.
- **Filters.** `--channel` (label or URL-name substring, or channel id, repeatable), `--team`, `--sender`
  (username, name or nickname substring, or user id), `--since` / `--until`.
- **Threads.** `--thread <post_id>` takes any post of a thread and prints the root and every reply.
  Selection filters do not apply to a thread; each one that was given is reported on stderr.
- **Browsing.** Filters without a query browse: the command shows the newest page (in time order), and you
  walk through the pages with `--before` / `--after` cursors `<ts_ms>.<post_id>`.
- `--mode hits|count|timeline|senders|channels`; `--context N` shows N posts before and after each hit in the
  same channel, by time keyset; `--limit`, `--width`, and `--tz` (default `+09:00`; dates, month buckets and
  display use this fixed offset). `--until` is inclusive of the whole period given (`--until 2024-10` ends with
  the last second of October).
- `--json` prints an envelope: `source, mode, query, fts, verify, weak, exclude, exclude_fts, filters, tz,
  total, rows, built_at, updated_at, index{seq, ledger_seq, stale}`, plus `page` when browsing and the thread
  fields for a thread. Large ids are strings. `--explain` prints the FTS5 expression; the separator token is
  shown as `<S>`.
- A stderr note appears when the index is behind the ledger, and when a query falls back to a full scan.
- Exit codes: 0 ok (0 hits included) · 2 usage, bad query, missing or outdated index · 75 index lock busy
  · 1 unexpected.

There is no masking, pseudonymisation or redaction. Names, nicknames, channel titles, file names and texts are
shown as they are. Whoever uses the output applies their own privacy rules.

### Query language and tokenizer

| Query | Meaning |
|---|---|
| `배포 일정` | both terms (AND) |
| `"정기 회의"` | the exact phrase |
| `회의 OR 미팅` | either term |
| `배포 -롤백` | the first term, excluding the second |
| `api*` | prefix |

Text is normalised as NFC + casefold. Each post gets an n-gram shadow column: Korean/CJK runs become
overlapping bigrams and Latin/digit runs become one token. A query term is issued as an FTS5 phrase over the
same tokens. As a result:

- Korean matches as a substring: `배포` finds `배포를` and `재배포`.
- A Latin/digit run at a term's edge must match a whole run: `api` finds `API 키` but not `rapid`, and
  `api*` finds `apis`.
- Quoted phrases and terms with punctuation (`C++`, `AES-256`) match literally after normalisation.

Tokenizer `ngram-2` fixes two bugs of the knowledge-base engine (`ngram-1`) it was ported from:

1. **Missed matches.** `10월` did not find `10월에`, nor `3시` find `3시에` or `SQL문` find `SQL문을`. The
   query's lone `월` token never appeared in a document that held only `월에`. A Korean run that touches a
   Latin/digit run now also emits a one-character head or tail token, so the phrase lines up.
2. **False positives.** `가나다` matched `가나 나다`: the bigrams of two separate words lined up as one
   phrase. A separator token now stands between runs that whitespace or punctuation kept apart.

In addition, excluded terms use the same match rule as positive ones (`-api` no longer drops posts containing
`rapid`), and `OR` next to an excluded term is refused instead of being silently ignored. A property test in
`tests/core/test_textindex.py` checks the index against an independent model of these rules on thousands of
random queries.

An unquoted term with a Latin/digit run longer than 32,000 characters is refused: SQLite FTS5 cuts tokens at
32,768 bytes, so such a run would also match longer runs that start with it. Snippets mark matches with
the same case folding the search uses, so `straße` marks `STRASSE`.

## Scheduled task

`AppConfig.schedule` in `mattermost/apps.py` defines `\llm-wiki\mattermost-capture`: every three hours from 02:14
(local time), at most 1 hours per run, as SYSTEM. The generated Arguments are
`-s -B <PROJECT>\manage.py collect mattermost --config <cfg>`, with `<PROJECT>` as WorkingDirectory.

```
python -s -B manage.py schedule install mattermost [--config <cfg>] [--python <interpreter>] [--run-now|--dry-run]
python -s -B manage.py schedule status mattermost [--config <cfg>] [--python <interpreter>]
python -s -B manage.py schedule run mattermost
python -s -B manage.py schedule remove mattermost
```

Install, run and remove need elevation. Install validates the config, data root, interpreter and task checks
before registration, replaces a task of the same name and compares the definition read back. Dry-run prints
the definition without registering it. Status shows definition differences, last result and the newest run's
finished step lines; it returns 3 when attention is needed. A new start is ignored while one runs, and a missed
start runs when the machine returns.

`collect mattermost` runs the capture step (the live input set; with `mm_sync`, the collector first) and then the
index step after every capture exit code. With `mm_sync`, `task_checks` refuses a config without `mm_url` or an
existing protected token file. Only committed manifests enter the index, so the index step is a no-op when nothing new was committed.
Steps are logged in `<DATA_ROOT>/reports/task.log`, and collect returns capture's exit code.

## Slash command and tests

The `raw-search` skill (`.claude/skills/raw-search/SKILL.md`, generated from `tools/agent_templates/`) provides
`/raw-search` from the root project — one command for every app; it picks the app from the request. It searches
through the `llm_wiki_raw` MCP tools when they are connected, else `manage.py search --json`; it aggregates
first, widens empty results and cites dates, channels and masked senders. The search uses the app's default
config, as the CLI does.

```
python -s -B manage.py test mattermost
python -s -B manage.py test
```

App tests synthesize inputs in code under temporary directories. Search tests capture made-up inputs through
`mattermost/tests/ledgerfix.py` and compare incremental indexing with rebuilds, including resets, mid-commit,
defects, crashes, version checks, locks, threads, search modes and filters. Core tests use toy apps without
importing the real apps. See the root README for the full command table and core maintenance.
