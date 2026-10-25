# Claude Code raw-layer app

`cc_archive` keeps a protected data root, append-only ledger and `index/claudecode.sqlite` of the owner's
Claude Code conversations: prompts, Claude's reply text and a one-line summary of each tool call. Tool results,
thinking and attachments are left out. The app includes a stdlib collector (`collector.py`) that copies what it
keeps from the owner's `.claude` folder into the archive. Claude Code deletes transcripts after about 30 days; the
archive keeps them. With `cc_sync: true`, live `manage.py collect claudecode` updates the archive before capturing
it. Frozen inputs never call the collector. Core files are unchanged.

```
python -s -B -m claudecode.collector --max-items 20
python -s -B -m claudecode.collector --capture
python -s -B manage.py collect claudecode
```

## Options

| option | default | meaning |
|---|---|---|
| `cc_sync` | `false` | run the collector before live capture |
| `cc_claude_dir` | `""` | absolute path of the owner's `.claude` folder; `setup` fills `<HOME>\.claude`. Required with `cc_sync`, because the scheduled task runs as SYSTEM, whose home is another profile |

`sources.cc_archive` is `<DATA_ROOT>\archive`.

## Sources the collector reads (read-only)

Paths are relative to `cc_claude_dir`:

| source | container id | kind |
|---|---|---|
| `projects/<project>/<sessionId>.jsonl` | `<sessionId>` | `session` |
| `projects/<project>/<sessionId>/subagents/<stem>.jsonl` (with `<stem>.meta.json` beside it) | `<sessionId>.<stem>` | `subagent` |
| `projects/<project>/<sessionId>/subagents/workflows/<run>/agent-*.jsonl` (Workflow agents, meta beside it; a resumed run's copy of the same agent shares its container, and `journal.jsonl` is not read) | `<sessionId>.<stem>` | `subagent` |
| `history.jsonl` | `history` | `history` |

A container id must be a valid key part (`rawlayer.capture.canon.pk_text`: non-empty, no `:`, no control
characters); a source whose id is not is skipped and counted in `errors`.

## Projection (transcripts)

Each line of a transcript is one JSON entry. Only complete lines count: a last line without a trailing `\n` is
still being written and is left for the next run. Only entries with `type` `user` or `assistant` and a
`message.content` (a string, or a list of blocks) are considered. An entry is skipped when:

- `isMeta` or `isCompactSummary` is true;
- it is a `user` entry whose content blocks are all `tool_result`;
- its text, after stripping leading whitespace, starts with `<local-command-stdout>`, `<bash-stdout>`,
  `<bash-stderr>`, `<local-command-stderr>` or `<task-notification>`;
- after the rules below it has neither text nor tools.

From the remaining entries: `text` blocks (or the string content) are joined with `"\n\n"`; `thinking`,
`redacted_thinking`, `image` and `tool_result` blocks are dropped; each `tool_use` block becomes
`{"name": <name>, "summary": <summary>}`. The summary is, for `Bash` and `PowerShell`, the `description` and the
first 200 characters of `command`, joined with `" | "` (either may be empty); for any other tool, the first
200 characters of the first non-empty string among `file_path`, `path`, `pattern`, `url`, `query`,
`description`, `prompt`; else `""`.

## Projection (history)

`history.jsonl` lines are `{"display", "pastedContents", "timestamp" (epoch ms), "project", "sessionId"}`. A
line is archived only when its `sessionId` has no `session` or `subagent` container (in the manifest or among
the transcript files seen this run) and its `timestamp` is more than 24 hours old; `pastedContents` is never
kept. This backfills prompts whose transcripts Claude Code already deleted, without duplicating live sessions.

## Row (the native record)

Every row carries exactly these fields (strings default to `""`):

| field | transcripts | history |
|---|---|---|
| `session` | `sessionId` | `sessionId` |
| `agent` | subagent `<stem>`, else `""` | `""` |
| `uuid` | entry `uuid` | `h<timestamp>.<sessionId>` |
| `parent` | `parentUuid` or `""` | `""` |
| `n` | 0-based line number in the source file | `timestamp` |
| `ts` | entry `timestamp` (ISO 8601 UTC, as stored) | `timestamp` as ISO 8601 UTC with milliseconds and `Z` |
| `role` | `user` or `assistant` | `prompt` |
| `cwd` | `cwd` | `project` |
| `branch` | `gitBranch` | `""` |
| `model` | `message.model` (assistant) | `""` |
| `text` | joined text | `display` |
| `tools` | list of `{"name", "summary"}` | `[]` |
| `from` | `transcript` | `history` |

The ledger key is `cc:msg:<container id>:<uuid>`; kind `msg`; `ts_ms` is `ts` in epoch milliseconds.

## Archive layout (`sources.cc_archive`)

```
manifest.json
exports/<container id>/<YYYYMMDDTHHMMSSffffffZ>.json     immutable partitions, never rewritten
.collector.lock
```

`manifest.json`:

```json
{"version": 1, "generation": "<uuid4 hex, new on every publish>",
 "files": {"<source path relative to cc_claude_dir, posix>": {"size": 0, "mtime_ns": 0, "container": "<id>"}},
 "containers": {"<id>": {"id": "<id>", "kind": "session|subagent|history", "session": "<sessionId or ''>",
                         "agent": "<stem or ''>", "project": "<projects/ folder name or ''>",
                         "agent_type": "<meta agentType or ''>", "description": "<meta description or ''>",
                         "files": ["exports/<id>/<stamp>.json"]}}}
```

A partition is `{"container": "<id>", "exportedAt": "<ISO now>", "rows": [<row>...], "rowCount": <len(rows)>}`,
at most 10000 rows. A container's `files` list is in ascending (stamp) order without duplicates; a new partition's
stamp is later than the container's last one. Within a container a `uuid` appears once: a run writes only rows
whose `uuid` is not in the container's existing partitions. Rows are immutable once archived.

The collector reads a source file only when its `(size, mtime_ns)` differs from `files` (or with `--full`).
When a source file disappears its `files` entry, container and partitions stay: Claude Code's cleanup is not a
deletion. Nothing in the archive is ever deleted or rewritten except `manifest.json`, published atomically last.

## Collector interface

`claudecode/collector.py` follows `discord/desktop.py` and `tools/archiveio.py`:

- `sync(root, claude_dir, max_items=0, full=False, clock=None) -> stats`: under `archiveio.locked(root)`;
  partitions with `archiveio.atomic(..., immutable=True)`; manifest last. `max_items` bounds the number of
  changed source files processed (then `limited: true`). `clock` (epoch ms) is for tests (the 24-hour rule).
- `sync_config(cfg, auth_only=False, full=False, max_items=0)`: refuses with `CollectorError("source_missing")`
  unless `cfg.cc_claude_dir` is an absolute existing folder; `auth_only` returns
  `{"status": "local_ready", "authentication_required": False}`; else `sync(cfg.sources["cc_archive"],
  cfg.cc_claude_dir, max_items, full)`.
- `if __name__ == "__main__": raise SystemExit(archiveio.main("claudecode", sync_config))`.
- stats: `status` (`ok`), `files_seen`, `files_changed`, `files_gone`, `rows_written`, `partitions_written`,
  `lines_bad`, `errors`, `limited`. A source file that cannot be read counts in `errors` (its previous archive
  stays), and any error makes live capture stop with `collection_incomplete`. Normal conditions are therefore not
  errors: a file that grows while it is read is archived up to its last complete line and re-read next run, and a
  complete line that is not JSON is skipped and counted in `lines_bad`. `files_gone` counts sources no longer
  present. Output and exceptions carry
  counts and categories only: never text, paths, ids or names.

## Search

`manage.py search claudecode [QUERY] --json`: modes `hits`, `count`, `timeline`, `senders` (by role),
`sessions`, `projects`; filters `--project` (working folder), `--session ID` (a whole session in order),
`--role`, `--list-sessions`, `--before`, `--after`. `search claudecode --help` is the reference.

## Privacy

The archive, ledger and index live only under the protected `<DATA_PARENT>\claudecode`. Tool results are not
kept, so file contents and other ledgers' search output are not copied; but a token typed into a prompt or a
`Bash` command's first 200 characters is kept as typed. Answers built from this app follow AGENTS.md's masking
rules.
