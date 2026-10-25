# Gmail raw-layer app

`gm_archive` reads a private Gmail REST API v1 archive into its own append-only ledger and
`index/gmail.sqlite`. `gm_sync` defaults false: archive-only and frozen capture never contact an API.
With `gm_sync: true`, live `manage.py collect gmail` refreshes the archive before capture.
The implementation uses Python's standard library only. It never extracts credentials from a browser or mail client.

## Collection and authentication

Create a personal Google Cloud project, enable the Gmail API, configure its OAuth consent screen and create
an OAuth client of type **Desktop app**. Download its installed-app client JSON to `gm_client_file` under
`<DATA_PARENT>`. Use **In production**, without verification, for personal use under Google's fewer-than-100-user
[personal-use exception](https://support.google.com/cloud/answer/13464323). Expect the unverified-app consent
warning. Gmail-scoped refresh tokens issued while the app is **Testing** expire after seven days; a password
change also revokes Gmail refresh tokens. Re-authorize after revocation.
See Google's [refresh token expiration rules](https://developers.google.com/identity/protocols/oauth2#expiration).

Prepare the protected data root with `python -s -B manage.py setup gmail`, replace every placeholder in the
external config using `config.example.json`, and create its empty archive directory under the protected data
root with inherited ACLs. Both `gm_client_file` and `gm_token_file` must be absolute resolved paths under
`<DATA_PARENT>`; their parents pass the same ACL, git-tree and Windows Search precheck as data roots.
Keep `gm_sync: false` until manual genesis is complete. Do not put client JSON or tokens in this repository.

```
python -s -B -m gmail.collector --auth --config <CFG>
python -s -B -m gmail.collector --auth-check --config <CFG>
python -s -B -m gmail.collector --max-items 1000 --capture --config <CFG>
# Repeat the preceding command until limited=false; cursor stays unset during genesis.
python -s -B manage.py collect gmail --config <CFG>
# Set gm_sync=true in the external config if scheduled capture should sync the API.
python -s -B manage.py schedule install gmail --config <CFG>
```

`--auth` prints a consent URL to stderr. Open it manually in a browser and approve the read-only scope
`https://www.googleapis.com/auth/gmail.readonly`. The one-time installed-app flow uses an OS-assigned
`127.0.0.1` loopback port, random checked state and PKCE S256; it times out after five minutes. Code exchange
uses Google's fixed token endpoint. Only the refresh token is atomically persisted; access tokens are
refreshed on every run and remain in memory. See [installed-app OAuth](https://developers.google.com/identity/protocols/oauth2/native-app).
`--auth-check` refreshes and reads the profile; it does not publish message content. The collector otherwise
prints only vocabulary-validated aggregate counts/categories, never message text, addresses, IDs or credentials.

Genesis reads `users.getProfile` before listing, binds the lowercased account address's SHA-256 digest,
fully pages `messages.list(includeSpamTrash=true,maxResults=500)`, and obtains label membership through
per-label listings. A resumable pending-ID plan lives inside the archive. `--max-items` bounds raw-fetch
attempts (0 means unlimited); a run has a 3000-second fetch time budget. Each completed batch publishes
immutable content. Only an empty pending set sets the cursor to the profile historyId captured before
listing. Subsequent runs finish genesis/resync before history processing.

Incremental sync reads **all** history pages before publishing any event changes. It requests messageAdded,
messageDeleted, labelAdded and labelRemoved; only newly eligible, uncaptured messages need raw fetches.
Failed pages/fetches keep the old cursor and deletion state. Successful content already fetched may remain
published for safe replay. An expired historyId (HTTP 404) triggers a full ID diff and per-label membership
rebuild, with a fresh profile historyId taken before listing. Only a complete listing and completed pending
work can retire missing IDs. `--full` forces a fresh listing; it can replace a stalled pending plan if a
message was deleted before genesis fetched it. It never rewrites captured content.

The [Gmail quota table](https://developers.google.com/workspace/gmail/api/reference/quota) gives 6,000 units/minute
per user and 1,200,000/minute per project. Costs: messages.get 20 (all formats), messages.list 5, history.list 2,
labels.list 1, getProfile 1. The client uses a 5,000-unit/minute bucket with a 20-unit burst (about 250 raw
messages/minute before discovery/history costs), leaving headroom. It honors numeric/date Retry-After and
exponential backoff for 429 and rateLimitExceeded/userRateLimitExceeded 403, with at most six attempts;
transient 5xx responses also retry. Large mailboxes need repeated manual genesis runs.

Default collector exclusions, by API label ID: SPAM, TRASH, DRAFT, CATEGORY_PROMOTIONS, CATEGORY_SOCIAL,
CATEGORY_UPDATES. First-seen excluded messages have state only and are not raw-fetched. Previously captured
messages keep their content when excluded later. Removing an exclusion can make an unknown message eligible.
UNREAD is the default volatile state label and never changes a ledger signature. The reader applies no exclusions.
Labels may race between listing and get; a raw response that newly gained an excluded label is discarded.

Exit codes: collector 0 success or bounded genesis, 2 authentication/account refusal, 75 incomplete requests,
busy archive or incomplete incremental work, 1 unexpected/config failure. Capture uses the core's 0/2/3/75/1
contract; malformed/missing containers yield 75, and removing a retained key from a healthy container yields 3.
`--capture` runs capture/index once after collection, with automatic sync disabled for that child.

## Archive contract, version 1

```
archive/
  manifest.json
  labels.json
  exports/<YYYYMMDDTHHMMSSffffffZ>.json
  state/<first-one-or-two-hex-characters>.json
  _collector_identity.json
  _collector_state.json
  .collector.lock
```

The manifest and archive are private inputs, separate from the core's public capture manifests. The archive
manifest holds only file paths, history cursor, genesis status, generation and account binding hash; no
address, subject or label name is written there. Example (invented IDs and content):

```json
{
  "version": 1, "generation": "0123456789abcdef0123456789abcdef",
  "files": ["exports/20241001T000000000000Z.json"],
  "state_files": ["state/ab.json"], "cursor": "10", "genesis_complete": true,
  "binding_sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
}
```

Each partition is `{"version":1,"messages":[...]}`. Each message ID appears in exactly one immutable
partition, ever. A message has `id`, `thread_id`, `internal_date_ms`, present RFC headers (`message_id`,
`from`, `to`, `cc`, optional `bcc`, `subject`, `date`), `body_text`, `body_truncated`, attachment metadata,
and `size_estimate`. No raw MIME, label state, attachment bytes or per-fetch attachmentId is retained.
Partitions are never rewritten/deleted; file order equals manifest order, even if the clock moves backward.
`gm_partition_max_messages` defaults 1000; 0 uses that safe default, and positive values are accepted up to 100000.
Serialized partitions never exceed 16 MiB; one oversized message is refused.

State shards map message IDs to `{"label_ids":["INBOX"]}` or deleted state with additional `_removedAt`
(ISO-8601 UTC). Keys never disappear. `labels.json` maps API label IDs to `{id,name,type}`; a deleted label
is retained with `_removedAt`. A label ID is an API string such as INBOX or Label_1, not a message hex ID.
Message/thread IDs are TEXT, validated against `^[0-9a-f]{1,16}$`, with no padding or integer conversion.
Invalid primary keys are skipped and counted as `pk_bad`.

Publishing order is content partitions, changed state shards, labels.json, then manifest **last**. Replaced
files use temporary writes, fsync and atomic rename. Collector state is checkpointed after the manifest,
so a crash replays pending IDs. Valid orphan content partitions from a crashed publication are adopted on
the next sync without duplicating content; malformed orphan JSON requires repair rather than silent deletion.

The plain Origin returns one lazy container per manifest-listed file. Only one bounded content partition is
decoded at a time; no whole-mailbox body map is created. Missing/malformed partitions or shards fail their
container, preserving healthy sibling progress. All resolved paths stay below the archive. Orphan JSON is
counted; temporary and collector-private files are ignored. Manifest bytes are checked at discovery and
before/after each lazy read; concurrent publication can yield 75 and needs a retry, rather than a mixed cursor.

Keys: `gm:msg:<id>` (immutable), `gm:lbls:<id>` (mutable), `gm:label:<label-id>` (mutable directory).
Only msg uses day counts, from internal_date_ms. There is no thread ledger kind.

MIME extraction prefers text/plain over HTML. HTML drops script/style and uses block newlines. Charset
fallbacks are declared, UTF-8, CP949, EUC-KR, Latin-1, then replacement decoding. Whitespace is normalized;
HTTP(S) URL queries/fragments are removed from extracted text and headers before persistence.
`gm_body_max_bytes` defaults 131072 UTF-8 bytes (0 retains no body); truncation keeps valid UTF-8 and sets
`body_truncated`. This cap permits up to 16 MiB, subject to the stricter serialized partition cap.
Attachments retain MIME part path, filename, MIME type, decoded size and SHA-256 only.

## Search and schedule

```
python -s -B manage.py search gmail 배포 --label INBOX --json
python -s -B manage.py search gmail deployment --from sender@example.org --json
python -s -B manage.py search gmail --thread def123 --context 2 --json
python -s -B manage.py search gmail 배포 --mode labels --json
python -s -B manage.py search gmail 배포 --include-removed --json
```

FTS covers subject, From, To/Cc, body text and attachment filenames. Filters: repeatable `--label` (name
substring or exact ID; multiple flags are AND), `--from`/`--sender` (sender substring), `--thread` (thread ID),
`--since`, `--until`, `--limit`. Modes: hits, count, timeline, senders, labels, threads. Thread selection
honors other filters; context shows neighbors in that thread. `--before`/`--after` use `ts_ms.hex_id` cursors.
TRASH, SPAM and `_removedAt` are hidden unless `--include-removed` (or shared `--include-deleted`) is given.
Label aggregation counts each membership once; a message can belong to multiple labels. All modes use the
shared JSON envelope/watermark and Korean n-gram tokenizer. Search results are private plaintext.
One derivation handles state before/after content identically during incremental updates and rebuilds.

Frozen sources use `<inputs_root>/<set>/gm/archive`. The SYSTEM task `\llm-wiki\gmail-capture` starts at local
00:44 every three hours with a one-hour limit. When gm_sync is true, registration refuses missing or unsafe
client/token files. The owner must ensure SYSTEM can read those files through the protected inherited ACL.
Scheduled API sync should begin only after manual genesis and authentication checks succeed.

## Later versions and limitations

v2 may add Takeout seeding with an `rb` streaming MBOX splitter, reading Gmail's decimal ID from the mbox
`From <id>@...` separator; do not assume an X-GM-MSGID header. IMAP would use EXAMINE and BODY.PEEK only.
Neither source, desktop/cache reading, attachment content indexing nor credential extraction is part of v1.
Full discovery, history pages and lightweight ID/state maps occupy memory proportional to mailbox metadata;
message bodies stay partition-bounded. Raw API responses and MIME parsing still occupy one message's memory.
Snapshots preserve the final state observed per sync, not every intermediate label event between captures.
OAuth/GCP setup and live Google behavior require user verification; tests use only temporary synthetic data
and an injected fake HTTP transport.

## LLM Wiki export

`gm_wiki_project`, `gm_wiki_include`, `gm_wiki_exclude` and `gm_wiki_since` configure `tools/wikiexport.py gmail` (llm.md 13), which
writes the closed months, packed into Markdown files of about 16,000 characters with a section per mail, into an
LLM Wiki project's `raw/sources/gmail/`. Empty by default: nothing is exported.
