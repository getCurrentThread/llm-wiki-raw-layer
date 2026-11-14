# mattermost: the Mattermost raw layer

This app captures private Mattermost conversations into an append-only, crash-safe raw ledger and builds an
SQLite FTS5 search index at `<DATA_ROOT>/index/mattermost.sqlite`. It shares the root project's **rawlayer**
framework: the root README holds what every app shares (commands, machine config and data root, the search
index and query language, scheduled tasks, tests) and `../CORE.md` the contract. Message data and machine config
remain outside the repository.
The implementation uses the Python standard library.

## Layout

`apps.py` (source keys, options and schedule), `origins.py`, `readers/`, `fetch.py`, `admin.py`, `keys.py`,
`vocab.py`, `search.py` and `config.example.json`. App tests (`tests/`) synthesize inputs in code under temporary
directories; the search tests capture made-up inputs through `tests/ledgerfix.py` and compare incremental indexing
with rebuilds over threads, the search modes and filters.

## Config, origins and records

`mattermost/config.example.json` shows the machine config shape; the config is
`<DATA_PARENT>/raw/mattermost/config.json` and `<DATA_ROOT>` is its folder (the root README has the common keys
and the data root's layout).

Capture reads the archive directory read-only. Its one origin is `mm_archive`, with record kinds
`ch`, `post`, `file`, `user` and `blob`. The archive contains `manifest.json` channel records, `posts/**.jsonl`
post records (one file per channel; each post's `metadata.files` become `file` records), `users.json` and, once
the fetch step keeps attached files, `files.json`: one `blob` record `mm:blob:<file_id>` per kept file with its
`post_id`, `sha256`, `size` and `path` (relative to the archive). The kept files themselves stay out of the
ledger and are never read by capture. It is read in one stable pass between two reads of the
manifest. A changed manifest retries the pass (at most three times); continued changes make the origin unstable.
Every version of a key seen in a stable pass is recorded once.

`sources.mm_archive` is the archive directory. `mm_exclude_patterns` is a list of non-empty strings (default
empty). A channel whose display name or name contains a pattern, casefolded, is excluded, using mattertok's rule;
none is excluded by default.

## Collection and authentication

`fetch.py` is a standard-library port of the mattertok archiver (REST API v4). It writes mattertok's archive
format, so an archive mattertok wrote continues from its cursors, and it needs no other program. Archive-only
configs stay read-only; `mm_sync: true` makes live `manage.py capture mattermost` update the archive before
capturing it. With `mm_sync`, `task_checks` (and `schedule install`)
refuse a config without `mm_url` or an existing protected token file.

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
- `mm_files_max_mb` (default 0: off) keeps each attached file of at most that many MB, after the channels and
  users, newest first, in the run's remaining time: `files/<id[:2]>/<id>` in the archive, without an extension
  (a kept `.exe` never runs on a click; the name is in the post). A file is written atomically before
  `files.json` records it, and `files.json` never shrinks. A kept file missing from disk is fetched again.
  Deleted files, files over the cap and the files of excluded or unavailable channels are not fetched, and
  downloads stop while the disk would fall below the capture floor plus 1 GiB. A failed or short download
  counts in `files_failed`, not `errors`, so it never holds back the capture of posts; a 401 still stops the run.
- The archive is bound to the account and server that first synced into it (`account_changed` otherwise).
  `--max-items N` bounds channels; a run also stops after 40 minutes with `"limited": true`. Edits and deletions
  of posts already archived are not fetched again, as in mattertok.

```
python -s -B -m mattermost.fetch --login
python -s -B -m mattermost.fetch --auth-check
python -s -B -m mattermost.fetch --max-items 20 --capture
python -s -B manage.py capture mattermost
```

The fetch step prints one JSON line of counts and categories only (`channels`, `posts_written`,
`files_written`, `excluded`, `unavailable`, `errors`, `limited`, `users`, `files_stored`, `files_failed`,
`requests`); `posts_written` counts posts new to the archive or changed, `files_written` the channel files that
changed, `files_stored` the attached files kept this run. mattertok's `send` is `admin.py post` (below). Not
ported: its other interactive commands (read, search, watch, whoami, doctor), its WebSocket live mode (the scheduled
task's cursor sync replaces it), reconcile, its attachment layout and Markdown rendering; capture reads none of
them. mattertok 0.1.0 itself (scrubbed of organisation values, with a feature map in `contrib/README.md`) is kept
in git history only, as archived reference code: `git checkout bee1809 -- mattermost/contrib`.

## Administration (admin.py)

`admin.py` is the write side: it creates and checks channels, their members and incoming webhooks, and sends
messages, with the fetch step's client, config and token (`mm_url`, `mm_token_file` or `MM_TOKEN`). Capture never
calls it.

```
python -s -B -m mattermost.admin plan   --spec <spec.json>
python -s -B -m mattermost.admin apply  --spec <spec.json> --out <DATA_PARENT>\...\result.json
python -s -B -m mattermost.admin verify --spec <spec.json> --out <DATA_PARENT>\...\result.json
python -s -B -m mattermost.admin post   (--spec <spec.json> --key <key> | --channel-id <id>) --message-file <file>
                                        [--out <result.json> --via-webhook] [--root-id <id>] [--pin] [--dry-run]
```

- The spec names one team and its channels by key: `{"team": "<team name>", "channels": {"<key>": {"name": "<url
  name>", "display_name": "...", "type": "P", "purpose": "...", "header": "...", "members": ["<username>"],
  "webhook": {"display_name": "...", "description": "..."}}}}` (`type` defaults to `P`; `purpose`, `header` and
  `webhook` are optional). It names people: keep it under `<DATA_PARENT>`, never in the repository.
- `plan` only reads: per key `new`, `exists` or `conflict` (a channel of that name with another type or display
  name), and how many members the server does not know or are not active members of the team.
- `apply` refuses before any write when a member is unknown or not in the team, when `--out` is not inside
  `DATA_PARENT` or belongs to another team. Then per key it creates the channel or adopts the one whose type and
  display name match (a conflict is never adopted), sets `header`/`purpose` when they differ, adds missing members
  and, when `webhook` is given, creates one incoming webhook with `channel_locked` (an unlocked one could post into
  any channel the token's user belongs to; an existing unlocked hook or several hooks fail the item). `--out`
  records each key's channel id, hook id and webhook URL; it holds secrets.
- `verify` re-reads everything: channel type, team, display name, header and purpose, members exactly the token's
  user plus the spec's members, exactly one locked webhook matching `--out`.
- `post` sends the message file (trailing whitespace dropped) as the token's user, or with `--via-webhook` through
  the key's webhook URL from `--out` (no Authorization header; the URL must be on `mm_url`). It is at most once: an
  identical message among the channel's newest 60 posts is not sent again. `--pin` pins it.
- Writes are never retried (a timeout could otherwise create a channel, hook or post twice); every create is
  preceded by a lookup, so rerunning after a failure adopts what the server made. Reruns change nothing when
  nothing differs.
- It prints one JSON line of statuses and counts per spec key, never a token, webhook URL, display name or
  username. Exit codes: 0 ok, 1 some keys failed or an error came after a write (rerun to continue), 2 refused
  before any write.

## Search

`search mattermost --help` lists the modes, filters and cursors; the index, the query language and the output are
the root README's ("Search").

- **One row per post key.** Each key `mm:post:<id>` has one row, and its latest version wins (an edit, a new
  reply count, a move to another channel file).
- **Text.** It follows the knowledge-base corpus rule: the message; else the attachment pretext, title, text,
  fallback and fields; else the file names. `body_from` says which. File names are searchable on every post
  (a post with text has them in its search shadow, not in its text). A deleted post whose message the server
  emptied keeps its last captured text.
- **Kept files.** Each hit's `stored` maps the file ids of its kept files to their paths relative to the
  archive (`sources.mm_archive`), from the `blob` records; `{}` when none is kept.
- **Flags.** `edited` means `edit_at > 0` or the text changed (a new version with the same text is not an
  edit). Deleted posts (`delete_at > 0`) and system posts (`system_*`) are hidden unless
  `--include-deleted` / `--include-system`.
- **Labels.** The sender is the username from the user records (first/last name and nickname are kept too).
  The channel label and team come from the channel records. Renames relabel old rows.
- **Threads.** `--thread <post_id>` takes any post of a thread and prints the root and every reply.
  Selection filters do not apply to a thread; each one that was given is reported on stderr.

## Wiki export

`mm_wiki_project`, `mm_wiki_include`, `mm_wiki_exclude` and `mm_wiki_since` configure `manage.py collect mattermost` (llm.md 13), which
writes each channel's closed months, packed into Markdown files of about 16,000 characters, into the wiki
vault's `raw/sources/mattermost/`. Empty by default: nothing is exported.
