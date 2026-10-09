# Discord raw-layer app

`dc_archive` keeps a protected data root, append-only ledger and `index/discord.sqlite`.
The app includes a stdlib collector (`collector.py`) with API and token-free desktop-cache sources.
Archive-only configs remain read-only; `dc_sync: true` makes live `manage.py collect discord` update the
archive before capturing it. Frozen inputs never call a collector. Core files are unchanged.

## Collection and authentication

The configured source is `dc_source: "desktop"` or `"api"`. Desktop mode reads only Discord's HTTP cache,
extracting embedded JSON and bounded gzip payloads; it does not read cookies, Local Storage or session tokens.
`dc_desktop_path` defaults to `%APPDATA%/discord`. Cache coverage is partial: only locally retained messages
can be imported. Unknown guilds retain a `cache_unknown` label rather than being classified as DMs.
Cache eviction never produces tombstones, and older cached edits do not replace newer known edits.
When a previously unknown guild is classified later, channel metadata updates while the historical partition
directory remains fixed, preserving immutable source paths and manifest filename order.

API mode uses a supplied bot/user token (`dc_token_type: "auto"|"bot"|"user"`), in a private token file named
by `dc_token_file` under `<DATA_PARENT>`. In manual runs (`--auth-check`, `--capture`, `manage.py collect
discord`), `DISCORD_BOT_TOKEN` / `DISCORD_TOKEN` take precedence over the file when they are set. The SYSTEM
scheduled task sees no user variables, so with `dc_sync` on, `task_checks` (and `schedule install`) require an
absolute `dc_token_file` under `<DATA_PARENT>` whose folder passes the data-root precheck, the file itself, and
no machine-wide `DISCORD_BOT_TOKEN` or `DISCORD_TOKEN` (llm.md 6.2). In desktop mode, `dc_sync` needs an
absolute, existing `dc_desktop_path`, because SYSTEM's `%APPDATA%` is the system profile's.
The token is never printed or embedded in an archive. No token is extracted from Discord Desktop.
`dc_channel_ids` restricts API collection to selected channels; an empty list discovers guild channels,
active and joined archived threads, and user-visible DMs for user authentication. `dc_refresh_days` defaults
to 7. Empty refreshes cannot prove mass deletion because loss of history permission can also return an empty
array. Restricted message-content responses and failed channel fetches preserve the old cursor/partitions.

```
python -s -B -m discord.collector --auth-check
python -s -B -m discord.collector --capture
python -s -B manage.py collect discord
```

The collector CLI prints counts/categories only; `--max-items N` bounds channel publication for a diagnostic
run. `--capture` runs capture/index after collection, without collecting twice. Production configs and tokens
remain outside the checkout. The DCE port covers the JSON fields used by this app's projection; it does not
implement DCE HTML/text formats, media downloading, Markdown rewriting, or system-message fallback rendering.

## Archive contract, version 1

```
archive/
  manifest.json
  users.json
  exports/<guild_id>/<channel_id>/<YYYYMMDDTHHMMSSffffffZ>.json
```

Manifest:

```json
{
  "version": 1, "generation": "changes-every-run",
  "guilds": {"300": {"id": "300", "name": "Synthetic guild"}},
  "channels": {"100": {
    "id": "100", "type": "GuildTextChat", "guild_id": "300", "parent_id": null,
    "category_id": null, "category": "Synthetic category", "name": "Synthetic channel", "topic": "",
    "files": ["exports/300/100/20241001T000000000000Z.json"], "last_message_id": "1300000000000000000", "error": ""
  }}
}
```

Each immutable partition is a DCE JSON object with `guild`, `channel`, integer `messageCount`, and `messages`.
The count equals the array length; `channel.id` equals the manifest channel id. Keep partitions at most
10,000 messages; filename UTC timestamps must sort in the same order as `files`. Threads have their own
channel and files. DM guild id is the string `"0"`. `users.json` maps user ids to `{id,name,isBot,nicks}`;
`nicks` maps guild ids to nicknames. Each user object's id must match its map key. Never shrink the user set.
Message ids must be positive decimal snowflakes below `2**63` (the search index's signed INTEGER range);
invalid message/user ids are skipped and counted as `pk_bad`.

The collector publishes partitions, then users, then manifest, using temporary files plus atomic replace
with retries for Windows shared readers. Partitions and their manifest references are retained permanently.
New messages use `after=last_message_id`; an N-day refresh emits only changed projection signatures.
Messages missing from a completed refresh window are retained as copies with `_removedAt` (ISO UTC).
Do not remove known messages from an existing partition: missing keys in a successfully read container make
the entire origin suspect (exit 3, no commit). A missing or invalid partition invalidates all observations
for that channel (exit 75), including malformed embed URLs during projection; other healthy channels may
still commit. Orphan JSON files are counted, tmp files
are ignored. Paths must remain below `exports/`, including resolved symlink/junction paths.

The reader checks manifest bytes before and after the pass; changes retry three times at 0.5-second intervals.
Channel/guild metadata uses virtual manifest containers, so later exclusions become `room_gone`, not vanished.
Exclusions casefold-match guild name, category and channel name. `files`, cursors and errors are not hashed
as channel metadata. Message keys include the channel: `dc:msg:<channel_id>:<message_id>`, so a parent and
thread may retain the same starter id independently. Timestamps come from `(snowflake >> 22)+1420070400000`.

## Message signature and shared test vectors

`readers.archive.msg_native()` is the collector/reader projection contract:

- Keep id, type, timestamp, timestampEdited, isPinned, content and `_removedAt`.
- Keep author.id, attachments `{id,fileName,fileSizeBytes}`, stickers `{id,name}`.
- Keep embeds' text fields/URLs (remove URL queries/fragments), forwarded message text,
  reference `{messageId,channelId,guildId}`, interaction `{id,name}`.
- Omit signed attachment URLs, author display metadata, mentions, reactions and emoji URLs.

Executable vectors live in `tests/test_capture.py::test_projection_vector` and
`test_volatile_fields_no_revision`. Equal projections hash equally with
`canon.record_h(msg_native(message), {})`. Content/pin/edit/tombstone changes must hash differently.
Repeated identical signatures in immutable refresh partitions keep the first source of that signature;
the last observation still chooses the head. This avoids source-only refresh churn while retaining A→B→A.

## Use

```
python -s -B manage.py setup discord
# Fill sources.dc_archive with the absolute existing archive path in the external config.
python -s -B manage.py collect discord
python -s -B manage.py search discord deployment --guild Server --channel general --json
python -s -B manage.py search discord --thread 1300000000000000000 --json
python -s -B manage.py search discord --list-channels --json
python -s -B manage.py search discord --channel general --limit 30 --json
python -s -B manage.py search discord deployment --mode timeline --json
```

Filters: `--guild`, repeatable `--channel`, `--sender` (name/nickname/id), `--since`, `--until`,
`--include-deleted`. Modes: hits, count, timeline, senders, channels. `--context N` shows adjacent messages
in the same channel. `--before` / `--after` use `ts_ms.snowflake.channel_id` cursors; the channel tie-breaker
prevents skipping duplicate parent/thread starters. Threads include the named/root message, its matching
thread channel and reference replies. Query/selection filters do not cut a thread down. Parent renames update
`parent › thread` labels. Text fallback is content → embed text → filenames/forwarded text; tombstones keep
previous text when their content is empty. Search JSON contains freshness and watermark information.
Labels and text are private plaintext; follow the session's privacy rules when reporting search results.

Frozen input sets read `<inputs_root>/<set>/dc/archive`. Scheduled capture is SYSTEM at local 00:14 every
3 hours, with a 1-hour limit. Run genesis manually (or plan a 2-hour task limit) for a large archive.
With `dc_sync` disabled, an external collector must finish before capture starts. With it enabled, collection
runs inside the app's live read hook. Task registration is a separate operation.
To preview the task definition, supply a configured external config:
`python -s -B manage.py schedule install discord --config <CFG> --dry-run`.

## API implementation references

The API client pins v10, pages backward from the newest message in batches of 100, then publishes messages in
ascending snowflake order. Backward pagination covers bursts larger than 100 without relying on undocumented
`after` ordering. It handles rate-limit headers/429 backoff and binds an API archive to the authenticated
account digest. See [third-party references](../THIRD_PARTY.md) for the DCE mapping and cache-parser research.
