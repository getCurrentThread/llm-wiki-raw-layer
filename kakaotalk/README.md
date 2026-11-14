# kakaotalk: the KakaoTalk raw layer

This app captures private KakaoTalk conversations into an append-only, crash-safe raw ledger and builds an
SQLite FTS5 search index at `<DATA_ROOT>/index/kakaotalk.sqlite`. It shares the root project's **rawlayer**
framework: the root README holds what every app shares (commands, machine config and data root, the search
index and query language, scheduled tasks, tests) and `../CORE.md` the contract. Message data and machine config
remain outside the repository.
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

`apps.py` (source keys, options, schedule and the setup hooks), `origins.py`, `inputs.py`, `readers/`, `keys.py`
(value rules), `vocab.py`, `search.py`, `state.py` (the `schema_base` and `edb_salt` state tables), `provision.py`
(setup's defaults and the provisioning of the root key, keystore, backup and macOS database), `backup.py` (the
EvaSQLite backup decryption), `admin.py` (the write side — scriptable sending) and `config.example.json`.
`wincrypt.py`, `keystore.py` and `keychain.py` are the in-repo key derivation and decryption toolkit ported from
kakatok (the owner's prior research project; its notes live outside this repository), and `maccrypt.py` is the
macOS port of Jaeha0526/kakaocli (kakao-mcp, MIT — see the macOS section below). App tests (`tests/`) synthesize
inputs in code under temporary directories; the search tests capture made-up inputs through `tests/ledgerfix.py`
and compare incremental indexing with rebuilds over the search modes and filters.

## Config, origins and records

`kakaotalk/config.example.json` shows the machine config shape; the config is
`<DATA_PARENT>/raw/kakaotalk/config.json` and `<DATA_ROOT>` is its folder (the root README has the common keys and
the data root's layout).

Capture reads the decrypted backup room databases and the client's encrypted `.edb` room files read-only.
Origins, in ledger order, are `backup_decrypted`, `edb`, and `macdb` (the macOS client's decrypted database;
see the macOS section). The message key is `kakao:<chatId>:msg:<logId>` across origins. Records have kinds `schema`,
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
set, its parent directory must exist (setup's provisioning writes the file there). The path must lie inside
the app's data root: `provision.run_macdb` refuses (exit 2) to write the plaintext anywhere else, and a failed
write leaves no `.tmp` behind.
For frozen `T1`/`T2` inputs, encrypted rooms come from `<inputs_root>/<set>/kakao_user/chat_data`. Static
inputs remain at their configured paths and are checked against the freeze's hashes.

## In-repo key derivation and decryption (the kakatok port)

The app produces the keystore and the decrypted backups itself, with a toolkit ported from kakatok:

| module | what it does |
|---|---|
| `kakaotalk/keystore.py` | parses the client's CBOR keystore `appstate.dat` / `keystore.bin` (`info_prefix`, `salt`, `wrapped_dek_map`); the authoritative room inventory |
| `kakaotalk/keychain.py` | the key hierarchy: `KEK = HKDF-SHA256(root, salt, info_prefix + relpath verbatim)`, `DEK = AES-KW-unwrap(KEK, wrapped_dek)` (RFC 5869 / 3394); the page-1 header oracle `verify_dek`; the key-source rotation fingerprint |
| `kakaotalk/wincrypt.py` | EvaSQLite (pre-25.7.2 backups): registry `DeviceInfo` pragma + userId passphrases; SQLCipher-4 decryption; `scan_memory_keys` (memory-scan fallback from a running KakaoTalk.exe; the candidate walk steps 8 bytes — open-room DEKs sit 16-aligned but always-open databases' keys, the chat list among them, only 8-aligned) |

The toolkit is self-contained: the rest of the app — readers, capture, search — reaches it only through the
entry points above, so work outside these four modules never touches key material.

Setup turns that into capture inputs (see "First-run setup" below).

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

`kakaotalk/maccrypt.py` is the Python port of [Jaeha0526/kakaocli](https://github.com/Jaeha0526/kakaocli)
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
  and captures nothing.

A minimal macOS deployment: the same config with the PC-only sources pointing at placeholder files
(an empty file is a valid empty SQLite database for the origins you do not capture), `"mac_db"` set to the
decrypted database path, then `manage.py setup kakaotalk` and `manage.py capture kakaotalk --origins macdb`.

## Administration (admin.py)

`admin.py` is the write side: scriptable message sending through the running Windows client's own Loco WRITE
path, driven with frida (the client keeps its session, TLS and sync; no credential is read — the app's key
material is never touched). It ports the owner's kakaosend research, reverse-engineered against client
26.9.1.5339; the working notes live outside the repository.

```
python -s -B -m kakaotalk.admin check     --template <DATA_PARENT>\kakaotalk\admin\template.json
python -s -B -m kakaotalk.admin calibrate --template <...>                       # send ONE message in any plain-text room
python -s -B -m kakaotalk.admin rooms     --template <...> (--chat-id 0x... | --room "<title fragment>")
python -s -B -m kakaotalk.admin send      --template <...> (--chat-id 0x... | --room "<...>") --message-file <file> [--dry-run]
python -s -B -m kakaotalk.admin listen    --template <...> [--prefix !!] [--lifetime 900]
```

- The template (captured client bytes identifying the account) and its `state.json` live under the data
  parent, like the token files; `--template` must be an absolute path there. `check` runs before
  `calibrate` too: with no template yet it reports `"template": "missing"` (exit 0) beside the client
  guards. `calibrate` writes both, seeding the message-id counter `0x200` past the captured id (an offset
  the server accepts; `check` reports the counter and its headroom against the newest known live id). It
  waits up to three minutes for the calibration send, which must be a
  plain-text send — a capture of any other kind (`+0x180 != 10`) refuses `not_plain_room` (exit 2, nothing
  written). `send` and `listen` take every message id from that one counter, read and persisted under a lock
  on the state folder that spans the client call (a concurrent taker refuses `state_busy`, exit 2), and
  `send` refuses `bad_state` (exit 2) when the state file is missing or invalid instead of falling back to
  the calibrated id. The counter also stays ahead of the client's own: the listener's builder hook sees the
  owner's live msgIds and folds their maximum into the state (`live_msg_id_max`, under the same lock), and a
  taken id is `max(counter, live max + 1)` — the client's counter cannot catch up and collide.
- `send` needs no message of yours: a minimal clone of the calibrated send whose every pointer the builder
  could dereference is replaced (supplement sentinel, no-extra `+0x180 = 10` path, its own text block).
  At most once: the counter is persisted before the client is called. Text comes from a file, at most
  200 UTF-8 bytes, and is never printed. Chat ids are exact 64-bit values end to end (decimal strings across
  the frida bridge — a JS double would round ids above 2^53), and only the target room's WRITE response
  counts: another room's response inside the window reads as no response. Delivery is judged by the client's
  WRITE response handler (`err_code 0`) and ultimately the room — the client renders received messages up to
  a minute late; a "server unstable" dialog after a send is a stale long-lived session, not a crash: restart
  and retry.
- `send` and `listen` cover the rooms whose own messages carry no `extra` field (the self-chat room and the
  plain rooms); rooms that need `extra` are not covered yet, and neither are replies: the one approved
  reply observation (2026-11-02) showed the PC client's reply carries its source reference in sub-objects
  of the message object (and an *empty* string at `+0x17c`), not in a patchable extra string — beyond the
  clone-and-patch approach until that structure is reverse-engineered.
- `rooms` and `send` name the chat by `--chat-id` or `--room <title fragment>` — exactly one of the two
  (`chat_id_exclusive` / `chat_id_required`, exit 2). The fragment is resolved read-only from the search
  index (`<data_root>/index/kakaotalk.sqlite`, located and opened exactly as `search` does; only `data_root`
  is read from the config) before anything touches the client, with the same title semantics `search
  --room` uses: a case-insensitive substring, wildcards escaped, untitled rooms never matching (a purely
  numeric fragment matches titles, not ids — use `--chat-id` for ids). Exactly one match resolves; zero
  refuse `room_not_found`, several `ambiguous_room_title` (the count only — no titles or ids printed), a
  missing or unreadable index `index_missing` / `index_unreadable`, a locked one `index_busy`: all exit 2
  before the client is touched. A stale index resolves stale titles — capture first if a room is missing.
- `rooms` and `send` answer `no_room` when the running client holds no TalkChatRoom for the id — opening the
  chat materialises it. A send whose WRITE response never arrives — or only other rooms' responses —
  reports `unconfirmed` (exit 1) with `err_code` null and an `outcome_basis` naming the case; the write
  happened, the result is unknown, judge those by the room. When the client's WRITE response provides
  them, the send result also carries `log_id` — an exact 64-bit decimal string (never through a JS number),
  the msg id of the ledger key `kakao:<chatId>:msg:<logId>` — and `sent_at` (a unix timestamp, decimal
  string); fields the response does not carry are absent keys, never guesses. Neither is extracted yet: a
  PC-side re-observation (2026-11-02) saw the WRITE response handler fire — the earlier empty captures had
  watched phone-app sends the PC hook can never see — but neither logId nor sendAt appeared in the
  handler's first-level arguments or registers (they sit in second-level objects), so those keys stay
  absent until a deeper read-only capture pins them. With the handler confirmed firing, a response (and
  `err_code`) can be expected on ordinary sends.
- `listen` reacts only to the owner's own plain-text sends starting with the prefix (the calibrated sender
  id; a non-plain capture is skipped with a `[SKIP] not plain` event), echoes them prefix-stripped 0.3s later
  into the same room — built by the same shared clone builder and readback gates `send` uses — and each
  reply's message id is taken from the same state counter `send` uses, persisted under the state lock before
  the builder call (a lock a concurrent send holds fails that one command, never sends it, and the listener
  stays up). It tears down cleanly on Ctrl+C (`stopped_by` `interrupt`), `--lifetime` (`lifetime`) or a
  detached frida session (`detached`). While resident it prints one JSON line per event (`event`), command
  text included.
- Every address, byte pattern and struct offset the tooling uses lives in one per-version table keyed by the
  client's FileVersion (today: 26.9.1.5339 — `check` names the supported versions) and is spliced into the
  injected JS, which carries no such literal of its own: an unknown version or a builder-prologue mismatch
  refuses before anything is touched (the WRITE response handler's own prologue bytes are not recorded yet,
  so theirs is the one check still unfilled — the slot is in the table), and two running KakaoTalk clients
  refuse `ambiguous_client`. Exit codes: 0 ok, 1 a write happened but was rejected, failed or stayed
  unknown after it, 2 refused before any write. `send` statuses: `sent` (0: the target room's own WRITE
  response carries `err_code` 0 — the only proof of delivery); `server_rejected` (1: that response carries
  a non-zero errCode); `unconfirmed` (1: the builder was called and returned OK, but no response
  attributable to the target chat arrived in the window — none at all (`outcome_basis` `no_response`),
  only other rooms' (`other_room_response`), or an unreadable one (`unreadable_response`). The write
  happened; the result is unknown: check the room before resending. With the response handler now observed
  firing on PC sends, `unconfirmed` should be rare — and when it appears, the honest answer is "check the
  room", not a failure);
  `failed_after_write` (1: the injected script reached the client's send call — its `[CALLED]` marker — and
  no success followed, a builder failure, an exception or a timeout; also any frida error while the script
  loads, since loading runs it up to that call; the message may have arrived, so check the room before
  resending); `refused` (2: every failure before that call, nothing was sent). A frida session detaching
  mid-wait ends the wait early (send keeps that same split; calibrate reports `no_capture`; listen stops
  with `stopped_by` `detached`). Requires the `frida` Python package (imported lazily; the fetch step never needs it).

## Search

`search kakaotalk --help` lists the modes, filters and cursors; the index, the query language and the output are
the root README's ("Search").

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
- **Cursors.** `--before` / `--after` take `<ts>.<logId>.<row>`. The row id breaks ties between messages that
  share a time and logId (the same logId in two rooms), so no page boundary skips one. A two-part cursor
  `<ts>.<logId>` from an older page still works.

## Wiki export

`kakaotalk/apps.py` declares `wiki = (None, "chat")`: `manage.py collect kakaotalk` (llm.md 13) writes each room's
closed months as Markdown sources of the wiki vault, but there are no `*_wiki_*` config keys, so `--project`,
`--include`, `--exclude` and `--since` on the command line give its settings. Only when the owner asks for it.
