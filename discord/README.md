# Discord raw-layer app

`dc_archive` keeps a protected data root, append-only ledger and `index/discord.sqlite`. The fetch step
(`fetch.py`) reads Discord Desktop's HTTP cache; with `dc_sync: true`, `manage.py capture discord` runs it
before capturing the archive.

## Collection

The fetch step reads only Discord's HTTP cache under `dc_desktop_path` (default `%APPDATA%/discord`),
extracting embedded JSON and bounded gzip payloads; it never reads cookies, Local Storage or session tokens, and
needs no token. Coverage is partial: only locally retained messages can be imported. Unknown guilds keep a
`cache_unknown` label rather than being classified as DMs. Cache eviction never produces tombstones, and older
cached edits do not replace newer known edits. When a previously unknown guild is classified later, channel
metadata updates while the historical partition directory stays fixed, preserving immutable source paths and
manifest filename order. `dc_channel_ids` keeps only the channels it lists. With `dc_sync` on, `task_checks`
needs an absolute, existing `dc_desktop_path`, because SYSTEM's `%APPDATA%` is the system profile's. Keys of
the removed API source (`dc_source`, `dc_token_file`, `dc_token_type`, `dc_refresh_days`) are ignored.

```
python -s -B -m discord.fetch --auth-check
python -s -B -m discord.fetch --capture
```

The fetch step prints counts only; `--max-items N` bounds channel publication. `--capture` runs capture/index
after collection, without collecting twice. The DCE port covers the JSON fields this app's projection uses; it
does not implement DCE HTML/text formats, media downloading, Markdown rewriting or system-message rendering.

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

The fetch step publishes partitions, then users, then manifest, using temporary files plus atomic replace
with retries for Windows shared readers. Partitions and their manifest references are retained permanently.
Only messages whose projection signature changed are published again. A message an older archive marked
deleted carries `_removedAt` (ISO UTC).
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

`origins.msg_native()` is the fetch/reader projection contract:

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

## Search

Filters: `--guild`, repeatable `--channel`, `--sender` (name/nickname/id), `--since`, `--until`,
`--include-deleted`. Modes: hits, count, timeline, senders, channels. `--context N` shows adjacent messages
in the same channel. `--before` / `--after` use `ts_ms.snowflake.channel_id` cursors; the channel tie-breaker
prevents skipping duplicate parent/thread starters. Threads include the named/root message, its matching
thread channel and reference replies. Query/selection filters do not cut a thread down. Parent renames update
`parent › thread` labels. Text fallback is content → embed text → filenames/forwarded text; tombstones keep
previous text when their content is empty.

## References

The archive format follows DiscordChatExporter (DCE) JSON, and the desktop-cache scan follows discrawl's
MIT-licensed discorddesktop scanner.
