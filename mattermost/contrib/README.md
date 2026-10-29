# mattermost/contrib: archived reference code

## What this is

`mattertok/` is mattertok 0.1.0, the Mattermost client (REST API v4 + WebSocket) from which this app's collector
(`../collector.py`) was ported. The original checkout was lost; the sources were recovered verbatim from session
transcripts and then scrubbed of organisation values. Its own README (Korean) is `mattertok/README.md`.

## Status

Archived reference only.

- Not imported by the app. The app is standard-library only; mattertok needs `httpx` and `websockets`.
- Not run by `manage.py test` or `manage.py check`; `contrib/` and `contrib/mattertok/` are not packages (no `__init__.py`).
- Not covered by the core checksums.
- Behaviour is kept as recovered; only the scrub below was applied.

## Scrub

- No default server: `MM_URL` is required (`DEFAULT_URL` is empty; `load_settings()` exits if it is unset). Examples use
  `https://mattermost.example.com`.
- No default channel exclusion patterns: `DEFAULT_EXCLUDED_CHANNELS` is empty. Set `MM_EXCLUDE_CHANNELS`
  (comma-separated) yourself. The selfchecks pass explicit patterns.
- Host names, local paths (`<PROJECT>`, `<PYTHON>`, `<LOGS>`), person names and team names in docs and test data
  are placeholders.

## Running it (only if ever needed)

Use a separate venv; never install into the app's environment.

```powershell
cd <PROJECT>\mattermost\contrib\mattertok
python -m venv .venv
.\.venv\Scripts\python -m pip install httpx websockets
.\.venv\Scripts\python -m pip install -e .
$env:MM_URL = "https://mattermost.example.com"
$env:MM_TOKEN = "<token>"          # or run `mattertok login` (MM_LOGIN_ID / MM_PASSWORD)
.\.venv\Scripts\mattertok --help
```

Offline selfchecks (fake clients, no network): `python selfcheck.py`, `python selfcheck_live.py`.
`mt_reconcile.py` is a read-only archive-versus-server count check.

## Feature map

| mattertok feature | Where it lives now |
|-------------------|--------------------|
| login / session (`login`, token cache) | `collector.py` (`--login`, `--auth-check`, the token file; ported) |
| `whoami`, `doctor` (account and server-policy checks) | contrib only (not ported) |
| channels list, teams, unread | `collector.py` (`discover`, `plan`; ported for archiving only), CLI listing: contrib only (not ported) |
| read (channel / DM / thread) | contrib only (not ported); the app reads the archive, not the server |
| search (server-side) | contrib only (not ported); local search is `../search.py` (FTS5) |
| send | `admin.py`: `python -s -B -m mattermost.admin post` |
| watch (WebSocket) | contrib only (not ported) |
| archive (backfill / forward cursors, manifest, `posts/*.jsonl`, finalize) | `collector.py` (`sync`, `fetch`, `publish`; ported) |
| live (WebSocket archiving service, lock, gap fill) | contrib only (not ported) |
| attachments download | `collector.py` (`store_files`; ported, `files.json` blob records) |
| Markdown rendering / export | contrib only (not ported) |
| reconcile (`mattertok reconcile`, `mt_reconcile.py`) | contrib only (not ported) |
| selfcheck / selfcheck_live | contrib only; the app's tests are in `../tests/` |
| channel / member / webhook administration (plan / apply / verify) | `admin.py` (new; not part of mattertok) |
