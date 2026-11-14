# Gmail raw-layer app

`gm_archive` reads a private Gmail archive, which the fetch step fills over IMAP, into its own append-only ledger
and `index/gmail.sqlite`. `gm_sync` defaults false: archive-only capture never contacts Gmail.
With `gm_sync: true`, live `manage.py capture gmail` refreshes the archive before capture.
The implementation uses Python's standard library only. It never extracts credentials from a browser or mail client.

## Collection and authentication

The fetch step signs in to `imap.gmail.com:993` (TLS) as `gm_imap_user` with a Google
[app password](https://support.google.com/accounts/answer/185833): no Cloud project, consent screen or OAuth
client. App passwords need 2-Step Verification, and none can be created when it uses security keys only, for a
work or school account, or with Advanced Protection. A password change revokes them. An app password grants the
whole mailbox, sending included, with no read-only scope, so the fetch step itself stays read-only: it selects
folders with EXAMINE, reads with UID FETCH and `BODY.PEEK[]` (no `\Seen` change) and UID SEARCH, and never sends
STORE, COPY, APPEND or EXPUNGE. Gmail keeps IMAP on for personal accounts.

Prepare the protected data root with `python -s -B manage.py setup gmail`; it fills `sources.gm_archive` and
`gm_imap_password_file` (`<DATA_ROOT>\app_password.txt`). Set `gm_imap_user` to the Gmail address. The password file
must be an absolute resolved path under `<DATA_PARENT>`; its folder passes the same ACL, git-tree and Windows
Search precheck as data roots. It holds the app password on one line; spaces (Google shows it in four groups)
and surrounding whitespace are ignored. Keep `gm_sync: false` until manual genesis is complete. Never put the app
password in this repository.

```
python -s -B -m gmail.fetch --auth-check --config <CFG>
python -s -B -m gmail.fetch --max-items 1000 --capture --config <CFG>
# Repeat the preceding command until limited=false.
python -s -B manage.py capture gmail --config <CFG>
# Set gm_sync=true in the external config if scheduled capture should sync Gmail.
python -s -B manage.py schedule install gmail --config <CFG>
```

`--auth-check` signs in; it does not publish message content. The fetch step otherwise prints only
vocabulary-validated aggregate counts/categories, never message text, addresses, IDs or credentials.

IMAP keeps no change history, so every run lists the whole mailbox: one LIST, then per folder (All Mail, Trash,
Spam) a `UID FETCH 1:* (UID X-GM-MSGID X-GM-LABELS FLAGS)` of a few hundred bytes per message, and five
`X-GM-RAW "category:..."` searches over All Mail. The sign-in comes before any listing, and the archive binds the
lowercased address's SHA-256 digest. Only the eligible messages the archive lacks are fetched, one
`BODY.PEEK[]` each; every listed message's labels are rewritten. `--max-items` bounds those fetches (0 means
unlimited) and a run has a 3000-second fetch time budget. Each completed batch publishes immutable content.
Only a run that fetched every eligible message retires the IDs it no longer lists; a failed or bounded run
retires nothing. `--full` changes nothing: every run is full.

Gmail's [IMAP extensions](https://developers.google.com/workspace/gmail/imap/imap-extensions) give X-GM-MSGID and
X-GM-THRID as the decimal form of the hex message and thread IDs of Gmail's web interface; the archive keeps the
hex. Labels get API-style IDs: `\Inbox`, `\Sent`, `\Important`, `\Starred` (or the `\Flagged` flag) and `\Draft`
map to INBOX, SENT, IMPORTANT, STARRED and DRAFT; a message without `\Seen` has UNREAD; Trash and Spam folder
messages have TRASH and SPAM. Inbox categories are no IMAP labels: the category searches give CATEGORY_PERSONAL,
CATEGORY_SOCIAL, CATEGORY_PROMOTIONS, CATEGORY_UPDATES and CATEGORY_FORUMS. A user label, decoded from modified
UTF-7, gets `Label_<16 hex>` from its name's SHA-256, so a renamed label is a new label and the old one is retired.
Gmail limits a user to 15 IMAP connections and, by report, about 2,500 MB of IMAP downloads a day: a large
mailbox's genesis may take several days of bounded runs.

Default fetch-step exclusions, by label ID: SPAM, TRASH, DRAFT, CATEGORY_PROMOTIONS, CATEGORY_SOCIAL,
CATEGORY_UPDATES. First-seen excluded messages have state only and are not raw-fetched. Previously captured
messages keep their content when excluded later. Removing an exclusion can make an unknown message eligible.
UNREAD is the default volatile state label and never changes a ledger signature. The reader applies no exclusions.
Labels may race between listing and fetch; a fetched message that newly gained an excluded label is discarded.

Exit codes: fetch step 0 success or bounded genesis, 2 authentication/account refusal, 75 incomplete requests,
busy archive or incomplete work, 1 unexpected/config failure. Capture uses the core's 0/2/3/75/1
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
  .collector.lock
```

The manifest and archive are private inputs, separate from the core's public capture manifests. The archive
manifest holds only file paths, generation and account binding hash; no
address, subject or label name is written there. Example (invented IDs and content):

```json
{
  "version": 1, "generation": "0123456789abcdef0123456789abcdef",
  "files": ["exports/20241001T000000000000Z.json"],
  "state_files": ["state/ab.json"],
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
(ISO-8601 UTC). Keys never disappear. `labels.json` maps label IDs to `{id,name,type}`; a deleted label
is retained with `_removedAt`. A label ID is a system ID such as INBOX or a user label's `Label_<16 hex>`,
not a message hex ID.
Message/thread IDs are TEXT, validated against `^[0-9a-f]{1,16}$`, with no padding or integer conversion.
Invalid primary keys are skipped and counted as `pk_bad`.

Publishing order is content partitions, changed state shards, labels.json, then manifest **last**. Replaced
files use temporary writes, fsync and atomic rename. The next run lists the mailbox again, so a crash loses
no pending work. Valid orphan content partitions from a crashed publication are adopted on
the next sync without duplicating content; malformed orphan JSON requires repair rather than silent deletion.

The plain Origin returns one lazy container per manifest-listed file. Only one bounded content partition is
decoded at a time; no whole-mailbox body map is created. Missing/malformed partitions or shards fail their
container, preserving healthy sibling progress. All resolved paths stay below the archive. Orphan JSON is
counted; temporary and fetch-private files are ignored. Manifest bytes are checked at discovery and
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
Label aggregation counts each membership once; a message can belong to multiple labels. The index, the query
language and the output are the root README's ("Search").
One derivation handles state before/after content identically during incremental updates and rebuilds.

The SYSTEM task `\llm-wiki\gmail-capture` runs on the
schedule in [../README.md](../README.md#scheduled-tasks). When gm_sync is true, registration refuses an empty
`gm_imap_user` and a missing or unsafe app password file. The owner must ensure SYSTEM can read that file through
the protected inherited ACL. Scheduled sync should begin only after manual genesis and `--auth-check` succeed.

## Limitations

Takeout seeding, desktop/cache reading, attachment content indexing and credential extraction are not
implemented.
The listing and lightweight ID/state maps occupy memory proportional to mailbox metadata;
message bodies stay partition-bounded. One fetched message and its MIME parsing occupy one message's memory.
Snapshots preserve the final state observed per sync, not every intermediate label event between captures.
Tests use only temporary synthetic data and an injected fake IMAP connection.

## Wiki export

`gm_wiki_project`, `gm_wiki_include`, `gm_wiki_exclude` and `gm_wiki_since` configure `manage.py collect gmail` (llm.md 13), which
writes the closed months, packed into Markdown files of about 16,000 characters with a section per mail, into the
wiki vault's `raw/sources/gmail/`. Empty by default: nothing is exported.
