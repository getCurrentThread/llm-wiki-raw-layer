@AGENTS.md

# Claude Code notes

- `/raw-search <question>` (the `raw-search` skill in `.claude/skills/`) answers a question from the raw ledgers
  with cited records, through the `llm_wiki_raw` MCP tools when they are connected, else `manage.py search` from
  the repository root. The `raw-retriever` subagent (`.claude/agents/`) does the same in its own context: use it
  for broad or multi-app questions. The `llm-wiki-setup` skill runs llm.md's setup, health and recovery steps.
- These files are generated from `tools/agent_templates/`: edit the templates, then run
  `python -s -B tools/bootstrap.py agents sync-repo` (a test fails while they differ). The global install for use
  from any folder is llm.md section 11.
- Before committing, run `python -s -B manage.py test core discord gmail mattermost notion` and
  `python -s -B manage.py check`, and grep the diff for organisation identifiers, profile paths and secrets.
