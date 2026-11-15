# Notion raw-layer app

`nt_archive` reads atomic archive snapshots that the fetch step (`fetch.py`) takes from Notion Desktop's SQLite
database, with no token. Its ledger and `index/notion.sqlite` belong in its own protected data root.
`nt_sync: true` makes live `manage.py capture notion` update the archive first.

## Collection

The fetch step reads `nt_desktop_path` (default `%APPDATA%/Notion/notion.db`) through a read-only SQLite
connection and an in-memory backup including the live WAL. It selects only block/user/comment/discussion
tables; authentication tables are never queried or exported. Cached rich text is converted to block-level
archive records. Missing cached rows and order entries retain prior observations. Only explicit trash/deleted
or resolved flags retire records. The source DB is not modified.

Coverage is partial. Missing child blocks and CRDT-only bodies are counted, and previously captured content is
preserved. Readable siblings still import. CRDT decoding and exact internal database-property typing are not
implemented; cached authored property text remains searchable. A missing cache row does not establish a
server-side deletion or loss of access. `nt_page_ids` restricts collection; `--max-items` / `nt_max_pages` bound
page attempts. Keys of the removed API source (`nt_source`, `nt_cli`, `nt_comments`) are ignored.

```
python -s -B -m notion.fetch --auth-check
python -s -B -m notion.fetch --max-items 3 --capture
```

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
A page an older archive marked gone keeps its top-level `_gone_at`. Never delete known page files or ids.
`users.json` is a nonshrinking id→user-object map. Data source files contain the data source object (schema).
Normalize UUIDs to lowercase dashed form, including filenames. Duplicate ids inside a snapshot are deduplicated
(last object, first order entry). All rows from a malformed page are rejected (exit 75), without partial puts.
Removing a previously captured key from a healthy file makes the origin suspect (exit 3, no commit).

Publish a page via tmp + atomic replace. Retain authored properties, excluding computed formula/rollup payloads. Project media URL query
signatures and expiry_time out before hashing (image/file/pdf/video/audio, icon/cover and files properties).
Ordinary document hyperlinks retain their queries. Executable signature vectors are in
`tests/test_capture.py::test_url_rotation_no_rev`.

Keys: `nt:page:<pid>`, `nt:block:<pid>:<bid>`, `nt:comment:<pid>:<cid>`, `nt:ds:<id>`, `nt:user:<id>`.
Including the page in block/comment keys preserves moves and duplicate synced blocks across pages. Block
order is page-record derived data, not block native data: insertions/order-only changes do not reversion
every following block. A newly inserted block gets its own first version.

## Search

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
