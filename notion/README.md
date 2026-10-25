# Notion raw-layer app

`nt_archive` reads atomic archive snapshots. The app includes a token-free Notion Desktop SQLite reader and
an official `ntn` API collector. Its ledger and `index/notion.sqlite` belong in its own protected data root.
`nt_sync: true` makes live `manage.py collect notion` update the archive first; frozen inputs stay read-only.

## Collection and authentication

`nt_source: "desktop"` reads `nt_desktop_path` (default `%APPDATA%/Notion/notion.db`) through a read-only SQLite
connection and an in-memory backup including the live WAL. It selects only block/user/comment/discussion
tables; authentication tables are never queried or exported. Cached rich text is converted to block-level
archive records. Missing cached rows and order entries retain prior observations. Only explicit trash/deleted
or resolved flags retire records. The source DB is not modified.

Desktop coverage is partial. Missing child blocks and CRDT-only bodies are counted, and previously captured
content is preserved. Readable siblings still import. CRDT decoding and exact internal database-property
typing are not implemented; cached authored property text remains searchable. A missing cache row does not
establish a server-side deletion or loss of access.

`nt_source: "api"` invokes the installed official `ntn` binary (`nt_cli` can supply its absolute executable
path), using its existing per-user keychain authentication or `NOTION_API_TOKEN`. No keychain token is printed,
copied into config, or persisted in archives. The API version is pinned to `2026-03-11` and calls use the
production API host. `nt_page_ids` restricts collection; empty means discover accessible pages/data sources.
`nt_comments` defaults true; false preserves known comments and disables comment queries. Missing directory
permission does not prevent content collection: known user labels remain and author UUIDs are retained.

```
ntn login
python -s -B -m notion.collector --auth-check
python -s -B -m notion.collector --max-items 3 --capture
python -s -B manage.py collect notion
```

`--max-items` / `nt_max_pages` bound page attempts, not the number of API requests within a page. API fetching
includes recursive children and per-block comments, so large pages can require many requests. Search,
data-source query, block children, properties, users and comments paginate explicitly. Partial fetches keep
the previous complete snapshot. Initial/daily/full sweeps recheck known pages independently of edit-time
watermarks; `--full` forces an API sweep. Responses/logs are never printed by the collector; output is aggregate.
API authentication is verified with a read-only `/v1/users/me` call.

## Archive contract, version 1

```
archive/
  manifest.json
  users.json
  pages/<page_id[:2]>/<page_id>.json
  ds/<data_source_id>.json
```

The informational manifest is `{version:1,generation,pages,data_sources}`. Page metadata includes path, title,
parent, last_edited_time, in_trash and fetched_at; data source metadata includes path, title and parent.
The reader enumerates files directly and does not use manifest membership to delete records.

Each page file:

```json
{
  "page": {"id": "00000000-0000-0000-0000-000000000001", "properties": {}, "in_trash": false},
  "order": [["00000000-0000-0000-0000-000000000002", 0, null]],
  "blocks": {"00000000-0000-0000-0000-000000000002": {
    "id": "00000000-0000-0000-0000-000000000002", "type": "paragraph", "paragraph": {"rich_text": []}
  }},
  "comments": {}
}
```

Block/comment maps retain every known id. Removed blocks and resolved comments carry `_removed_at` (ISO UTC);
404/permission loss retains the page file and adds top-level `_gone_at`. Never delete known page files or ids.
`users.json` is a nonshrinking id→user-object map. Data source files contain the data source object (schema).
Normalize UUIDs to lowercase dashed form, including filenames. Duplicate ids inside a snapshot are deduplicated
(last object, first order entry). All rows from a malformed page are rejected (exit 75), without partial puts.
Removing a previously captured key from a healthy file makes the origin suspect (exit 3, no commit).

Publish a page only after fetching it completely, via tmp + atomic replace. Re-fetch when page edited time is
at least floor-minute(previous fetched_at), or its trash state or block set changes; minute-level timestamps
need overlap. Retain authored properties, excluding computed formula/rollup payloads. Project media URL query
signatures and expiry_time out before hashing (image/file/pdf/video/audio, icon/cover and files properties).
Ordinary document hyperlinks retain their queries. Executable signature vectors are in
`tests/test_capture.py::test_url_rotation_no_rev` and `test_projection_vector`.

Keys: `nt:page:<pid>`, `nt:block:<pid>:<bid>`, `nt:comment:<pid>:<cid>`, `nt:ds:<id>`, `nt:user:<id>`.
Including the page in block/comment keys preserves moves and duplicate synced blocks across pages. Block
order is page-record derived data, not block native data: insertions/order-only changes do not reversion
every following block. A newly inserted block gets its own first version.

## Search

```
python -s -B manage.py setup notion
# Fill sources.nt_archive with the absolute existing archive path in the external config.
python -s -B manage.py collect notion
python -s -B manage.py search notion deployment --page Project --context 2 --json
python -s -B manage.py search notion --page-text 00000000-0000-0000-0000-000000000001 --json
python -s -B manage.py search notion --list-pages --json
python -s -B manage.py search notion deployment --mode pages --json
```

Each block is a searchable row; page title/properties are one synthetic page row, and comments have their own
rows. Rich text, equations, table cells, captions and authored properties are extracted. Filters: repeatable
`--page` (title substring/UUID), `--sender`, `--since`, `--until` (last-edited time), `--include-deleted`.
Modes: hits, count, timeline, senders, pages. `--context N` uses document order, not edit time.
`--page-text` returns the title/properties, blocks in order, then comments; query/selection filters do not
cut down this whole-page mode. `--before` / `--after` browse by printed `ts_ms.nt:key` cursors.

`record()` updates only source columns/tables. One shared SQL derivation calculates order, depth, page title,
sender and deletion for incremental manifests and rebuilds. A block is deleted when tombstoned, its page is
trashed/gone, or it is missing from current page order; missing pages leave order NULL. Thus blocks arriving
before their page still fold deterministically. Breadcrumbs query parent links recursively, with cycle guards.
Search JSON includes watermark/freshness. Search results are private plaintext.

Frozen inputs read `<inputs_root>/<set>/nt/archive`. SYSTEM capture starts local 03:29 every 6 hours, with a
1-hour limit. Desktop mode can use this task. API sync with `ntn` must run as the owning user because its
keychain is per-user; the existing SYSTEM task installer refuses `nt_sync` with `nt_source: "api"`.
Archive-only SYSTEM capture can still consume an archive completed by a user-account API collector.
Preview with `python -s -B manage.py schedule install notion --config <CFG> --dry-run` after preparing the
external config and its data root.

## API collection details

The collector overlaps minute-level edit timestamps and performs daily full sweeps. Child pages/databases
remain separate containers; synced blocks fetch original children. It queries comments on each block instead
of assuming page-level queries include inline discussions. Failed child/comment requests cannot mark a page
gone: only an inaccessible page lookup does so. Backoff handles throttling/transient failures. See
[third-party references](../THIRD_PARTY.md) for SQLite cache and API sources.

## LLM Wiki export

`nt_wiki_project`, `nt_wiki_include`, `nt_wiki_exclude` and `nt_wiki_since` configure `tools/wikiexport.py notion` (llm.md 13), which
writes one Markdown file per page (headings kept) into an LLM Wiki project's `raw/sources/notion/`. Empty by
default: nothing is exported.
