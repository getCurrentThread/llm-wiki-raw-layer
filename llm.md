# llm.md — live operations guide for LLM agents

This file tells an LLM agent how to bring this repository's apps into live operation on a Windows machine, keep
them healthy and recover them, one gated step at a time. It covers operations only. Contracts and design live in
[CORE.md](CORE.md), [README.md](README.md) and each `<app>/README.md`; this file links to them instead of
repeating them.

## 1. How to use this file

- Read the whole file before running anything. Then run Phase 0 (section 4) and the card of the app you were
  asked to operate (section 6), from its first step. Each card runs the generic steps of section 5 at the points
  it names, with its own keys, bounds and extra steps in between. Never run section 5 as a sequence on its own.
  Outside a card, run only the single section 5 steps that sections 4, 7, 8 or 9 name (for example 5.7–5.10 to
  bring back the task of an app that is already live).
- Start every session, new or resumed, with `python -s -B '<PROJECT>\tools\bootstrap.py' doctor` (section 4). It
  changes nothing, and it names each in-scope app's next step (`next`): resume the card there, after reading the
  steps before it. Record the gates you pass with `bootstrap.py checkpoint mark` (5.0), so that the next session
  can resume as well.
- Every executable step ends with a **gate**: a command and the result it must show. When a gate fails, stop. Do
  not improvise a workaround; report the step, the command, its exit code and the aggregate output (section 10).
- **[human]** marks a step only the owner can do: use the handshake in 7.1 and wait.
- **[elevated]** marks a step that needs an Administrator process (7.2). Every elevated call shows the owner a UAC
  prompt, so it is also [human]; batch elevated commands (5.8) to keep the clicks few.
- Use PowerShell 7.3 or newer (`pwsh`) as the shell that runs your commands. Windows PowerShell 5.1 and pwsh
  7.0–7.2 strip the double quotes inside native-command arguments, so a JSON list given to `config set` (5.3)
  arrives broken.
- Agent shells usually keep no variables between tool calls. Send every `$name = ...` definition (the 5.3 helper,
  the 7.2 values) in the same command that uses it.
- Run every command from `<PROJECT>`. Pass `--config <CFG>` to every command that accepts it: `check <app>`,
  `collect`, `search`, the collectors, `schedule install` and `schedule status`. `setup`, `test`, `schedule run`
  and `schedule remove` take none; `setup` writes `<CFG>` itself.
- Placeholders appear bare in some commands and already inside quotes in others (for example 5.3, 6.1.3, 6.1.4,
  6.2, 7.2, 7.3). Substitute every one before running a command:
  - Wrap a bare path in single quotes.
  - Where a placeholder is already quoted, replace only the placeholder and keep its quotes: 7.3's `'"<CFG>"'`
    becomes `'"C:\...\config.json"'`.
  - Double every `'` inside a path: `'C:\Users\O''Brien\...'`.
  - A leftover `<` outside quotes is a redirection error. Inside quotes it silently becomes a literal path, so
    check before running.
  - Exception: `'<HOME>'` and `'<user>'` as the replacement text of `-replace` (the section 3 reader, 7.2) are
    output, not placeholders. Keep them exactly as written: substituted, the mask would print the real profile
    path.
- In reports and messages, write placeholders instead of real paths: a profile path contains the Windows user
  name.

| placeholder | meaning |
|---|---|
| `<PROJECT>` | this checkout, the folder holding `manage.py` |
| `<HOME>` | the profile folder the checkout sits in, e.g. `C:\Users\<user>` |
| `<DATA_PARENT>` | `<HOME>\llm-wiki-data` (`config/settings.py`) |
| `<app>` | `gmail`, `discord`, `notion` or `mattermost` |
| `<DATA_ROOT>` | `<DATA_PARENT>\<app>` |
| `<CFG>` | `<DATA_ROOT>\config.json` |
| `<PY>` | absolute path of the Python interpreter the scheduled task will start (0.2) |
| `<source_key>` | the app's `sources` entry: `gm_archive`, `dc_archive`, `nt_archive` or `mm_archive` |
| `<sync_key>` | the app's sync option: `gm_sync`, `dc_sync`, `nt_sync` or `mm_sync` (5.6) |
| `<MM_ARCHIVE>` | the absolute path of an existing mattertok archive folder to continue from, if there is one (6.4) |
| `<mattertok>` | the mattertok checkout that `<MM_ARCHIVE>` came from, the folder holding its `.env` (6.4) |
| `<REPO_URL>` | the clone URL of this repository, which the owner gives (section 4) |
| `<keys>` | the key names a `setup` line lists after `fill in config.json:` (5.1) |
| `<N>` | a collector bound, given by the app card |
| `<HEAD_SEQ>`, `<TOTAL>` | the `head_seq` that `check <app>` printed and the `total` of the 5.5 timeline command, in the step that records them |
| `<T0>` | the UTC start time printed in 5.8 step 1 |
| `<W0>` | the write time printed by the discord token file handshake (6.2) |
| `<SITE>`, `<site-repo>`, `<login>` | the site's new folder under `$env:TEMP`, its new repository name, and the owner's GitHub login, held in `$login` or passed through the clipboard and never printed or typed (6.1.4) |
| `<task>` | a short name for the report file |

## 2. Ground rules

1. Always invoke Python as `python -s -B` (or `& '<PY>' -s -B`).
2. **kakaotalk is off-limits.** Never open, list, grep or read anything under `kakaotalk/` or its data root. Run
   kakaotalk commands only when the owner explicitly asks, and read only their pass/fail summary lines.
   Framework-wide `manage.py check`, with no app named, is allowed: it validates every app's contract and prints
   summary lines only. A bare `manage.py test` also runs kakaotalk's suite (0.4).
3. **Report aggregates only.** Never write any of these into a message, a report (section 10) or a file in
   `<PROJECT>`, and never show them in quoted command output. Configs, credentials and archives under
   `<DATA_PARENT>`, the browser download that 6.1.3 step 7 moves, and the temporary files that 7.2 and 7.3 delete
   are the only files that may hold them:
   - message text, subjects, names, addresses or phone numbers;
   - message, thread, label, channel or page ids, and exclusion patterns;
   - account emails, tokens, client JSON or consent URLs;
   - Google Cloud project ids, the owner's Google or GitHub account names, or the Windows user name in paths.

   Credential download file names embed a client id, so only count them. The collectors' JSON lines, capture's
   lines, the index count lines and the `timeline`/`count` search modes are aggregate-only by design; other
   search modes print private plaintext. An index failure line or a traceback in task.log can hold profile paths,
   so read task.log only through the section 3 reader, or the 5.9 poll, which prints step lines only. The 7.2
   script masks its output the same way.
4. Data, archives, configs and tokens live only under `<DATA_PARENT>`. The exceptions are sources read in place
   and never copied: the discord and notion desktop sources (`dc_desktop_path`, `nt_desktop_path`). Never copy any of them into the repository and never commit them
   (`config.json`, `raw/`, `index/`, `*.sqlite`, `*.jsonl` and `reports/` are gitignored).
5. Never ask a human to paste a secret into chat. Credentials move as files (download, then copy into
   `<DATA_ROOT>`), never as text.
6. Always name the app in `schedule` commands. Without one, `install` and `status` act on every app with a
   schedule, kakaotalk included (`run` and `remove` refuse).
7. Do not change code while operating. A bug found during operations is reported (symptom, command, exit code),
   not fixed.
8. Apps in scope: discord, gmail, mattermost, notion, and kakaotalk under rule 2. Do not create other apps; slack
   and telegram apps must not be implemented.
9. Never commit or push this repository while operating. The only push allowed is the owner-approved public site
   of 6.1.4, from its own folder.
10. Irreversible or replacing actions need the owner's explicit approval: `setup --force`,
    `check --rebuild-state`, `--full`, `schedule remove`, a new Google Cloud project or client, `--auth` over a
    working token. Never delete, move, rename or re-create by hand `<DATA_PARENT>` or anything in it. The commands
    this file runs rewrite their own files there, at the steps that name them and with the approval above where it
    is listed; `collect --rebuild-index` needs none, because it rebuilds only the derived index from the ledger.

## 3. Exit codes and outputs

| command | exit codes |
|---|---|
| `collect` / capture | 0 ok · 3 suspect (that origin, one `sources` entry such as `gm_archive`, committed nothing) · 75 busy or below the disk floor (nothing committed), or an origin or container skipped (what was read is committed) · 2 refused · 1 error |
| `-m <app>.collector` | 0 ok · 75 `errors` above 0, busy, a network or API failure, or another non-auth category · 2 `auth_required`, `auth_failed`, `account_changed` (the last only from a sync run, never from `--auth-check`) · 1 config or unexpected error · with `--capture`, a non-zero code of its `collect` step (the row above) is returned as is |
| `-m gmail.collector --auth` | 0 `auth_ready` · 2 every failure; `status` names the cause (7.3) |
| `-m mattermost.collector --login` | 0 `auth_ready` · 2 every failure; `status` names the cause (6.4) |
| `search` | 0 (also for zero hits) · 2 usage, query or missing/outdated index · 75 retry later · 1 |
| `check` | 0 · 1 errors (including `differing_rows>0`) · 2 config refused. It does not test the ACL or Windows Search. |
| `setup` | 0 ready · 3 placeholders remain, the config refused, or an ACL/Windows Search problem (read every line) · 2 refused (usage, or the config is already there) · 1 error |
| `schedule` | 0 · 2 refused or not elevated · 1 `schtasks` failed · 3 needs attention (from a non-elevated prompt, possibly only "not readable") |
| `tools/bootstrap.py` | 0 ok · 3 needs attention (`doctor`: an item under `need attention`; `config`: placeholders left or the config refused; `agents status`: an item not current; `agents sync-repo --check`: the checkout's copies differ; `agents uninstall`: an item kept) · 2 refused, nothing written (usage, kakaotalk, a key or value the app refuses, an edit that would make an accepted config refused) · 1 error |

- **`check <app>` lines:**
  - `config: ok`
  - `replay check=yes differing_rows=N head_seq=N`
  - `core: ok files=69`
  - `check: ok errors=0 refused=0`

  While a capture runs (often the scheduled task), the replay line is replaced by
  `note: ledger busy ...; the folded state was not verified` with exit 0. That is not a pass: check again after
  the run.
- **Collector output:** one JSON line with sorted keys, e.g.
  `{"app": "gmail", "errors": 0, "limited": false, "messages_written": 200, ...}`. Gates quote its fields in that
  form. A failure line carries the category in `status` (e.g. `"status": "auth_required"`) and has no `errors`.
- **Scheduled task result:** the task returns collect's code, which is capture's. With a sync option on, the
  collector runs inside capture: its failures (an expired token included) show as capture 75, and its counts
  (`errors`, `limited`, `messages_written`) are written nowhere.
- **`schedule status` output:** `last result:` prints the code, followed by its text when the code is known.
  - Ok: `0 (ok)`, `0x00041301 (running now)`, `0x00041303 (has not run yet)`.
  - Not ok: `1 (capture error)`, `2 (capture refused)`, `3 (capture suspect)`,
    `75 (capture busy, or an origin skipped)`, `124 (capture timed out)`,
    `0x00041306 (stopped (time limit, or by hand))`,
    `0x80070002 (the interpreter or a file the action names was not found)`.
  - Any other code is printed without a text and is not ok.
- **task.log:** `<DATA_ROOT>\reports\task.log`.
  - Every run appends a block: the scheduled task, `collect`, and a collector with `--capture`. A block is a bare
    line `run`, then `<UTC> step=capture rc=N seconds=N` with capture's indented lines, then
    `<UTC> step=index rc=N seconds=N` with the index's lines.
  - Index runs after every capture rc. A step line is written when its step ends, so while capture works the
    newest block is the single line `run`. A run (scheduled, or a manual `collect` or `--capture`) is in progress
    while the newest block has no `step=index` line yet; elevated status shows a scheduled one as `running now`.
    A block that never gets one was stopped (section 9).
  - The file rolls over to `task.log.1` above 4 MiB.
  - A run refused before its data root was known logs to `<DATA_PARENT>\task-refused.log` instead. A task whose
    interpreter or checkout could not start writes nothing.

  Read task.log only with this reader, which masks profile paths. `$k` is how many of the newest blocks it shows
  (an unfinished block counts as one): 1 unless a step asks for more.

  ```powershell
  $k = 1
  $log = @(Get-Content -LiteralPath '<DATA_ROOT>\reports\task.log' -Tail 400 -ErrorAction SilentlyContinue)
  $starts = @(for ($j = 0; $j -lt $log.Count; $j++) { if ($log[$j] -ceq 'run') { $j } })
  $user = [regex]::Escape((Split-Path $HOME -Leaf))
  if (-not $starts.Count) { 'no run line in the last 400 lines' } else { $log[$starts[[Math]::Max(0, $starts.Count - $k)]..($log.Count - 1)] -replace [regex]::Escape($HOME.Replace('\', '\\')), '<HOME>' -replace [regex]::Escape($HOME), '<HOME>' -replace "(?<=[\\/])$user(?![\w.-])", '<user>' }
  ```

  The reason lines under `step=capture`:
  - `capture done ... seq=N`: committed.
  - `capture busy`: another process held the ledger lock; nothing committed.
  - `capture skipped disk_floor=yes`: below the disk floor; nothing committed.
  - `refused: precondition or configuration`, with rc 2. It never names the reason (section 9, capture 2 row).
  - `<source_key> status=error`: that origin failed, for example the sync inside it. gmail and notion also print
    a line starting `origin error <source_key>` before it; discord and mattermost do not.
  - `<source_key> status=suspect`, with `capture done ... suspect=N` and rc 3: a key captured before is missing
    from a container that was read in full; that origin committed nothing.
  - `<source_key> status=unstable`: the source kept changing through all three read passes, so nothing of that
    origin was read this run (another program was writing the archive). Capture still commits and exits 75.

  Under `step=index`, rc 75 with `<app>: busy reason=another index update holds the lock` means another run's
  index step held the index lock.

## 4. Phase 0 — machine preflight (once per machine)

**Getting the code** (a machine without a checkout; Git for Windows first, 0.1). Clone with git, not from a zip,
into a folder under the owner's profile that no sync client (OneDrive and the like) manages:
`git clone '<REPO_URL>' "$HOME\llm-wiki-raw-layer"`. That folder is `<PROJECT>`. Capture records the code version
from git, and the SYSTEM task finds `<DATA_PARENT>` through the profile that holds the checkout (0.1). An existing
checkout is never moved, re-cloned or updated (`git pull`) unless the owner asks: every task names its path, and
rule 7 applies to code updates too.
Gate: `git -C '<PROJECT>' rev-parse --is-inside-work-tree` prints `true`.

**Doctor.** `python -s -B '<PROJECT>\tools\bootstrap.py' doctor` runs from any folder and needs no elevation.
- It reads and stats only: no lock, no file content, and masked paths.
- Its `machine` block answers the gates of 0.1–0.3 and 0.5, except the shell. It checks that a `pwsh` 7.3 or newer
  is installed, not which shell runs your commands. So always run 0.1's `$PSVersionTable.PSVersion` and
  `$PSNativeCommandArgumentPassing` gates yourself, in your own shell. The rows below stay the definition of each
  gate; run a row's own command when doctor names a problem in it, or when the owner asks.
- Then one block per in-scope app (kakaotalk is skipped, rule 2): config, credential and source paths (whether
  they are there), index, task, the newest task.log run and the checkpoints. `next` is the llm.md step to do next;
  `8` means live.
- Without elevation, doctor cannot read a SYSTEM task, so its task is `unknown`. Then `next` is `8` only once a
  `5.10` checkpoint is recorded (5.10, section 8). Without one, it is `5.7`: check the task elevated first.
- It exits 0 when nothing needs attention, and 3 otherwise. An app with no config counts only when named with
  `--app <app>`.
- `--json` prints the same as one object. `--verify` also runs `check <app>`, which takes the ledger lock: never
  while a capture may run.
Gate for Phase 0: its `machine` lines show no problem, and `need attention` lists no `machine` item.

Do 0.6 first: the installs in 0.1 and 0.2 and the exclusion in 0.5 can need the owner.

| step | action | gate |
|---|---|---|
| 0.1 | Prerequisites. This checkout sits under the owner's profile (`<HOME>\...`): outside a profile the owner's commands still work, but every SYSTEM run fails at start (`1 (capture error)`, no task.log block), so never move the checkout yourself. Git for Windows is installed machine-wide on PATH: the SYSTEM task's precheck runs `git` and refuses the data root when it cannot. PowerShell 7.3 or newer. `gh` (GitHub CLI) only for the site in 6.1.4; its block checks for it. The four apps use the standard library only; no pip installs. If `pwsh` or `git` is missing: **[human]** the owner installs it (the Microsoft Store package of PowerShell needs no UAC prompt; an MSI or a machine-wide Git install shows one), then restart the agent session in pwsh. | `git --version` succeeds; `([Environment]::GetEnvironmentVariable('Path', 'Machine') -split ';').Where({ $_ -and (Test-Path -LiteralPath (Join-Path $_ 'git.exe')) }).Count` is 1 or more (the SYSTEM task sees only the machine PATH); `$PSVersionTable.PSVersion` is 7.3 or newer; `$PSNativeCommandArgumentPassing` is not `Legacy` |
| 0.2 | Choose `<PY>`. In a shell opened after any Python install: `python -s -B -c "import sys; print(sys.executable)"` (if `python` is missing or opens the Store, use `py -c ...`). `(Get-Command python).Source` is not the test: the python.org install manager's `python` is a `WindowsApps` alias that starts a real interpreter. If nothing qualifies: **[human]** install Python 3.12+ from python.org: the Python install manager needs no UAC prompt; the classic installer shows one unless **Use admin privileges when installing py.exe** is unticked. Then open a new shell and repeat. Wherever bare `python` does not start `<PY>`, run `& '<PY>' -s -B` in place of `python -s -B`. | `<PY>` is not under `\WindowsApps\`; `& '<PY>' -s -B --version` is 3.12 or newer |
| 0.3 | `python -s -B manage.py check`, then `python -s -B -c "from pathlib import Path; from rawlayer import conf; print(Path(conf.PROJECT_DIR).is_relative_to(Path.home().resolve()))"` | exit 0 with `apps: 5 apps, no collision`, `core: ok files=69`, `check: ok errors=0 refused=0`; the second command prints `True` |
| 0.4 | Optional: `python -s -B manage.py test core discord gmail mattermost notion`. This is the full suite an agent may run. A request for "all tests" does not name kakaotalk: run this and say that kakaotalk was skipped. Run `manage.py test kakaotalk` only when the owner names kakaotalk, and read only its final `test:` line. | exit 0 and the last line `test: core ok; discord ok; gmail ok; mattermost ok; notion ok` |
| 0.5 | **[human]**, deferred to 5.1. If `<DATA_PARENT>` does not exist yet, skip this row in Phase 0: 5.1 creates it and sends you back here. Windows Search must not index `<DATA_PARENT>`; on a new profile it usually does. The owner excludes it: Indexing Options → Modify → untick the folder. This covers every app. | if `<DATA_PARENT>` exists, the 5.1 probe prints `search: ok`; otherwise the row is deferred to 5.1 |
| 0.6 | Confirm the owner is available for [human] and UAC steps in this session. | the owner confirms, or names a time; until then run no [human] or [elevated] step |

## 5. Generic bring-up steps

The app cards in section 6 run these steps at the points they name, with their own keys, bounds and commands.

**5.0 The bootstrap tool.** `tools/bootstrap.py` does the config edits and the bookkeeping of these steps. Run it
as `python -s -B tools/bootstrap.py ...` from `<PROJECT>`, or with its full path from anywhere. It prints key
names and states, never config values. It refuses kakaotalk.
- `config get <app>`: each key's state (missing, placeholder, empty, set, or a path in or outside the data root and
  whether it is there), never its value. Booleans, integers and `*_source` values are shown.
- `config set <app> <KEY> <VALUE> [<KEY> <VALUE> ...]`: the 5.3 edit (its value rule; `--string` keeps every
  value a string). After writing, it reports the placeholders left, `config ok` or the refusal, and what
  `schedule install` would refuse.
- `config defaults <app>`: fills the keys `setup` fills (5.1) into a config that is already there; it never
  replaces a value that is set.
- `checkpoint mark <app> <step> [--field NAME=VALUE ...]`: records a passed gate in
  `<DATA_ROOT>\reports\bootstrap.json`. It takes only these steps:
  - section 5: `5.1`, `5.2`, `5.3`, `5.4`, `5.5`, `genesis_complete` (5.5), `5.6`, `5.7`, `5.8`, `5.9`, `5.10`;
  - gmail (6.1): `6.1.1`, `6.1.2`, `6.1.3`, `6.1.4`, `6.1.5`, `6.1.6`, `6.1.7`, `6.1.8`, `6.1.9`;
  - discord (6.2): `6.2`, `6.2.1`, `6.2.2`, `6.2.3`; notion (6.3): `6.3`, `6.3.1`, `6.3.2`, `6.3.3`;
    mattermost (6.4): `6.4`, `6.4.1`, `6.4.2`, `6.4.3`, `6.4.4`;
  - the health routine: `8`.

  `6.<card>.<n>` is numbered step `<n>` of a card whose steps are a numbered list, and a bare card id is a gate
  the card names without a step. Fields are integers or booleans: `rc`, `head_seq`, `total`, `errors`, `limited`,
  `messages_written`, `posts_written`, `channels`, `pages`, `excluded`, `unavailable`, `differing_rows`.
- `checkpoint show <app>` lists what is recorded, and `checkpoint clear <app> [<step>]` removes it.

**5.1 Setup.** `python -s -B manage.py setup <app>` creates `<DATA_PARENT>` with a protected ACL when it is
missing, and writes `<CFG>`. It fills the app's own paths:
- the archive folder, which it creates, and the credential files' paths, all in `<DATA_ROOT>`;
- for a desktop source, the desktop path in `<HOME>`, when it is there.

The app card lists what stays for you to fill.
Gates:
- Read every line. Exit 0, or exit 3 with `fill in config.json: <keys>; then manage.py check <app>`. `<CFG>` is
  written even when the same run also prints `data parent:` problem lines.
- A `data parent: not excluded from Windows Search` line: do 0.5 now. A `data parent:` line about the ACL: stop
  and use its section 9 row.
- After any `data parent:` line, verify with this read-only probe, never by rerunning `setup`. Once the config
  exists, `setup` exits 2 before it checks anything.
  `python -s -B -c "from rawlayer.capture.paths import DATA_PARENT as d; from rawlayer.management.commands.setup import acl_problem, search_problem; print('acl:', acl_problem(d) or 'ok'); print('search:', search_problem(d) or 'ok')"`
  Gate: `acl: ok` and `search: ok`.
- Exit 2 because the config already exists: keep it. Use `--force` only when the owner asks, because it replaces
  the config.

**5.2 Archive folder.** 5.1 creates `<DATA_ROOT>\archive`. For a config written before `setup` filled defaults,
run `python -s -B tools/bootstrap.py config defaults <app>`. It creates the folder and fills only keys that are
still unset. The fallback without the tool is
`New-Item -ItemType Directory -Force -Path <DATA_ROOT>\archive | Out-Null`.
Gate: `Test-Path -LiteralPath <DATA_ROOT>\archive` is True. Inside `<DATA_ROOT>` the folder inherits the protected
ACL.

**5.3 Fill the config** with a JSON-safe edit, never a text edit: a text edit broke the backslashes once and
`check` refused the config (exit 2, `config unreadable`).
`python -s -B tools/bootstrap.py config set <app> sources.<source_key> '<DATA_ROOT>\archive' <key> <value> ...`
- Values `true`, `false`, `null`, plain integers (`0`, `-5`) and values starting with `[` or `{` are parsed as
  JSON. Anything else, `007` and `3.5` included, is a string.
- Source paths go under `sources.<source_key>`; app options are top-level keys. Any other key is refused, and
  `v`, `impl`, `data_root` and `inputs_root` need `--force`, which needs the owner's approval (rule 10).
- Pass a list as one argument: `'["one", "two"]'`.
- Never end a quoted Windows path with a backslash.
- Before it writes anything, the tool refuses (exit 2, `<CFG>` unchanged):
  - a value that is still a placeholder (`<...>`), in a list too;
  - a `sources.<source_key>` that is not an absolute path to an existing folder;
  - a value the app's own option check rejects.
- When `<CFG>` is accepted (`config ok`), an edit that would make it refused is refused too: exit 2 with the
  reason, `<CFG>` unchanged. A config that is not accepted yet can be filled one key at a time.
- It keeps the file it replaces as `config.json.prev`.

Only when `tools/bootstrap.py` itself cannot run (it is missing, or exits 1), use this `$setKey` helper; never
after the tool refuses (exit 2): a refusal is a stop-and-fix, and the helper would skip its checks. The helper
applies the same value rule, but it checks nothing. Send the `$setKey` definition and the `python` call in one
command, every time (5.6 too).

```powershell
$setKey = @'
import json, re, sys
path, key, raw = sys.argv[1:4]
literal = raw in ('true', 'false', 'null') or raw[:1] in ('[', '{') or re.fullmatch(r'-?(0|[1-9][0-9]*)', raw)
value = json.loads(raw) if literal else raw
with open(path, encoding='utf-8') as f:
    cfg = json.load(f)
node = cfg
*parents, last = key.split('.')
for name in parents:
    node = node.setdefault(name, {})
node[last] = value
with open(path, 'w', encoding='utf-8') as f:
    json.dump(cfg, f, indent=2, ensure_ascii=False)
'@
python -s -B -c $setKey '<CFG>' 'sources.<source_key>' '<DATA_ROOT>\archive'
```

Gate: `config set` exits 0 with `config ok` (with the helper: it exits 0), and
`python -s -B manage.py check <app> --config <CFG>` exits 0 with `config: ok`. Exit 3 with `fill in:` names the
keys still to set.

**5.4 Authenticate** (apps with a collector). Do the app card's auth steps, then
`python -s -B -m <app>.collector --auth-check --config <CFG>`
Gate: exit 0 with `"status": "authenticated"` (gmail, and the `api` source of discord or notion) or
`"status": "local_ready"` (the `desktop` source).

**5.5 Bounded first collection.** Always run the collector before the first `collect`: capture of an empty
archive exits 75.
`python -s -B -m <app>.collector --max-items <N> --capture --config <CFG>`
Gates:
- The collector exits 0 with `"errors": 0`. `"limited": true` only means work is pending.
- `python -s -B manage.py check <app> --config <CFG>` shows `differing_rows=0` and `head_seq` of 1 or more.
- `python -s -B manage.py search <app> --since 1970-01-01 --mode timeline --limit 0 --json --config <CFG> | ConvertFrom-Json | Select-Object total, @{n='stale'; e={$_.index.stale}}`
  shows `total` above 0 and `stale` False.
  - A selector such as `--since` is required even in aggregate modes, and it drops undated rows.
  - `--limit 0` is allowed only with `timeline`.

Finish genesis before 5.8: repeat with a larger bound until `"limited": false`. `--max-items 0` means
unlimited; for notion, 0 falls back to `nt_max_pages`, which is unlimited at its default 0. The task's time limit
is one hour, so it must not start on a large backlog. This holds for gmail too:
[gmail/README.md](gmail/README.md) keeps `gm_sync` false until manual genesis is complete.
Once a run reports `"limited": false` with these gates passed, record it:
`python -s -B tools/bootstrap.py checkpoint mark <app> genesis_complete --field limited=false --field head_seq=<HEAD_SEQ>`.
Until it is recorded (or 5.10, which comes after genesis), doctor's `next` for an app with sync off stays 5.5.

**5.6 Turn on sync**, only where the app card allows it:
`python -s -B tools/bootstrap.py config set <app> <sync_key> true`. Only when the tool cannot run (5.3), run
`python -s -B -c $setKey '<CFG>' <sync_key> true` in the same command as the 5.3 definition.
Gates:
- That command exits 0, and `config set` prints no `schedule install would refuse:` line.
- `python -s -B tools/bootstrap.py config get <app>` shows `<sync_key>` as `true`. Without the tool:
  `(Get-Content -LiteralPath '<CFG>' -Raw | ConvertFrom-Json).<sync_key>` prints `True`.
- `check <app> --config <CFG>` exits 0 and prints no line starting with
  `note: schedule install would refuse this config:`. Examples of such a note:
  `app gmail: gm_sync requires client and token files`,
  `app notion: nt_sync requires a user-account task (ntn keychain)`,
  `app discord: dc_sync requires an absolute dc_desktop_path to an existing folder (SYSTEM has another %APPDATA%)`.

**5.7 Preview the task.**
`python -s -B manage.py schedule install <app> --config <CFG> --python <PY> --dry-run > $null`
Gate: exit 0. The XML holds profile paths, so it is discarded. On exit 2 the reason stays visible on stderr:
interpreter, data-root precheck (ACL, Windows Search, git work tree) or the app's `task_checks`.

**5.8 [elevated] Install, check and start** (one UAC prompt).
1. Print the start time and keep it as `<T0>`: `[DateTime]::UtcNow.ToString("yyyy-MM-dd'T'HH:mm:ss'Z'")`.
2. Run one 7.2 batch, which by default holds these three commands in this order:
   - `install <app> --config <CFG> --python <PY>`
   - `status <app> --config <CFG>`
   - `run <app>`

Gates, each on that command's own `rc=` line:
- install: `rc=0`, with `schedule: registered \llm-wiki\<app>-capture: ... as SYSTEM` and
  `definition read back: as registered`. If install's rc is not 0, the other two results do not count: fix it
  (section 9) and repeat 5.8. Installing again replaces the task.
- status: `rc=0`, with `schedule: \llm-wiki\<app>-capture registered (enabled)`, `definition: current (...)` and
  `last result:` `has not run yet` or `0 (ok)`.
- run: `rc=0`, with `schedule: started \llm-wiki\<app>-capture once`. This SYSTEM smoke run proves that SYSTEM can
  read the config, credentials and archive.

From a non-elevated prompt, status can exit 3 with `not registered, or not readable from this prompt`. That is
not evidence that the task is missing.

**5.9 Wait for the SYSTEM run** (no elevation). Every 30 s, for at most the task's limit, run one short poll:

```powershell
Select-String -LiteralPath '<DATA_ROOT>\reports\task.log' -Pattern '^(\S+) step=(capture|index) rc=' -ErrorAction SilentlyContinue |
  Where-Object { $_.Matches[0].Groups[1].Value -gt '<T0>' } | ForEach-Object Line
```

Stop when it prints a `step=index` line. If it has printed nothing after 2 minutes, run the section 3 reader:
- A newest block of only `run` means capture is still working: keep polling.
- Otherwise the run never reached capture: go to 5.10 now. If `<DATA_PARENT>\task-refused.log` was written after
  `<T0>`, the run was refused before its data root was known.

Gate: the new lines read `step=capture rc=0` and `step=index rc=0`. With sync on, the collector runs inside the
capture step; there is no separate line for it.

**5.10 [elevated] Final status.** One 7.2 batch with `status <app> --config <CFG>` only.
Gate: `rc=0` and `last result:` `0 (ok)`. Status also lists the newest run's step lines, which must all show
`rc=0`. On `running now`, the run is still going: wait and repeat 5.10 later.
After `0 (ok)`, run `python -s -B manage.py check <app> --config <CFG>` and the 5.5 timeline command once more.
Their `head_seq` and `total`, with `next run:` from this status, form the report's `baseline:` line (section 10).
Record the pass, which lets a non-elevated doctor report the app as live (section 4):
`python -s -B tools/bootstrap.py checkpoint mark <app> 5.10 --field head_seq=<HEAD_SEQ> --field total=<TOTAL>`.

**Several apps, or the fewest UAC prompts.**
1. Run each card through 5.7.
2. Print `<T0>` (5.8 step 1), then send one 7.2 batch (prompt 1) in 7.2's several-apps form: `install` for every
   app, then `status` for every app, then `run` for every app. Gate each command by 5.8's gates on its own `rc=`
   line.
3. Poll each app (5.9) with that `<T0>`.
4. Send one batch with `status` for every app (prompt 2), and finish 5.10 per app.

If one app's install rc is not 0, gate the other apps normally. After the fix, run 5.8–5.10 for the failed app
alone, with a new `<T0>`.

## 6. App cards

| app | task | start / every / limit | keys to set or check after `setup` (5.1) | sync option | details |
|---|---|---|---|---|---|
| gmail | `\llm-wiki\gmail-capture` | 00:44 / 3 h / 1 h | none: `setup` fills `sources.gm_archive`, `gm_client_file`, `gm_token_file` | `gm_sync` | [gmail/README.md](gmail/README.md) |
| discord | `\llm-wiki\discord-capture` | 00:14 / 3 h / 1 h | `dc_source` (the example sets `desktop`), then `config defaults discord` after changing it; `dc_desktop_path` when the cache is not at its standard place | `dc_sync` | [discord/README.md](discord/README.md) |
| notion | `\llm-wiki\notion-capture` | 03:29 / 6 h / 1 h | `nt_source` (the example sets `desktop`); `nt_desktop_path` when the database is not at its standard place | `nt_sync` | [notion/README.md](notion/README.md) |
| mattermost | `\llm-wiki\mattermost-capture` | 02:14 / 3 h / 1 h | `mm_url`, `mm_exclude_patterns` (the owner's values) | `mm_sync` | [mattermost/README.md](mattermost/README.md) |

### 6.1 gmail (Gmail API, read-only OAuth)

Values (5.1 fills them):
- `sources.gm_archive` = `<DATA_ROOT>\archive`
- `gm_client_file` = `<DATA_ROOT>\client.json`
- `gm_token_file` = `<DATA_ROOT>\token.json`

Keep `gm_sync` false until 6.1.8, and keep the default `gm_exclude_labels` ([gmail/README.md](gmail/README.md)).

**6.1.1** Run 5.1–5.3.

**6.1.2 Already set up?**
- If `Test-Path -LiteralPath <DATA_ROOT>\client.json` is True and
  `python -s -B -m gmail.collector --auth-check --config <CFG>` exits 0: run
  `python -s -B manage.py check gmail --config <CFG>`, note its `head_seq`, and skip to 6.1.7.
- If `client.json` exists but the check fails: use 6.1.9 instead.
- Otherwise `client.json` is missing, which on a new machine happens even when this Google account already has the
  project from an earlier bring-up. Ask the owner whether it does (an earlier bring-up report names it). If it
  does, select that project by name and only check the gates of 6.1.3 steps 2, 4 and 5 (**Disable API** shown,
  exactly the read-only scope, **In production**), changing nothing. Then, with the owner's approval (rule 10),
  do 6.1.3 steps 6–7 to create and move one new Desktop client, and continue with 6.1.5. If it does not, continue
  with 6.1.3.

Never create another project or client, or run `--auth` over a working token, without the owner's explicit
approval (rule 10).

**6.1.3 Google Cloud console** (once per Google account). Use the browser rules in 7.4; append `?hl=en` for
English labels. Touch only the new project: no other project, IAM, billing or organization setting.

1. Ask the owner first, in one message, to approve creating a dedicated project and to agree a short, neutral
   project name and app name (step 3), with no names or emails in either. Then, at
   `https://console.cloud.google.com/projectcreate?hl=en`, create the project with that name. Later pages must
   have this project selected; select it by name and never report its id. Record the project name and the app
   name (never the id) in the report, because a later recovery selects the project by that name.
   Gate: the project picker shows the new project's name.
2. `https://console.cloud.google.com/apis/library/gmail.googleapis.com?hl=en` → **Enable API**. Gate: **Disable
   API** is shown.
3. `https://console.cloud.google.com/auth/overview?hl=en`: **Get started**, then:
   1. **App name**: the name agreed in step 1. **User support email**: open the dropdown and click the option that
      contains `@` (the signed-in account), without reading, repeating or reporting its text. Then **Next**.
   2. Audience **External**, then **Next**.
   3. **[human]** **Contact email**: the owner types the same address and presses Enter so it becomes a chip, then
      **Next**. Do not type it yourself: you would have to read the address from the page or from chat.
   4. **[human]** **I agree to the Google API Services: User Data Policy.** The agent ticks it only when the owner
      explicitly delegates it; match the checkbox on `User Data Policy`, because its accessible name varies in
      spacing. Ask for 3 and 4 in one handshake.
   5. **Continue**, then **Create**.

   Gate: stay on the tab until the overview shows the configured app, and **Branding**
   (`https://console.cloud.google.com/auth/branding?hl=en`) no longer offers **Get started**. Navigating away
   during the save can cancel it; if **Get started** reappears, re-enter the same data in the same project.
4. `https://console.cloud.google.com/auth/scopes?hl=en`:
   1. **Add or remove scopes**.
   2. Under **Manually add scopes**, paste `https://www.googleapis.com/auth/gmail.readonly` into the
      **Manually paste scopes** box, then **Add to table**.
   3. Confirm that its row is ticked, then **Update**.
   4. Wait for the side panel to close, then **Save**.

   Gate: exactly one scope, the read-only one.
5. `https://console.cloud.google.com/auth/audience?hl=en`: **Publish app** → **Confirm**. Gate: the status is
   **In production**. Do not submit for verification; a personal app runs unverified and shows Google's warning.
   Testing-mode refresh tokens expire after seven days ([gmail/README.md](gmail/README.md)), so this step is
   required.

   **Publish app** can be disabled while showing only `aria-disabled=true`; do not keep clicking it. Then
   **Branding** (`https://console.cloud.google.com/auth/branding?hl=en`) needs an **Application home page**, an
   **Application privacy policy link** and an **Authorized domain**. If the owner has none, do 6.1.4 first. Stop
   and ask if Google demands domain-ownership verification.
6. `https://console.cloud.google.com/auth/clients?hl=en`: **Create client** → Application type **Desktop app** →
   name → **Create** → **Download JSON** (7.4). Never read, screenshot or dump the dialog or page that shows the
   client id and secret. If the dialog closed without a download, do not reopen the client to read it. Ask the
   owner (rule 10); with their approval, create another Desktop client and download from its creation dialog, and
   tell the owner that the first client can be deleted on the Clients page. Gate: step 7's count.
7. Move the file. The file name embeds the client id, so the commands below print only counts:

   ```powershell
   $dest = '<DATA_ROOT>\client.json'
   $dl = (New-Object -ComObject Shell.Application).NameSpace('shell:Downloads').Self.Path
   $hits = @(Get-ChildItem -LiteralPath $dl, ([Environment]::GetFolderPath('Desktop')) -File -Filter 'client_secret_*.json' -ErrorAction SilentlyContinue)
   "matches=$($hits.Count) client_exists=$(Test-Path -LiteralPath $dest)"
   if ($hits.Count -eq 1 -and -not (Test-Path -LiteralPath $dest)) { Copy-Item -LiteralPath $hits[0].FullName -Destination $dest -ErrorAction Stop; Remove-Item -LiteralPath $hits[0].FullName -ErrorAction Stop }
   ```

   - With 0 or several matches, ask the owner.
   - With `client_exists=True`, never replace the file: stop and ask.
   - If `Copy-Item` reports an error, the download stays where it is: stop and report.
   - The copy inherits the data root's ACL. Never open, print or hash the file.

   Gate: `Test-Path -LiteralPath <DATA_ROOT>\client.json` is True, and running the block again prints
   `matches=0 client_exists=True`.

**6.1.4 Public site for the consent screen** (only when 6.1.3 step 5 needs it).
1. Ask first. Propose a separate public static site with only two pages: a new GitHub Pages repository,
   published through the owner's logged-in `gh`.
2. Show the owner both pages' text and get approval before publishing. The pages must contain no names, emails or
   ids.
   - Home page: a personal, single-user tool through which the owner archives their own Gmail read-only to their
     own computer.
   - Privacy page: only the `gmail.readonly` scope for the owner's account; data kept only on the owner's
     machine; nothing sold, shared, or used for ads or AI training; revocation at
     `https://myaccount.google.com/permissions`; adherence to the Google API Services User Data Policy, including
     Limited Use.
3. Create a new, empty folder `<SITE>` under `$env:TEMP`, never inside `<PROJECT>` or any other repository; the
   run-from-`<PROJECT>` rule does not apply here. Write the two approved pages there as `index.html` and
   `privacy.html`, then run the block below.
   - The output of `git` and `gh` names the owner's account, so the block prints only booleans and exit codes.
   - Each step runs only when the one before it succeeded. If `Push-Location` fails, nothing runs.
   - The neutral commit identity keeps the owner's name and email out of the public commit metadata. Add no AI
     attribution lines.
   - `<site-repo>` must be a repository name that does not exist yet; never overwrite or delete a repository.
   - Never add a remote to this checkout, push it or enable Pages on it. This is the only push allowed (rule 9).

   ```powershell
   Push-Location -LiteralPath '<SITE>' -ErrorAction Stop
   try {
     $ok = [bool](Get-Command gh -ErrorAction SilentlyContinue); "gh_found=$ok"
     if ($ok) { $LASTEXITCODE = 0; git rev-parse --git-dir *> $null; $ok = $LASTEXITCODE -ne 0; "outside_any_repo=$ok" }
     if ($ok) { gh auth status *> $null; "gh_auth=$LASTEXITCODE"; $ok = $LASTEXITCODE -eq 0 }
     if ($ok) { git init -b main *> $null; git add index.html privacy.html; git -c user.name=Site -c user.email=site@example.invalid -c commit.gpgsign=false commit -m 'Add homepage and privacy policy' *> $null; "commit=$LASTEXITCODE"; $ok = $LASTEXITCODE -eq 0 }
     if ($ok) { gh repo create '<site-repo>' --public --source . --remote origin --push *> $null; "create=$LASTEXITCODE"; $ok = $LASTEXITCODE -eq 0 }
     if ($ok) { gh api -X POST 'repos/{owner}/{repo}/pages' -f 'source[branch]=main' -f 'source[path]=/' *> $null; "pages=$LASTEXITCODE" }
   } finally { Pop-Location }
   ```

   Gate: `gh_found=True`, `outside_any_repo=True`, then `gh_auth`, `commit`, `create` and `pages` all 0. The
   block returns to the previous folder. Otherwise, by the first failing value:
   - `gh_found=False`: **[human]** the owner installs GitHub CLI. Then rerun the block.
   - `outside_any_repo=False`: `<SITE>` is inside a repository (perhaps `<PROJECT>`) or was used before. Create
     a new empty folder under `$env:TEMP`, write the pages again and start over.
   - `gh_auth` not 0: **[human]** the owner runs `gh auth login`. Then rerun the block.
   - `commit` not 0: stop and report.
   - `create` not 0: stop and ask the owner. The repository may already exist: never run `create` for it again,
     and never overwrite or delete it.
   - `pages` not 0: once the cause is fixed, run only
     `Push-Location -LiteralPath '<SITE>' -ErrorAction Stop; try { gh api -X POST 'repos/{owner}/{repo}/pages' -f 'source[branch]=main' -f 'source[path]=/' *> $null; "pages=$LASTEXITCODE" } finally { Pop-Location }`
     (`gh` resolves `{owner}/{repo}` from `<SITE>`).
4. Every 30 s, for at most 10 minutes, check both pages; the block prints only status numbers:

   ```powershell
   $login = gh api user --jq .login
   foreach ($u in "https://$login.github.io/<site-repo>/", "https://$login.github.io/<site-repo>/privacy.html") { try { (Invoke-WebRequest -Uri $u -Method Head -SkipHttpErrorCheck -ErrorAction Stop).StatusCode } catch { 'request failed' } }
   ```

   Gate: both print 200.
5. In **Branding** (`https://console.cloud.google.com/auth/branding?hl=en`), fill the three fields from the
   clipboard, so that the login never appears in your output or your tool calls. For each value, run the one
   command, which prints nothing, then click the field and press Ctrl+V:
   1. **Application home page**: `Set-Clipboard -Value "https://$(gh api user --jq .login).github.io/<site-repo>/"`
   2. **Application privacy policy link**:
      `Set-Clipboard -Value "https://$(gh api user --jq .login).github.io/<site-repo>/privacy.html"`
   3. **Add domain**, then **Authorized domain 1**: `Set-Clipboard -Value "$(gh api user --jq .login).github.io"`

   If your browser tool cannot paste the system clipboard, make each paste a **[human]** step ("방금 복사한 값을 이
   칸에 Ctrl+V로 붙여넣어 주세요"). Then **Save**, and clear the clipboard with `Set-Clipboard -Value ' '`. Never
   read back, quote or report the host or the URLs.
   Gate: after a reload, each of the three fields is non-empty (check only that), and **Publish app** in 6.1.3
   step 5 is no longer `aria-disabled=true`.
6. Return to 6.1.3 step 5 and publish the app.

If the owner declines the site and has no home page and privacy page on a domain they control, stop and report
that the app cannot be published. Continue in Testing only if the owner explicitly chooses it:
- **[human]** the owner adds their account under Audience → **Test users**.
- Refresh tokens then expire after seven days. Scheduled runs fail with capture 75 until 6.1.5 is repeated.

**6.1.5 [human] Consent.**
1. Handshake first (7.1). The browser will open. Within five minutes the owner:
   - picks the account to archive (in a recovery, the same account as before);
   - on the unverified-app warning, clicks **Advanced**, then **Go to <app name> (unsafe)**;
   - then clicks **Continue** (or **Allow**).

   Wait for the owner's reply that they are at this machine, then run 7.3 at once: the five minutes start when it
   starts.
2. Run 7.3. It starts `gmail.collector --auth`, which waits at most five minutes on a `127.0.0.1` loopback with
   PKCE, opens the consent URL and removes its temp files afterwards.

Gate: `opened=True auth_exit=0 status=auth_ready`.
- `opened=True auth_exit=2 status=auth_timeout`: retry once with a fresh run. Never reuse an old URL.
- `opened=False`, any other status, or a second failure: stop and report.

**6.1.6** Gate: `python -s -B -m gmail.collector --auth-check --config <CFG>` exits 0. This is the real token
check. `--auth` writes the token file only when it succeeds, so after a failed or timed-out attempt the old,
possibly revoked, token is still there and its existence proves nothing.

**6.1.7 Bounded genesis.** `python -s -B -m gmail.collector --max-items 200 --capture --config <CFG>`
Gate: exit 0 and `"errors": 0`, then the 5.5 gates. `"limited": true` means messages remain. On a new archive
(6.1.3–6.1.6 just ran), `"messages_written"` must be above 0. When 6.1.2 sent you here with a `head_seq` of 1 or
more, `"messages_written": 0` is fine (no new mail since the last sync).

Repeat with a larger bound until `"limited": false`, before 6.1.8, then record it:
`python -s -B tools/bootstrap.py checkpoint mark gmail genesis_complete --field limited=false --field head_seq=<HEAD_SEQ>`
(doctor's `next` names these steps 6.1.7 and 6.1.8). `--max-items 1000` takes about four minutes
at the client's throttle of roughly 250 message fetches per minute: give it a long tool timeout, or run it in the
background and wait. A run stops fetching 3000 seconds after it starts. The listing counts toward that time but
is never cut short, and the capture and index that follow the sync are outside it, so a scheduled run that still
has genesis work can pass the one-hour limit.

**6.1.8** Only after genesis is complete (`"limited": false` in 6.1.7), run 5.6 with `gm_sync`. `task_checks`
refuses the install unless the client and token files exist at protected paths. Then run 5.7–5.10.

**6.1.9 Recovery.** Use it when the gmail task shows a non-ok last result. When the owner reports a revocation, a
password change or a Google account problem, start at step 3 without waiting for a failed run.
1. If `last result:` is `the interpreter or a file the action names was not found`, the run never started and
   task.log has no block for it: use that section 9 row. For `stopped (time limit, or by hand)`, use its row.
2. Read the newest block with the section 3 reader. Under `step=capture`:
   - `capture busy`: another process held the ledger lock (`collect`, a collector's `--capture` step, or
     `check`). The lock is released when that process ends; watch the next run. If the next run is busy too,
     report it. Never list process command lines to find the holder: they hold profile paths.
   - `capture skipped disk_floor=yes`: use the section 9 disk row.
   - rc 2 with `refused: precondition or configuration`: use the section 9 capture 2 row.
   - rc 3 with `gm_archive status=suspect`: use the section 9 suspect row; do not continue to step 3.
   - `origin error gm_archive ...` and `gm_archive status=error`: the sync inside capture failed. Go to step 3.
3. Run `python -s -B -m gmail.collector --auth-check --config <CFG>`.
   - **Exit 2 `auth_required`:** the token expired or was revoked (a password change, a revocation at the
     permissions page, an app still in Testing), or `client.json` or `token.json` is missing or unreadable.
     1. Confirm **In production** on the Audience page. Select the project by the name in the bring-up report.
     2. If `client.json` is missing, ask the owner (rule 10); with their approval, redo 6.1.3 steps 6–7 in the
        same project.
     3. Repeat 6.1.5, asking for the same account as before without naming it, e.g. "이전에 연결한 것과 같은
        Gmail 계정을 선택해 주세요".
     4. Then 6.1.6 and step 4.
   - **Exit 2 `auth_failed`, or exit 1:** stop and report.
   - **Exit 75 `transport_error` or `api_error`:** network or Google trouble. `"http_status": 403` can mean that
     the Gmail API was disabled. Retry once later, then report `status` and `http_status`. Exit 75 with any other
     status: stop and report.
   - **Exit 0:** go to step 4.
4. `--auth-check` proves that the token refreshes, not which account it belongs to. Only a sync compares the
   account with the archive's binding. While no run is in progress (the section 3 reader's newest block has its
   `step=index` line), run `python -s -B -m gmail.collector --max-items 200 --config <CFG>`. It runs without
   `--capture`; the next scheduled run captures what it writes.
   - **Exit 0:** after a fix in step 3 (a re-consent, a restored `client.json`), go to step 5. With no fix, the
     cause was probably transient (network, quota, busy). If the two newest scheduled runs have both failed (the
     section 3 reader with `$k = 2`), stop now. Otherwise watch the next run, and stop if it fails too. When you
     stop, report (section 10) each failing block's step and reason lines, `head_seq` and `total`, and ask before
     any repair.
   - **Exit 2 `account_changed`:** the owner chose another account at consent, and `token.json` now holds that
     account's working token. Replacing it is `--auth` over a working token (rule 10): ask for approval in the
     same 7.1 message as the consent handshake. Then repeat 6.1.5 once with the original account, then 6.1.6 and
     step 4 again. If it persists, stop: the archive is bound to its first account, and another mailbox needs its
     own data root.
   - **Exit 75 with `"limited": true`:** more new mail than the bound; what was fetched is kept. It is not a
     stall. Run it again (a larger bound such as `--max-items 1000` is fine) until it exits 0, then follow the
     exit 0 bullet.
   - **Exit 75 with `"errors": 1` and `"limited": false`:** run the same command once more, right away. If that
     second run exits 75 with `"errors": 1`, `"messages_written": 0` and `"limited": false`, a pending plan is
     stalled on a message deleted before genesis fetched it. With the owner's approval (rule 10), run once
     `python -s -B -m gmail.collector --full --max-items 200 --capture --config <CFG>`, then go to step 5.
     `--full` forces a fresh listing and never rewrites captured content. Any other result of the second run is
     handled by these bullets, without a third run.
   - **Exit 2 `auth_required`:** the token failed in the meantime. Go back to step 3.
   - **`collector_busy`:** another collector run, scheduled or manual, holds the lock. Retry after it.
   - **Anything else** (exit 75 with another `status`, exit 1): stop and report `status`, `http_status`,
     `errors`, `limited` and `messages_written`.
5. After any fix, prove it under SYSTEM. After a disk-floor skip, check both numbers of the section 9 disk row
   first. Print `<T0>` (5.8 step 1), then run one 7.2 batch with `run gmail`, then 5.9 and 5.10.

### 6.2 discord

Choose the source and set `dc_source` explicitly. It defaults to `api` when the key is absent, although the
config that `setup` writes from the example sets `desktop`.
- **desktop:** token-free and partial. It reads the Discord desktop cache in `dc_desktop_path`
  (`<HOME>\AppData\Roaming\discord`). **[human]** The Discord desktop app must be installed and signed in. Cache
  eviction never counts as deletion.
- **api:** a bot or user token in a file, `dc_token_file` = `<DATA_ROOT>\token.txt`.
  - The path must be absolute and under `<DATA_PARENT>`; otherwise the collector reports `unsafe_token_path`
    (exit 75).
  - The file holds only the token, on one line. Surrounding whitespace and a leading `Bot ` are ignored. A missing
    or empty file gives `auth_required` (exit 2).
  - Keep `dc_token_type` at `auto`: the collector tries the token as a bot, then as a user token on HTTP 401.
    `--auth-check` reports which one as `token_type`.
  - `DISCORD_BOT_TOKEN` and `DISCORD_TOKEN` take precedence over the file whenever they are set, and SYSTEM does
    not see user variables. So that 5.4 and 5.5 test the file the task will use, start those commands with
    `$env:DISCORD_BOT_TOKEN = $null; $env:DISCORD_TOKEN = $null;` in the same command.
  - With `dc_sync` on, `schedule install` (and the 5.6 gate) refuses a token path outside `<DATA_PARENT>` or
    in a folder that fails the data-root precheck, a missing token file, and a machine-wide `DISCORD_BOT_TOKEN`
    or `DISCORD_TOKEN`, which SYSTEM does see and which would replace the file. It never reads the token: 5.8's
    run is the first proof under SYSTEM.

**Bot setup [human]** (api with a bot token), at `https://discord.com/developers/applications`:
1. **New Application** → name → **Create**.
2. **Bot** → Privileged Gateway Intents → turn on **Message Content Intent** → **Save Changes**. Without it,
   server messages that do not mention the bot arrive with empty text. The collector counts one error (`"errors"`
   above 0, exit 75) only for a channel in which every message fetched that run is a plain message (type 0) with no
   content, attachments, embeds, stickers or forwards. A channel that also holds replies, pins or other message
   types is archived with empty text and no error, so `"errors": 0` does not prove the intent is on, and
   `--auth-check` does not detect it either. **[human]** the owner confirms that the intent is on.
3. **OAuth2** → **URL Generator**: scope `bot`; bot permissions **View Channels** and **Read Message History**.
   Open the generated URL and add the bot to each server to archive (this needs Manage Server there). A bot sees
   only the servers it was added to, never the owner's DMs.
4. **Bot** → **Reset Token**. The owner puts it into the token file (the handshake below), never into chat. Take
   no screenshot, snapshot or text dump of that page (7.4).

Gates: 5.4 prints `"status": "authenticated"` and `"token_type": "bot"`; after 5.5, `"channels"` is above 0.

**Token file handshake** (api). Create the file in place, so that it inherits the data root's ACL; an existing
file is reopened for the owner to overwrite, never deleted (rule 10). The command prints the file's write time:

```powershell
$t = '<DATA_ROOT>\token.txt'; if (-not (Test-Path -LiteralPath $t)) { New-Item -ItemType File -Path $t | Out-Null }; (Get-Item -LiteralPath $t).LastWriteTimeUtc.Ticks; Start-Process notepad -ArgumentList ('"' + $t + '"')
```

Keep the printed number as `<W0>`. Then **[human]**, e.g. "방금 열린 메모장 탭에 Discord 토큰 한 줄만 붙여넣고
Ctrl+S로 저장한 다음 Ctrl+W로 그 탭을 닫아 주세요. 창만 닫으면 메모장이 다음 실행 때 토큰 탭을 다시 엽니다. 채팅에는
붙여넣지 마세요." Closing the tab, not only the window, keeps Windows 11 Notepad's session restore from holding a
copy of the token outside `<DATA_PARENT>`.
Gate, without opening or printing the file:
`$f = Get-Item -LiteralPath '<DATA_ROOT>\token.txt'; $f.Length -gt 0 -and $f.LastWriteTimeUtc.Ticks -gt <W0>`
prints True.

Steps:
1. Run 5.1–5.3. 5.1 fills `sources.dc_archive` = `<DATA_ROOT>\archive`, plus the path of the source the config
   names: `dc_desktop_path` (desktop; only when `<HOME>\AppData\Roaming\discord` is there) or `dc_token_file` =
   `<DATA_ROOT>\token.txt` (api). Set `dc_source`, and after changing it run `config defaults discord`. A
   `dc_desktop_path` must be absolute: under SYSTEM, `%APPDATA%` points to the system profile. `check discord` does
   not read the token file, so the 5.3 gate passes before the file exists.
   - desktop: Gate: `Test-Path -LiteralPath` on `dc_desktop_path` is True.
   - api: with a bot token, the bot setup steps 1–3 first, then step 4 with the token file handshake. Gate: the
     handshake's gate. Only then run 5.4.
2. Run 5.4. Run 5.5 with `<N>` = 5; it bounds channels. Then finish genesis.
   - Channels the token cannot read (HTTP 403, e.g. private channels the bot was not given) count as `errors` on
     every run; they are not skipped. During genesis, exit 75 with `"errors"` above 0 is a stop: report the
     count.
   - Then ask the owner either to give the bot **View Channels** and **Read Message History** there, or to name
     channels to leave out. Leave them out with `dc_exclude_patterns`, case-insensitive fragments of the server,
     category or channel name, or keep only some with `dc_channel_ids`. Set them with
     `tools/bootstrap.py config set` (5.3), and never echo either in reports.
   - Rate limits and 5xx responses are already retried inside the collector.
3. Run 5.6 with `dc_sync`, then 5.7–5.10.

Recovery:
- `auth_failed` (HTTP 401): **[human]** replace the token through the token file handshake (the file exists;
  never delete it), then rerun `--auth-check`.
- Exit 75 with `"errors"` above 0 in a manual run, or `dc_archive status=error` with capture rc 75 in task.log:
  at least one channel failed. With `dc_sync` on, the whole origin commits nothing in such a run. A one-off
  failure clears on the next run. If it repeats, run `--auth-check` first, then treat it as the 403 case of step 2.
- Exit 2 `account_changed` (api, from a sync run; `--auth-check` does not detect it): stop. The archive is bound
  to the account that first synced into it; another account needs its own data root.
- Exit 3 suspect: use the section 9 suspect row.

### 6.3 notion

Sources:
- **desktop:** token-free and partial. It reads the Notion desktop database read-only, so Notion can stay open,
  through `nt_desktop_path` (`<HOME>\AppData\Roaming\Notion\notion.db`). **[human]** The Notion desktop app must
  be installed, signed in and synced. If that file does not exist, for example with a Store install, ask the owner
  where the database is.
- **api:** the official `ntn` CLI. **[human]** `ntn login` stores its credentials in the owner's keychain. Set
  `nt_cli` to the absolute path of `ntn.exe` when it is not on PATH.

Only desktop mode can sync inside the SYSTEM task. `task_checks` refuses `nt_sync` together with
`nt_source: api`. In api mode, run the collector in the owner's session and keep the SYSTEM task archive-only
(`nt_sync` false).

Steps:
1. Run 5.1–5.3. 5.1 fills `sources.nt_archive` = `<DATA_ROOT>\archive` and, in desktop mode, `nt_desktop_path`
   when the database is at its standard place. Set `nt_source`, and after changing it to desktop run
   `config defaults notion`. Desktop mode needs an absolute `nt_desktop_path`. Gate: `Test-Path` on that file is
   True.
2. Run 5.4. Run 5.5 with `<N>` = 3; it bounds pages. Then finish genesis with larger bounds until
   `"limited": false`.
3. Desktop mode: run 5.6 with `nt_sync`. Then run 5.7–5.10.

Recovery:
- `auth_required` (api): **[human]** `ntn login`, then `--auth-check`.
- `cli_missing` (exit 75, api): `ntn.exe` was not found, or `nt_cli` does not name an existing `.exe`. Set `nt_cli`
  with `tools/bootstrap.py config set` (5.3; if `ntn` is not installed, **[human]** the owner installs it), then
  rerun `--auth-check`.
- Exit 2 `account_changed` (api, from a sync run): stop. The archive is bound to the account that first synced
  into it; another account needs its own data root.
- Desktop `--auth-check` failure: confirm that `nt_desktop_path` exists and that Notion has synced.
- api: a captured page whose lookup returns 403 or 404 keeps its file and is marked gone (`pages_gone`), never
  deleted. A page not captured yet that returns 403 or 404 counts as an error (exit 75; see `error_counts`).
- Exit 3 suspect: use the section 9 suspect row.

### 6.4 mattermost

The app's collector (`mattermost/collector.py`, a port of the mattertok archiver) reads the server's REST API v4
with the owner's session token and keeps `<DATA_ROOT>\archive` in mattertok's format
([mattermost/README.md](mattermost/README.md)). Each channel is fetched with cursors, so a run fetches only new
posts; edits and deletions of posts already archived are not fetched again.

Values (5.1 fills the archive and the token file):
- `sources.mm_archive` = `<DATA_ROOT>\archive`
- `mm_url` = the server's `https://` address, which the owner gives
- `mm_token_file` = `<DATA_ROOT>\token.txt`
- `mm_exclude_patterns` = `[]` unless the owner names channels to skip, or mattertok's effective list when step 1
  continues a mattertok archive. Each pattern is a plain text fragment, not a glob or regex: a channel is skipped
  when its display name or name contains it, ignoring case, and it is never fetched. Never report the patterns.
  Pass a list as one argument, e.g. `'["one", "two"]'`.

**Sign-in [human].** The owner runs, in their own terminal from `<PROJECT>`:
`python -s -B -m mattermost.collector --login --config <CFG>`
It asks for the login id, the password (not shown) and, when the account uses one, an MFA code. It keeps only the
session token in `mm_token_file` and prints `"status": "auth_ready"`; the password is never stored. Repeated failed
sign-ins can lock the account: after one `auth_failed`, stop and ask. A personal access token, where the server
allows one, can go into the token file instead (the 6.2 token file handshake); it does not expire.
`MM_TOKEN` takes precedence over the file whenever it is set, and SYSTEM does not see user variables: start the
5.4 and 5.5 commands with `$env:MM_TOKEN = $null;` in the same command.

Steps:
1. Run 5.1–5.3 with the values above. With the owner's approval, an existing mattertok archive at `<MM_ARCHIVE>`
   can be continued instead of downloaded again: copy only its `manifest.json`, `users.json` and `posts\` into
   `<DATA_ROOT>\archive` (`files\` and `markdown\` are not read):
   `robocopy '<MM_ARCHIVE>' '<DATA_ROOT>\archive' manifest.json users.json /COPY:DAT; robocopy '<MM_ARCHIVE>\posts' '<DATA_ROOT>\archive\posts' /E /COPY:DAT`
   Exit 1 means files were copied. The first sync then resumes from mattertok's cursors.
   mattertok skipped some channels by default and this app skips none, so before the first sync set
   `mm_exclude_patterns` to mattertok's effective list: its `MM_EXCLUDE_CHANNELS` setting (comma-separated), or,
   when that is unset, the `DEFAULT_EXCLUDED_CHANNELS` tuple in mattertok's `src\mattertok\config.py`. The setting
   sits in mattertok's `.env` beside its credentials: read that one line only
   (`Select-String -Path '<mattertok>\.env' -Pattern '^MM_EXCLUDE_CHANNELS='`) or ask the owner. Without the list,
   the channels mattertok skipped are downloaded.
2. The sign-in above, then 5.4. Gate: `"status": "authenticated"`.
3. Run 5.5 with `<N>` = 20; it bounds channels. A new archive downloads whole histories at four requests a
   second, and a run also stops by itself after 40 minutes with `"limited": true`; repeat until
   `"limited": false`.
   - `"unavailable"` counts channels whose history the server refused (HTTP 403/404: left, archived, or no
     permission). They keep their files and are not errors, unless every channel was refused.
   - `"excluded"` counts channels skipped by `mm_exclude_patterns`.
   - Every scheduled run also reads the whole archive within the one-hour limit: the section 3 reader's
     `step=capture` line must show `seconds=` under 2700, or stop and report it before 5.7.
4. Run 5.6 with `mm_sync`, then 5.7–5.10.

Recovery:
- `auth_required` (exit 2; with `mm_sync` on, capture 75 with `mm_archive status=error`): the session expired or
  was revoked. **[human]** sign in again, then rerun `--auth-check`.
- `auth_failed` from `--login`: wrong id, password or MFA code. Stop and ask.
- `api_error` with a 3xx `http_status`: the server redirected and the collector never follows a redirect, so
  `mm_url` is not the server's own address. Ask the owner for it.
- Exit 75 with `"errors"` above 0: a channel or user lookup failed. With `mm_sync` on, the origin commits nothing
  in such a run; a one-off failure clears on the next run. If it repeats, run `--auth-check`, then report.
- Exit 2 `account_changed`: stop. The archive belongs to the account and server that first synced into it;
  another account needs its own data root.
- Exit 3 suspect: use the section 9 suspect row.

### 6.5 kakaotalk

Owner-only (rule 2). This file contains no procedure for it.

## 7. Human-in-the-loop handshakes

**7.1 Asking.**
- Send one short message in the owner's language: what to do, where, and any time limit. Then wait for the reply.
  Wait for it before a step with a deadline (6.1.5) and before launching 7.2, whose UAC prompt appears at once.
- For steps without a deadline, prefer polling an observable result (e.g. `Test-Path`) over asking again.
- Never ask for secrets. Name folders as `%USERPROFILE%\...`, never with the user name.
- Human-only steps:
  - sign-ins, passwords, 2-Step codes, CAPTCHAs, phone verification, billing
  - terms or policy checkboxes, unless explicitly delegated
  - choosing the account and clicking **Continue** or **Allow** on a consent page
  - every UAC **Yes**
  - installing or connecting browser extensions
- Example ask (`<앱 이름>` is the App name of 6.1.3 step 3): "기본 브라우저에 Google 로그인 창이 열립니다. 지금부터
  5분 안에 진행해 주세요: 보관할 Gmail 계정 선택 → 'Google에서 확인하지 않은 앱' 화면에서 '고급' →
  '<앱 이름>(으)로 이동(안전하지 않음)' → '계속'(또는 '허용')."

**7.2 Elevation.** Typing `! <command>` into an agent chat is not elevated. Launch one Administrator PowerShell
per batch of `schedule` subcommands. It writes each command's output, followed by an `rc=N` line, to a temp file,
and the script prints that file with profile paths masked.

```powershell
$py = '<PY>'; $project = '<PROJECT>'; $cfg = '<CFG>'
$q = { "'" + ([string]$args[0]).Replace("'", "''") + "'" }
$sched = @("install <app> --config $(& $q $cfg) --python $(& $q $py)", "status <app> --config $(& $q $cfg)", "run <app>")
$out = Join-Path $env:TEMP ('llm-wiki-elevated-' + [guid]::NewGuid() + '.txt')
$body = ($sched | ForEach-Object { "`$LASTEXITCODE = 1; & $(& $q $py) -s -B manage.py schedule $_ *>> $(& $q $out); `$c = `$LASTEXITCODE; ('rc=' + `$c) >> $(& $q $out); if (`$c -and -not `$rc) { `$rc = `$c }" }) -join '; '
$cmd = "`$rc = 0; Set-Location -LiteralPath $(& $q $project) -ErrorAction Stop; $body; exit `$rc"
$enc = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($cmd))
$p = $null
try { $p = Start-Process pwsh -Verb RunAs -Wait -PassThru -ErrorAction Stop -ArgumentList '-NoProfile', '-EncodedCommand', $enc } catch { 'exit=not-started (UAC declined, or pwsh not found)' }
if ($p) { "exit=$($p.ExitCode)" }
if (Test-Path -LiteralPath $out) {
  Get-Content -LiteralPath $out | Where-Object { $_ -notmatch '^\s+(runs|in):' } |
    ForEach-Object { $_ -replace [regex]::Escape($HOME.Replace('\', '\\')), '<HOME>' -replace [regex]::Escape($HOME), '<HOME>' -replace "(?<=[\\/])$([regex]::Escape((Split-Path $HOME -Leaf)))(?![\w.-])", '<user>' }
  Remove-Item -LiteralPath $out
} elseif ($p) { 'nothing ran: check the project folder' }
```

- `$sched` lists the batch's `schedule` subcommands in order. The default is 5.8's install, status and run.
  Build any other entry the same way, quoting each path with `& $q`, e.g. `"status <app> --config $(& $q $cfg)"`
  or `"run <app>"`.
- For several apps, give each app its own config path: replace the `$sched` line with the two lines below,
  keeping only the subcommands the step names (e.g. only the `status` part for 5.10 or section 8).

  ```powershell
  $cfgs = @{ gmail = '<DATA_PARENT>\gmail\config.json'; notion = '<DATA_PARENT>\notion\config.json' }; $apps = 'gmail', 'notion'
  $sched = @($apps | ForEach-Object { "install $_ --config $(& $q $cfgs[$_]) --python $(& $q $py)" }) + @($apps | ForEach-Object { "status $_ --config $(& $q $cfgs[$_])" }) + @($apps | ForEach-Object { "run $_" })
  ```

- Gate each command on its own `rc=` line. `exit=` is the first non-zero code, or 0 only when every command
  returned 0.
- `exit=not-started`: UAC was declined (or `pwsh` is missing) and nothing ran. Ask once more through 7.1, then
  stop and report. `nothing ran`: `<PROJECT>` is wrong.
- The script blocks until the owner answers UAC and the batch ends. Run it as one command with a timeout of at
  least 300 s, or in the background and wait for it. If it is killed anyway, the batch may still run after a late
  Yes: wait a minute, delete `$env:TEMP\llm-wiki-elevated-*.txt` without reading it (it holds unmasked paths),
  then learn the state with a 7.2 batch holding only `status <app> --config <CFG>`.
- The `runs:` and `in:` lines are dropped. The profile folder is shown as `<HOME>`, and any other path segment
  equal to the profile folder's name as `<user>`, because a profile reached through a junction prints its
  resolved path.
- `-EncodedCommand` keeps paths with apostrophes or double spaces intact. Passed as plain arguments, they would
  break, because `Start-Process` joins its arguments with single spaces.
- Only `schedule install`, `run`, `status` and `remove`, each with an app, may run elevated. `remove` needs the
  owner's explicit approval (rule 10), and a request to pause or stop an app's schedule is not that approval by
  itself. Tell the owner that there is no disable command, and that `remove` unregisters only that app's task
  while its config, archive and data stay. Then wait for a yes.
  - It takes no `--config`: `$sched = @("remove <app>")`.
  - Gate: `rc=0` and `schedule: removed \llm-wiki\<app>-capture`. An elevated `status <app>` afterwards exits 3
    with `is not registered, or not readable from this prompt`.
  - Then, without elevation, `python -s -B tools/bootstrap.py checkpoint clear <app> 5.10`: a doctor without
    elevation takes a recorded 5.10 as proof that the task is there, and would report the app as live (`8`).
    Clear it the same way when an elevated `status` shows a task deleted outside this file.
  - To bring the task back, run 5.7–5.10; 5.10 records the checkpoint again.

**7.3 Consent (gmail `--auth`).** The script runs the blocking command and waits until the consent URL appears.
It checks the URL's shape, opens it and never prints it. At the end it prints only `opened`, the exit code and
the `status`:

```powershell
$id = [guid]::NewGuid()
$err = Join-Path $env:TEMP "llm-wiki-consent-$id.err"; $out = Join-Path $env:TEMP "llm-wiki-consent-$id.out"
try {
  $p = Start-Process '<PY>' -ArgumentList '-s', '-B', '-m', 'gmail.collector', '--auth', '--config', '"<CFG>"' `
       -WorkingDirectory '<PROJECT>' -RedirectStandardError $err -RedirectStandardOutput $out -NoNewWindow -PassThru
  $null = $p.Handle; $opened = $false
  while (-not $p.HasExited) {
    if (-not $opened -and (Test-Path -LiteralPath $err)) {
      $u = Get-Content -LiteralPath $err | Where-Object { $_ -match '^https://accounts\.google\.com/' } | Select-Object -First 1
      if ($u -and $u -match 'redirect_uri=http%3A%2F%2F127\.0\.0\.1' -and $u -match 'gmail\.readonly') { Start-Process $u; $opened = $true }
      $u = $null
    }
    Start-Sleep -Milliseconds 500
  }
  $p.WaitForExit()
  $st = (Get-Content -LiteralPath $out -Raw -ErrorAction SilentlyContinue | ConvertFrom-Json -ErrorAction SilentlyContinue).status
  "opened=$opened auth_exit=$($p.ExitCode) status=$st"
} finally { Remove-Item -LiteralPath $err, $out -ErrorAction SilentlyContinue }
```

- `--auth` exits 0 with `status=auth_ready`, and 2 for every failure. `status` names the cause:
  - `auth_timeout`: no consent within five minutes.
  - `auth_failed`: consent denied, or the code exchange failed.
  - `auth_required`: `client.json` is unusable, or Google returned no refresh token.
  - `state_mismatch`, `unsafe_token_path`, `transport_error` or `error`.

  With `opened=False`, the URL never appeared or failed the shape check: do not retry.
- The script blocks until the collector exits: up to five minutes for the owner, plus up to one minute for the
  token exchange. Run it as one command with a timeout of at least 420 s, or in the background and wait for it.
- If the script is killed anyway, its `finally` does not run. Stop the leftover process with
  `Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.CommandLine -match 'gmail\.collector\s.*--auth(\s|$)' } | ForEach-Object { Stop-Process -Id $_.ProcessId }`.
  Then delete `$env:TEMP\llm-wiki-consent-*` without reading the files: they hold the consent URL.
- If your sandbox refuses to open a browser, ask the owner to run this script. Never pass the URL through chat.

**7.4 Browser automation.**
- Use your own browser extension or integration that drives the owner's signed-in browser. Do not enable Chrome
  remote debugging (CDP) or start a separate profile unless the owner asks.
- Work in a new tab. Never read cookies, storage or saved passwords, and never report the titles or URLs of
  existing tabs.
- Report progress as booleans. Console URLs carry project ids, so do not print them.
- Console pages show the signed-in account's name and email, in the account button and the support-email
  dropdown. Find controls by their label with a targeted element query (find by text or role) rather than with
  full-page snapshots or screenshots. Never quote snapshot text in messages or reports.
- On pages that show a client id or secret, take no screenshot, accessibility snapshot or page-text dump. Locate
  only the control labelled `Download JSON` with a targeted query, click it, and read nothing else that the query
  returns. If your browser tool cannot click without a snapshot, make this one click a **[human]** step.

## 8. Steady-state health routine

Run this per app after a scheduled run or when asked.

0. `python -s -B '<PROJECT>\tools\bootstrap.py' doctor`, without elevation. An app whose `next` is `8` has
   nothing outstanding that doctor can see. Any other `next` names the step or section 9 row to go to first.
   Doctor's task lines are `registration unknown` without elevation: step 2 is still the proof. An app that went
   live before this tool has no `5.10` checkpoint, so its `next` is `5.7` (`5.5`, or gmail's `6.1.7`, when its
   sync is off, as notion's `api` source keeps it): run this routine, and once steps 2–5 pass, record `5.10` with
   the command at the end of 5.10 (`<HEAD_SEQ>` from step 4, `<TOTAL>` from step 5). A recorded 5.10 also stands
   for genesis, which 5.5 finishes before 5.8.
1. Baseline: read the newest `<PROJECT>\reports\<app>-*.md` and take its `baseline:` line. If there is none, say
   so and treat this run as the baseline.
2. **[elevated]** One 7.2 batch with `status <app> --config <CFG>` for every app to check (7.2's several-apps
   form). Gate per app: `rc=0`, `registered (enabled)`, `definition: current`, and `last result:` `0 (ok)`. On
   `running now`, wait for the run to end (at most the limit), then start again at step 2. Note `next run:`.

   If the owner cannot answer a UAC prompt now (for example, they asked from another device), skip step 2 and run
   steps 1 and 3–5 without elevation. Report `last result` and `next run` as not verified, take the newest block's
   `step=capture` rc from step 3 as the latest run's result, and offer step 2 for when the owner is at the
   machine.
3. The section 3 reader: the newest block has `step=capture rc=0` and `step=index rc=0`.
4. `check <app> --config <CFG>`: `differing_rows=0` and `check: ok`.
   - `head_seq` never decreases. Every capture that commits raises it by one, even when nothing changed; task.log
     shows this as `capture done ... seq=N`.
   - If `head_seq` is unchanged across scheduled runs, none of them committed: read their blocks with the
     section 3 reader, `$k` set to the number of runs since the baseline (8 for a day of 3-hourly runs).
   - A `ledger busy` note is not a pass: check again after the run.
5. The 5.5 timeline command: `stale` is False. `total` normally does not drop. A drop alone is not a failure,
   because rows deleted at the source are hidden from it (gmail: also messages moved to Trash or Spam). Report the
   change.

Report per app (section 10): exit codes, `head_seq`, `total`, the last result and the next run.

## 9. Troubleshooting

| symptom | cause | fix |
|---|---|---|
| `setup` exit 2, config already there | existing config | keep it; `--force` only on the owner's request |
| `setup` line `data parent: not excluded from Windows Search`; `schedule install` (also `--dry-run`) exit 2 `config refused (data root not excluded from Windows Search)`. A collector exit 1 with `"error_type": "ConfigError"` (discord, notion) or `{"app": "gmail", "status": "error"}` (gmail) can mean this or any other config or precheck problem; run 5.7's dry run to see which. A capture refusal names no reason (capture 2 row) | the indexer would index private data | **[human]** 0.5, then the 5.1 probe (not `setup`, which is refused once the config exists) |
| `setup` line `data parent:` about the ACL (`without (OI)(CI)`, `not protected`, `ACL unreadable`); `schedule install` (also `--dry-run`) exit 2 `config refused (data root ACL ...)` | `<DATA_PARENT>` was not created by `setup`, or its ACL was changed | stop and report. Never delete, move, rename or re-create `<DATA_PARENT>`: it holds every app's data, kakaotalk's included. Never hand-edit ACLs. **[human]** the owner decides |
| `check` exit 2, `config unreadable` | invalid JSON after a text edit (backslashes) | stop and report: neither `tools/bootstrap.py config set` nor the 5.3 `$setKey` helper can read invalid JSON. **[human]** the owner repairs the file, or approves (rule 10) putting back the `config.json.prev` that the last `config set` kept, or `setup --force` |
| `check` refuses a path | a source folder that is missing or not absolute, or a data root outside `<DATA_PARENT>` | create it, use absolute paths |
| `check` prints `ledger busy`, no replay line | a capture is running | not verified yet; check again after it finishes |
| capture 2: `last result:` `2 (capture refused)`, or task.log `step=capture rc=2` with `refused: precondition or configuration` | the config or the data-root precheck failed: a missing path, a data root inside a git work tree, the ACL, Windows Search | run 5.7's dry run without elevation; its stderr names the reason. If the dry run exits 0, the cause is most likely one that only SYSTEM meets: git missing from the machine PATH (rerun the 0.1 machine-PATH git gate) |
| capture 3: `last result:` `3 (capture suspect)`, or task.log `<source_key> status=suspect` | a known key vanished from a fully read container; nothing of that origin was committed | report it; do not force (no `--full`, no `--rebuild-state`). Ask the owner whether the archive was damaged or the account switched |
| `search` exit 2, `give a query or ...` | aggregate modes still need a selector | add `--since 1970-01-01` or an app selector |
| `search` exit 2, no index yet | never collected | run the collector (5.5) or `collect` |
| `search` exit 2, index damaged or another version | outdated or broken index | `collect <app> --config <CFG> --rebuild-index` |
| `check` exit 1, `differing_rows>0` | folded state disagrees with the ledger | report it. With the owner's approval (it rebuilds the folded state from the ledger and changes neither the ledger nor the index), run `check <app> --config <CFG> --rebuild-state`. It prints `replay check=no differing_rows=N head_seq=N`, where N is what it found before rebuilding, and exits 0. `ledger busy` instead means nothing was rebuilt: retry after the run. Then `check` again: `differing_rows=0`, `head_seq` unchanged. If it still differs, stop and report |
| capture 75 on a first run | empty archive (no manifest yet) | run the collector before `collect` |
| capture 75, task.log line `capture skipped disk_floor=yes` | free space on the data drive minus the run's peak is under 10 GiB. The peak is 1 GiB plus the archive JSON that capture reads: gmail `exports` and `state`, discord `exports`, notion every `*.json`, mattermost none | report free GiB: `[math]::Round((Get-PSDrive -Name (Split-Path '<DATA_PARENT>' -Qualifier).TrimEnd(':')).Free / 1GB, 1)`, and the GiB needed (an upper bound that prints no names; mattermost: 11): `$s = 0; foreach ($f in Get-ChildItem -LiteralPath '<DATA_ROOT>\archive' -Recurse -Filter *.json -File -ErrorAction SilentlyContinue) { $s += $f.Length }; [math]::Round(11 + $s / 1GB, 1)`. **[human]** the owner frees space on that drive until free GiB is above the GiB needed. Never delete or move files yourself, least of all under `<DATA_PARENT>`. Before any SYSTEM proof run (6.1.9 step 5), check both numbers again |
| capture 75 with task.log line `capture busy`, or `step=index rc=75` with `<app>: busy reason=another index update holds the lock` | another process held the ledger lock (`collect`, a collector's `--capture` step, `check`, or the scheduled task) or the index lock (another run's index step) | wait and retry later; the lock dies with its process, so a repeat means some process still holds it. If the next scheduled run also shows it, report it. Never list process command lines to find the holder: they hold profile paths |
| `schedule install` exit 2, elevation | not an Administrator process | 7.2 |
| `schedule install` exit 2, task check (e.g. `app gmail: gm_sync requires client and token files`, `app notion: nt_sync requires a user-account task (ntn keychain)`) | the sync option without what it needs | fix the config, rerun 5.7 |
| Store-alias refusal, or `last result:` `the interpreter or a file the action names was not found` | `WindowsApps` alias, or a moved or removed interpreter (e.g. after a Python upgrade). The run never started, so task.log has no block for it | choose a real `<PY>` (0.2). Every app's task names the interpreter it was installed with, usually the same one. Find the in-scope apps with a task from the bring-up reports in `<PROJECT>\reports\`, or with one 7.2 batch of `status <app> --config <CFG>` for each in-scope app whose `<CFG>` exists (`registered` means it has a task). Run 5.7 with the new `<PY>` for each of them, print `<T0>` (5.8 step 1), then send one 7.2 batch with `install` for every one of them, then `status` for every one, then `run` for every one. Then run 5.9 per app and one 5.10 batch. kakaotalk's task needs the same: tell the owner (rule 2) |
| elevated status: `definition: its action is not this project's ...` | the checkout was moved, or the task was registered from another checkout | if `<PROJECT>` is no longer under `<HOME>`, stop and ask (0.1). Otherwise run 5.7–5.10 from this checkout for each in-scope app with a task, and tell the owner that kakaotalk's task needs the same (rule 2) |
| `last result:` `1 (capture error)` and no task.log block written after the run started | the run died before `collect` began: the checkout is outside a user profile (0.1), or an import failed | run 0.3's second command without elevation. `False`: the checkout is outside the profile; stop and tell the owner that every SYSTEM run fails there and that the checkout must sit under `%USERPROFILE%` (0.1); never move it yourself. `True`: stop and report (rule 7) |
| `last result:` `stopped (time limit, or by hand)`, or a block without `step=index` | the one-hour limit was hit | finish genesis manually with bounded collector runs (5.5); a mattermost run whose capture alone (6.4 step 3) passes the limit: stop and report it |
| `running now` longer than the limit | a hung run | wait for the limit to stop it, then read the section 3 reader |
| non-elevated status exit 3, `not registered, or not readable from this prompt` | SYSTEM task not readable without elevation | elevated status (7.2) |
| gmail not-ok last result; `--auth-check` exit 2 | `auth_required` (token expired or revoked), or `auth_failed` (stop) | 6.1.9 |
| discord collector `"channels": 0` with `"errors": 0` (bot) | the bot was added to no server | **[human]** step 3 of the bot setup in 6.2 |
| Google **Publish app** disabled | no home page, privacy link or authorized domain | 6.1.3 step 5, then 6.1.4 |
| Google console back at **Get started** | navigation during the save cancelled it | re-enter in the same project and wait for the overview |

## 10. Reporting

Write `<PROJECT>\reports\<app>-<YYYYMMDD-HHMM>-<task>.md` (gitignored; create the folder first if it is missing)
for every bring-up, health check and recovery, and reply with its summary. Include:
- a line `baseline: head_seq=N total=N next_run=<time>` carrying this run's values, taken at the end of the task
  while no run is in progress: `head_seq` from `check <app> --config <CFG>`, `total` from the 5.5 timeline command
  (for a bring-up, the ones after 5.10; for a health check, section 8 steps 4 and 5), and `next_run` from the last
  `schedule status`; then the change from the previous baseline;
- for each step: the step id, the command (placeholders instead of real paths), its exit code and the aggregate
  fields (counts, booleans, `head_seq`, `total`);
- what the human did, described without values; for a gmail bring-up, the project name and the app name, never
  the id;
- what remains, with the `next run:` time from `schedule status`.

Never include the items listed in rule 3.

## 11. Agent integration (Claude Code)

`python -s -B tools/bootstrap.py agents ...` (`tools/agentkit.py`) renders `tools/agent_templates/` into Claude
Code's global configuration, so that the ledgers can be searched and this runbook found from any folder. It
needs no elevation and touches no data. It installs:
- a marked block in `<HOME>\.claude\CLAUDE.md`, between `<!-- llm-wiki-raw-layer:begin ... -->` and
  `<!-- llm-wiki-raw-layer:end -->`; the rest of the file is kept byte for byte;
- the `raw-search` and `llm-wiki-setup` skills and the `raw-retriever` subagent under `<HOME>\.claude\`;
- the user-scope MCP server `llm_wiki_raw` (`tools/mcp_rawsearch.py`, read-only search of every app), registered
  with `claude mcp add-json --scope user`. kakaotalk is registered as named-only: the tools' app `all`, and
  `raw_status` without an app, leave it out, so it is searched only when a call names it.

Its manifest and backups live in `%LOCALAPPDATA%\llm-wiki-raw-layer\agentkit`, never in `<PROJECT>`.

1. `agents status`. Each item is `absent`, `current`, `stale` (an older version this tool wrote), `modified`
   (changed by hand since), `foreign` (not this tool's), `legacy` (a hand-made copy from before this tool: a
   `# llm-wiki-raw-layer` section of the global CLAUDE.md, or `.claude\agents\knowledge-retriever.md`) or
   `unreadable` (a global CLAUDE.md that is not UTF-8 or whose markers are broken: the owner fixes it by hand).
   `project_mismatch` means the integration points at another checkout. Exit 0 when every item is `current`.
2. `agents install --dry-run` (add `--adopt-legacy` when step 1 shows `legacy`). It prints the diff with paths
   masked and changes nothing. Show the owner the diff and wait for an explicit yes: it rewrites their global
   configuration (rule 10).
3. With that yes, the same command without `--dry-run`. `install` refuses a `legacy`, `modified` or `foreign`
   item unless `--adopt-legacy` or `--force` names it, and backs up whatever it replaces or removes. `--python
   <PY>` names the interpreter the MCP server runs with; without it, install keeps the interpreter the server was
   installed with (while that file is still there), else uses the one running the command. When a `claude mcp`
   call fails, the error ends with the CLI's own reason.
   Gate: `agents status` exits 0, and in a new Claude Code session opened outside `<PROJECT>`, `/mcp` lists `llm_wiki_raw` as connected and `raw_status` (no app) answers for the apps other than
   kakaotalk. Call it with kakaotalk only when the owner asks.
4. Re-run `install` after `git pull` or a moved checkout: a template change shows as `stale`, a moved one as
   `project_mismatch`. It keeps the recorded interpreter; pass `--python` again to change it.

`agents uninstall` removes what install wrote and nobody changed since (a `modified` item is kept and named;
`--force` removes it too, saving the changed copy first and naming where). `--restore` also puts back what install
replaced or removed, from the backups, including an original whose earlier restore failed. Without `--restore`,
the backups of the items it removes are forgotten (their files stay under the state folder's `backups`, and the
output says so). Both take `--dry-run`.

The checkout's own copies (`.claude/skills`, `.claude/agents`, `.agents/skills`) come from the same templates
with commands run from `<PROJECT>`: after editing a template, run `agents sync-repo`. `agents sync-repo --check`
exits 3 while they differ, and a test fails.
