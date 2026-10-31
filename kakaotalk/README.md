# kakaotalk: the KakaoTalk raw layer

This app captures private KakaoTalk conversations into an append-only, crash-safe raw ledger and builds an
SQLite FTS5 search index at `<DATA_ROOT>/index/kakaotalk.sqlite`. It shares the root project's **rawlayer**
framework (see `../README.md` and `../CORE.md`). Its app label and package are its folder name, `kakaotalk`.
Message data and machine config remain outside the repository.
The implementation uses the standard library plus pycryptodome for `.edb` decryption.

## Scope and authorization

This app runs on the owner's own machine and opens only the owner's own, signed-in KakaoTalk account: the
client's local databases and keystore, and the owner's device backups — a personal archival and search tool for
one's own history, not a collection tool for anyone else's data. Everything stays local: sources are read in
place, plaintext exists only under `<DATA_ROOT>` (outside this repository, excluded from Windows Search),
nothing captured is sent anywhere, and keys are credentials that never leave the data root and are never printed. The
Windows key hierarchy and the memory fallback are the owner's kakatok research (its notes live outside this
repository); the macOS side ports the MIT-licensed kakaocli derivation (below). `admin.py` posts only as the
owner's own signed-in account, one message at a time, owner-invoked — the write-side counterpart of mattermost
admin.py's webhook post.

## Layout

Run commands from `<PROJECT>`, the root containing `manage.py`, `config/settings.py`, and `rawlayer/`.
This app's folder contains `apps.py` (source keys, options, schedule and the setup hooks), `origins.py`,
`inputs.py`, `readers/`, `keys.py` (value rules), `vocab.py`, `search.py`, `provision.py` (setup's defaults and
the provisioning of the root key, keystore, backup and macOS database), `backup.py` (the EvaSQLite backup
decryption), `admin.py` (the write side — scriptable sending), `tests/`, and `config.example.json`. `state.py`
declares the `schema_base` and `edb_salt` state
tables.
`katok_decrypt.py`, `keystore_v2.py` and `keychain.py` are the in-repo key derivation and decryption toolkit
ported from kakatok (the owner's prior research project; its notes live outside this repository), and
`kakaocli.py` is the macOS port of Jaeha0526/kakaocli (kakao-mcp, MIT — see the
macOS section below). Core tests are at `<PROJECT>/tests/core/`;
app tests are at `<PROJECT>/kakaotalk/tests/`. The slash command is
`<PROJECT>/.claude/commands/raw-search.md` (shared with mattermost).

## Config, origins and records

`kakaotalk/config.example.json` shows the machine config shape. The default config is
`<DATA_PARENT>/kakaotalk/config.json`. Common keys are `v` (1), `impl` (matching settings.IMPL_NAME), `data_root`,
`inputs_root`, and `sources`. Source paths must be absolute and exist; replace the example's placeholders with
machine-specific paths. `<DATA_ROOT>` conventionally means `<DATA_PARENT>/kakaotalk`. It must be inside
`DATA_PARENT`, outside git work trees, with the parent's required ACL and Search exclusion. Frozen input sets
need an existing `inputs_root`; live input resolution does not require it to exist.

Capture reads the decrypted backup room databases and the client's encrypted `.edb` room files read-only.
Origins, in ledger order, are `backup_decrypted`, `edb`, and `macdb` (the macOS client's decrypted database;
see the macOS section). The kakatok-era export origins (`merged_out`, `live_out`, `decrypted_live`) are gone:
the app reads the client itself now, and the labels those exports carried come from the client's own
databases. The message key is `kakao:<chatId>:msg:<logId>` across origins. Records have kinds `schema`,
`msg`, `room`, `hist`, `meta`, and `attachment`. The `edb` origin
uses guarded pending-to-absent detection when a key disappears from a completely read room, and titles its
rooms from the client's own chat-list database (`chat_data/chatListInfo.edb`, decrypted with the room keys —
the same store wraps its DEK): the client-materialised `chatRoomTitle` first, then the type-3 room-title meta,
then the displayed member names, the Windows reading of the macOS reader's title chain. The same store keys
the client's user database (`TalkUserDB.edb`, at the user directory's root), whose `talkUser` names label the
senders: the account's display name (`nickName`), with the owner's own alias (`friendNickName`) only where
the account has none — the join the kakatok research documented as the sender reconstruction. Both are read
best-effort — a missing key means untitled or unnamed
rooms, never an error — and only label-bearing columns are read, so records change only when the labels
themselves change. The backup keeps its names the same way: setup's backup step decrypts the backup's own
chat-list database and writes each room's title into the `_manifest.json` entry the `backup_decrypted` reader
passes through as the room record's derived fields, while the decrypted `TalkUserDB.sqlite` beside the rooms
names its senders.

| `sources` key | input |
|---|---|
| `backup_decrypted` | directory of decrypted backup room databases |
| `kakao_user_dir` | client user directory; encrypted rooms are under `chat_data` |
| `keystore` | decryption keystore file |

`mac_db` is an optional
top-level path (default null): the macOS client's decrypted database, what the `macdb` origin captures. It is
an option, not a source, so configs on machines without macOS inputs stay valid with no placeholder; when
set, its parent directory must exist (setup's provisioning writes the file there).
For frozen `T1`/`T2` inputs, encrypted rooms come from `<inputs_root>/<set>/kakao_user/chat_data`. Static
inputs remain at their configured paths and are checked against the freeze's hashes.

The record format, canonical hashes and commit protocol follow `SPEC.md` and `SPEC-CLARIFICATIONS.md` of the
`llm-wiki` checkout. Committed records are kept in `<DATA_ROOT>/raw/ledger/<origin>/<YYYY>/` as gzipped JSONL
segments. Each capture commit has a manifest in `<DATA_ROOT>/raw/manifests/`; capture's fold is
`<DATA_ROOT>/raw/.state/state.sqlite`, and its run log is `<DATA_ROOT>/raw/log.md`.

## In-repo key derivation and decryption (the kakatok port)

The app is self-sufficient: it no longer needs kakatok's research pipeline to produce the keystore or the
decrypted backups. The ported toolkit (originally `sample/kakatok`, removed after the port) lives in the app
package:

| module | what it does |
|---|---|
| `kakaotalk/keystore_v2.py` | parses the client's CBOR keystore `appstate.dat` / `keystore.bin` (`info_prefix`, `salt`, `wrapped_dek_map`); the authoritative room inventory |
| `kakaotalk/keychain.py` | the key hierarchy: `KEK = HKDF-SHA256(root, salt, info_prefix + relpath verbatim)`, `DEK = AES-KW-unwrap(KEK, wrapped_dek)` (RFC 5869 / 3394); the page-1 header oracle `verify_dek`; the key-source rotation fingerprint |
| `kakaotalk/katok_decrypt.py` | EvaSQLite (pre-25.7.2 backups): registry `DeviceInfo` pragma + userId passphrases; SQLCipher-4 decryption; `scan_memory_keys` (memory-scan fallback from a running KakaoTalk.exe; the candidate walk steps 8 bytes — open-room DEKs sit 16-aligned but always-open databases' keys, the chat list among them, only 8-aligned) |

The toolkit is self-contained: the rest of the app — readers, capture, search — reaches it only through the
entry points above, so work outside these four modules never touches key material.

Setup turns that into capture inputs (see "First-run setup" below); after it, `collect` reads the `.edb` rooms
and the decrypted backups exactly as before — the ledger contract is unchanged; only the producers of the
keystore and `backup_decrypted` moved into this repo.

## First-run setup: config defaults and provisioning

`manage.py setup kakaotalk` is not just folders and a config file — through the framework's `setup_defaults` /
`setup_provision` hooks (`kakaotalk/provision.py`) it resolves what it can itself and provisions the one input
no command can read anywhere else, the root key:

- **Config defaults.** Each placeholder source the machine can name is filled before the config is written: the
  client's live user directory detected under `%LOCALAPPDATA%\Kakao\KakaoTalk\users` (the non-backup folder
  holding `appstate.dat`), and the app's own outputs (`backup_decrypted`, `keystore`) under the data root. A
  value already set — a user's own `--from` config — is never replaced; what stays a `<...>` placeholder is
  named after the write.
- **Root provisioning.** Every room DEK is wrapped under the 32-byte root, and on-disk artifacts do not hold it
  (`profile.dat`'s plaintext is a stray value; the live root comes out of the client's Themida-VM transform —
  the kakatok research). Setup installs it in three steps, each verified against the client's own store, unwrap
  success being the root's only honest oracle (`keychain.root_status`): an installed
  `<DATA_ROOT>/keys/root_v2.hex` stays while it unwraps the store; a copy on this machine
  (`Documents\kakatok\root_v2.hex`, …) is adopted only after it verifies; and the running client's memory is
  scanned for a 32-byte value that unwraps it — the same candidate walk the DEK scan uses, with the unwrap
  oracle in place of the page-1 header (`--restart-client` restarts KakaoTalk.exe and waits out its auto-login
  to rescan a freshly started client). Residency is version-dependent (the research observed 26.8.1 wiping the
  root at derivation; the client observed in validation held it and the scan recovered it, 623/623), so the
  scan is best-effort and its failure is an attention line, not an error. With a live root setup also writes
  the keystore at once (`provision.write_keystore_for`: every entry the root unwraps, each key checked against
  its room file's page-1 header oracle; rooms whose entry does not unwrap are left out and read as `no_key` —
  the honest signal of a rotated root — and `--scan` adds the room-DEK memory fallback for them). The
  key-source files' fingerprint is diffed against `<DATA_ROOT>/keys/rotation_state.json`, so a rotation is
  reported the moment it is observed. Keys are credentials: setup prints counts and paths only, never key
  material.
- **Backup.** The `*_backup_*` user directory beside the configured `kakao_user_dir` decrypts into the
  configured `backup_decrypted` source (`kakaotalk/backup.py`, EvaSQLite: the machine's pragma from the HKCU
  `Software\Kakao\KakaoTalk\DeviceInfo` registry values and the userId from the backup's
  `ActionLogDB.edb_*.backup`), with the `_manifest.json` the origin reads. A room whose source no longer
  decrypts loses its stale decrypted file, so capture never mistakes an old copy for current content. The
  registry read needs the KakaoTalk user's own HKCU — which is why provisioning lives in `setup`, run
  interactively, never in the SYSTEM task. No backup directory on the machine is a skip, not a problem.
- **Reruns.** `manage.py setup kakaotalk --provision` reruns exactly this provisioning against the config.json
  that is already there — the recovery path when the root has rotated (logout/re-login/device added): recover a
  new root with `--scan` (the running client's memory) or install one with `--root <hex file>` (verified
  against the store before it is installed).

## macOS support (the kakaocli port)

`kakaotalk/kakaocli.py` is the Python port of [Jaeha0526/kakaocli](https://github.com/Jaeha0526/kakaocli)
(branch `kakao-mcp`, MIT License; the key derivation goes back to
[this gist](https://gist.github.com/blluv/8418e3ef4f4aa86004657ea524f2de14)). On macOS the KakaoTalk client
keeps one SQLCipher database of `NT*` tables in the app container, and both its 78-hex-character filename and
its passphrase derive offline from two values the machine knows:

    filename = PBKDF2-HMAC-SHA256(".".join([".","F",userId,"A","F",reversed(uuid),".","|"]),
                                  salt=base64(SHA1(uuid) ‖ SHA256(uuid)) reversed)[hex 28:106]
    key      = PBKDF2-HMAC-SHA256(reversed("F".join(["A",hashed(uuid),"|","F",uuid[:5],"H",userId,"|",uuid[7:]])),
                                  salt=uuid[int(len(uuid)*0.3):])   →  PRAGMA KEY='<hex>'

the `IOPlatformUUID` (ioreg) and the userId from the KakaoTalk preference plists (`FSChatWindow*` key
suffixes, direct keys, the `DESIGNATEDFRIENDSREVISION` SHA-512 hash — verified against `AlertKakaoIDsList`
candidates, then a bounded brute force — or `NSWindow Frame FSChatWindowFrame_*`). The port also decrypts the
database with the standard library and pycryptodome alone (no `sqlcipher` build): compatibility 3 and 4
parameter sets are auto-detected through the page-1 header oracle, and the WAL is applied up to its last
valid commit frame, the same rules `readers/edb.py` uses.

Two pieces put a Mac into the ledger:

- setup's provisioning decrypts the client database (`provision.run_macdb`): on the Mac, as the KakaoTalk user,
  `setup kakaotalk` (or `setup kakaotalk --provision` to rerun it) discovers the database (the derived name
  first, then any 78-hex file in the container), reads it with its WAL in one consistent snapshot, decrypts it
  and writes the plaintext to the configured `mac_db` path. It runs whenever the config sets `mac_db`; the
  lookups can be skipped by passing `uuid`/`user_id`/`db`/`home` to `run_macdb` directly (a copied database,
  or another home), so a copied database also decrypts off macOS. The key is a credential: output is the
  derived filename and counts only. Attention is reported when the derived key opens no page (wrong
  uuid/userId) or the database holds no `NTChatMessage` rows.
- the `macdb` origin captures that decrypted database: `schema` records for the `NT*` tables, `msg` records
  from `NTChatMessage` (`logId`, `chatId`, `authorId`, `message`, `type` and the timestamp — the client spells
  it `sentAt`, the app's value rules `sendAt`; the reader maps that one name for the timestamp, nothing else),
  and `room` records titled the way the client names rooms (`chatName`, the
  `NTChatMeta` type-3 title, the open-link name, the direct-chat partner). Message keys
  are the ones every origin uses, so a message seen on both a PC and a Mac is one row across origins; search
  takes macdb text after `edb` and before `backup_decrypted`, its titles after `edb`, and its sender names
  (from `NTUser`) at the live client's rank. A database without `NTChatMessage` (a placeholder) is noted `not_mac`
  and captures nothing. The search index stamps moved to `kakao-4` when the retired kakatok export origins left
  and the client's own user database began naming senders: the first index step after upgrading rebuilds once.

A minimal macOS deployment: the same config with the PC-only sources pointing at placeholder files
(an empty file is a valid empty SQLite database for the origins you do not capture), `"mac_db"` set to the
decrypted database path, then `manage.py setup kakaotalk` and `manage.py collect kakaotalk --origins macdb`.

## Commands

All examples run from `<PROJECT>`. `--config <cfg>` is optional and overrides the default above.

```
python -s -B manage.py setup kakaotalk [--from <cfg>|-] [--force] [--provision] [--scan] [--root <hex file>] [--restart-client]
python -s -B manage.py collect kakaotalk [--config <cfg>] [--input-set live|T1|T2] [--origins a,b,...] [--rebuild-index]
python -s -B manage.py search kakaotalk [QUERY] [--config <cfg>] [options]
python -s -B manage.py check kakaotalk [--config <cfg>] [--export-state <DATA_ROOT>/raw/.verify/<file>] [--rebuild-state]
python -s -B manage.py test kakaotalk
python -s -B manage.py schedule install|status|run|remove kakaotalk [--config <cfg>] [--python <exe>] [--run-now|--dry-run]
```

Brackets show optional arguments, not literal shell text. Capture returns 0 ok, 3 suspect, 75 busy, below the
disk floor or skipped, 2 refused, and 1 error. A suspect origin commits nothing; other origins may commit.
`setup --provision` reruns the provisioning above against the config.json that is already there. `check`
verifies the folded state against the ledger, and its `--export-state` / `--rebuild-state` dump or rebuild that
state (the old export-state and replay). The root README's command table describes the whole shared surface.

## Administration (admin.py)

`admin.py` is the write side: scriptable message sending through the running Windows client's own Loco WRITE
path, driven with frida (the client keeps its session, TLS and sync; no credential is read — the app's key
material is never touched). It ports the kakaosend It ports the owner's kakaosend research, reverse-engineered against client
26.9.1.5339; the working notes live outside the repository.

```
python -s -B -m kakaotalk.admin check     --template <DATA_PARENT>\kakaotalk\admin\template.json
python -s -B -m kakaotalk.admin calibrate --template <...>                       # send ONE message in any plain-text room
python -s -B -m kakaotalk.admin rooms     --template <...> --chat-id 0x...
python -s -B -m kakaotalk.admin send      --template <...> --chat-id 0x... --message-file <file> [--dry-run]
python -s -B -m kakaotalk.admin listen    --template <...> [--prefix !!] [--lifetime 900]
```

- The template (captured client bytes identifying the account) and its `state.json` live under the data
  parent, like the token files; `--template` must be an absolute path there. `calibrate` writes both, seeding
  the message-id counter `0x200` past the captured id (an offset the server accepts; `check` shows headroom).
  It waits up to three minutes for the calibration send.
- `send` needs no message of yours: a minimal clone of the calibrated send whose every pointer the builder
  could dereference is replaced (supplement sentinel, no-extra `+0x180 = 10` path, its own text block).
  At most once: the counter is persisted before the client is called. Text comes from a file, at most
  200 UTF-8 bytes, and is never printed. Delivery is judged by the client's WRITE response handler
  (`err_code 0`) and ultimately the room — the client renders received messages up to a minute late; a
  "server unstable" dialog right after a send is a stale long-lived session, not a crash: restart and retry.
- `send` and `listen` cover the rooms whose own messages carry no `extra` field (the self-chat room and the
  plain rooms); rooms that need `extra` are not covered yet.
- `rooms` and `send` answer `no_room` when the running client holds no TalkChatRoom for the id — opening the
  chat materialises it. A send whose WRITE response never arrives still reports `sent` with `err_code` null;
  judge those by the room.
- `listen` reacts only to the owner's own sends starting with the prefix (the calibrated sender id), echoes
  them prefix-stripped 0.3s later into the same room, and tears down cleanly on Ctrl+C or `--lifetime`.
  While resident it prints one JSON line per event (`event`), command text included.
- Addresses are locked to client 26.9.1.5339: a version or builder-prologue mismatch refuses before anything
  is touched. Exit codes: 0 ok, 1 a write happened but was rejected or failed after it, 2 refused before any
  write. Requires the `frida` Python package (imported lazily; the collector never needs it).

## Search

### index (collect's index step)

`collect`'s index step brings `<DATA_ROOT>\index\kakaotalk.sqlite` up to the committed ledger (the search reads only
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

### search kakaotalk

```
python -s -B manage.py search kakaotalk "<query>" --json
python -s -B manage.py search kakaotalk "<query>" --room "<title fragment>" --since <date> --context 3
python -s -B manage.py search kakaotalk "<query>" --mode timeline
python -s -B manage.py search kakaotalk --room "<title fragment>" --before <cursor>
python -s -B manage.py search kakaotalk --list-rooms
```

- **One row per message key.** Each key `kakao:<chatId>:msg:<logId>` has one row across the three Kakao
  origins.
- **Text.** It comes from the best origin whose latest capture of the message has text, in the order edb >
  macdb > backup_decrypted. An origin that has since tombstoned or
  emptied the message does not outrank another origin's current text with its old one. A message deleted for
  everyone, or deleted locally, keeps the last text that was captured.
- **Flags.** `deleted` rows are hidden unless `--include-deleted`. `gone` means no origin holds the message
  any more. `edited` means an origin's text changed between captures.
- **Labels.** Sender names come from the client's own user database — `TalkUserDB` read beside the rooms
  (`edb`) and from the backup's decrypted copy (`backup_decrypted`; the accounts' display names, the live
  client's winning a tie) — and the macOS database's NTUser; room titles from the newest capture (the
  client's chat-list database for `edb` rooms, the manifest titles for the backup's).
  Renames relabel old rows.
- **Filters.** `--room` (title substring or chat id, repeatable), `--sender` (name substring or author id),
  `--since` / `--until`.
- **Browsing.** Filters without a query browse: the command shows the newest page (in time order), and you
  walk through the pages with `--before` / `--after` cursors `<ts>.<logId>.<row>`. The row id breaks ties
  between messages that share a time and logId (the same logId in two rooms), so no page boundary skips one.
  A two-part cursor `<ts>.<logId>` from an older page still works.
- `--mode hits|count|timeline|senders|rooms`; `--context N` shows N messages before and after each hit in the
  same room, by time keyset; `--limit`, `--width`, and `--tz` (default `+09:00`; dates, month buckets and display
  use this fixed offset). `--until` is inclusive of the whole period given (`--until 2024-10` ends with the last
  second of October).
- `--json` prints an envelope: `source, mode, query, fts, verify, weak, exclude, exclude_fts, filters, tz,
  total, rows, built_at, updated_at, index{seq, ledger_seq, stale}`, plus `page` when browsing. Large ids are
  strings. `--explain` prints the FTS5 expression; the separator token is shown as `<S>`.
- A stderr note appears when the index is behind the ledger, and when a query falls back to a full scan.
- Exit codes: 0 ok (0 hits included) · 2 usage, bad query, missing or outdated index · 75 index lock busy
  · 1 unexpected.

There is no masking, pseudonymisation or redaction. Names, nicknames, room titles and texts are shown as they
are. Whoever uses the output applies their own privacy rules.

### Query language and tokenizer

| Query | Meaning |
|---|---|
| `송금 계좌` | both terms (AND) |
| `"정기 회의"` | the exact phrase |
| `회의 OR 미팅` | either term |
| `제출 -취소` | the first term, excluding the second |
| `api*` | prefix |

Text is normalised as NFC + casefold. Each message gets an n-gram shadow column: Korean/CJK runs become
overlapping bigrams and Latin/digit runs become one token. A query term is issued as an FTS5 phrase over the
same tokens. As a result:

- Korean matches as a substring: `송금` finds `송금을` and `입금송금`.
- A Latin/digit run at a term's edge must match a whole run: `api` finds `API 키` but not `rapid`, and
  `api*` finds `apis`.
- Quoted phrases and terms with punctuation (`C++`, `AES-256`) match literally after normalisation.

Tokenizer `ngram-2` fixes two bugs of the knowledge-base engine (`ngram-1`) it was ported from:

1. **Missed matches.** `10월` did not find `10월에`, nor `3시` find `3시에` or `SQL문` find `SQL문을`. The
   query's lone `월` token never appeared in a document that held only `월에`. A Korean run that touches a
   Latin/digit run now also emits a one-character head or tail token, so the phrase lines up.
2. **False positives.** `가나다` matched `가나 나다`: the bigrams of two separate words lined up as one
   phrase. A separator token now stands between runs that whitespace or punctuation kept apart.

In addition, excluded terms use the same match rule as positive ones (`-api` no longer drops messages
containing `rapid`), and `OR` next to an excluded term is refused instead of being silently ignored. A
property test in `tests/core/test_textindex.py` checks the index against an independent model of these rules on
thousands of random queries.

An unquoted term with a Latin/digit run longer than 32,000 characters is refused: SQLite FTS5 cuts tokens at
32,768 bytes, so such a run would also match longer runs that start with it. Snippets mark matches with
the same case folding the search uses, so `straße` marks `STRASSE`.

## Scheduled task

`AppConfig.schedule` in `kakaotalk/apps.py` defines `\llm-wiki\kakaotalk-capture`, run as SYSTEM
(times and limit: [../README.md](../README.md#scheduled-tasks)). The generated Arguments are
`-s -B <PROJECT>\manage.py collect kakaotalk --config <cfg>`, with `<PROJECT>` as WorkingDirectory.

```
python -s -B manage.py schedule install kakaotalk [--config <cfg>] [--python <interpreter>] [--run-now|--dry-run]
python -s -B manage.py schedule status kakaotalk [--config <cfg>] [--python <interpreter>]
python -s -B manage.py schedule run kakaotalk
python -s -B manage.py schedule remove kakaotalk
```

Install, run and remove need elevation. Install validates the config, data root, interpreter and task checks
before registration, replaces a task of the same name and compares the definition read back. Dry-run prints
the definition without registering it. Status shows definition differences, last result and the newest run's
finished step lines; it returns 3 when attention is needed. A new start is ignored while one runs, and a missed
start runs when the machine returns.

`collect kakaotalk` runs the capture step (the live input set) and then the index step after every capture exit
code. Only committed manifests enter the index, so the index step is a no-op when nothing new was committed.
Steps are logged in `<DATA_ROOT>/reports/task.log`, and collect returns capture's exit code.

## Slash command and tests

`.claude/commands/raw-search.md` provides `/raw-search` from the root project — one command for both apps; it
picks the app from the request. It uses one to three JSON searches, widens empty results and cites dates,
rooms and senders. Config is optional, with the same default as the CLI; an override uses `--config <cfg>`.

```
python -s -B manage.py test kakaotalk
python -s -B manage.py test
```

App tests synthesize inputs in code under temporary directories. Search tests capture made-up inputs through
`kakaotalk/tests/ledgerfix.py` and compare incremental indexing with rebuilds, including resets, mid-commit,
defects, crashes, version checks, locks, search modes and filters. Core tests use toy apps without
importing the real apps. See the root README for the full command table and core maintenance.
