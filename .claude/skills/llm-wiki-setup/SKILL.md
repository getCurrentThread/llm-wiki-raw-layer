---
name: llm-wiki-setup
description: Install, configure, turn on scheduled collection, health-check or recover the llm-wiki raw layer apps (gmail, discord, notion, mattermost) on this Windows machine, following the repository's gated runbook llm.md and its tools/bootstrap.py. Use when asked to set up or bring up an app, enable capture or its schedule, check health or fix a failed run (e.g. "gmail 수집 켜줘", "디스코드 스케줄 등록", "수집 상태 점검", "노션 캡처 복구"), or to install the agent integration.
---

Operate the llm-wiki raw layer by its runbook, `llm.md`, and the checkout's rules, `AGENTS.md`:
- `llm.md`
- `AGENTS.md`

The tool the runbook uses, run from the repository root (the folder holding manage.py) (`BOOT` below):
`python -s -B tools/bootstrap.py`

1. Read llm.md whole before running any step (its section 1). Its ground rules (section 2) bind every step:
   kakaotalk is off-limits, no code changes while operating, no commits or pushes, and the owner's explicit
   approval for anything irreversible or replacing.
2. Run `BOOT doctor`. Its `machine` block answers Phase 0. Each app's `next` is the llm.md step to resume at; `8`
   means live. Use `--json` when you need the fields, and `--app <app>` for one app.
3. For each app the owner named, follow its card in section 6 from the step `next` names, after reading the steps
   before it. Pass every gate before the next step; when a gate fails, stop and report (section 10).
4. Edit configs only with `BOOT config set <app> <key> <value> ...` and `BOOT config defaults <app>` (llm.md 5.0
   and 5.3). They never print values, and neither do you: no tokens, client files, URLs of the owner's servers or
   exclusion patterns in messages. After a gate passes, record it with `BOOT checkpoint mark <app> <step>` (the
   steps and fields that 5.0 lists).
5. [human] steps (sign-ins, consent, bot setup, Windows Search) go to the owner through the 7.1 handshake: say
   exactly what to do, then wait. [elevated] steps go through one batched UAC prompt (7.2).
6. Report by section 10: the steps run, their gates and exit codes, and aggregates only.

The agent integration (the global CLAUDE.md block, these skills, the raw-retriever subagent and the
`llm_wiki_raw` MCP server) is managed with `BOOT agents status|install|uninstall` (llm.md section 11). Run
`install --dry-run` first, show the owner the diff, and install only with their approval.
