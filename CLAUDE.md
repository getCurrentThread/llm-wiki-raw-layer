@AGENTS.md

# Claude Code notes

- `/raw-search <question>` (the `raw-search` skill) answers a question from the raw ledgers
  with cited records, through `manage.py search`. The `raw-retriever` subagent does the same in its own context: use it
  for broad or multi-app questions. The `llm-wiki-setup` skill runs llm.md's setup, health and recovery steps.
- The skills and the subagent are installed globally from `tools/agent_templates/` (llm.md section
  11): edit the templates, then run `agents install` again.
- Before committing, run `python -s -B manage.py test core claudecode discord gmail kakaotalk mattermost notion` and
  `python -s -B manage.py check`, and grep the diff for organisation identifiers, profile paths and secrets.
