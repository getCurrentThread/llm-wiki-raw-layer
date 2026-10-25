---
name: raw-search
description: Answer a question from the owner's raw ledgers of KakaoTalk, Mattermost, Discord, Notion, Gmail and Claude Code (what was said, who, when, how many; find a conversation, mail or page) with cited, masked records. Use for requests like "카톡에서 송금 얘기 찾아줘", "지난주 마터모스트 배포 논의", "노션 회의록", "지메일 영수증 몇 통".
argument-hint: <question or search terms>
allowed-tools: {{MCP_TOOLS}}, {{BASH_SEARCH}}
---

Answer the request from the raw ledgers. Request: $ARGUMENTS

{{include:_search.md}}

## Answer

Answer first, then the evidence. Cite each claim by its app, date, room/channel/page (masked when it names a
person) and a masked sender, plus the query and filters that found it. For counts and trends, give the aggregate rather than rows. Say what is
uncertain or missing (a stale index, filters that excluded rows, zero hits).
