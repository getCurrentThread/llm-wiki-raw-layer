from rawlayer.registry import AppConfig, Option
from tools.appsetup import defaults, existing_path
from tools.archiveio import boolean, text


class ClaudeCodeConfig(AppConfig):
    label = "claudecode"
    archive = "cc"
    options = (Option("cc_sync", False, boolean), Option("cc_claude_dir", "", text))
    wiki = ("cc", "chat")       # a session is a conversation: `manage.py collect claudecode`
    schedule = {"task": r"\llm-wiki\claudecode-capture", "start": "02:44",
                "every_hours": 3, "limit_hours": 1,
                "description": "Capture Claude Code transcripts and update its search index"}

    def setup_defaults(self, cfg, note):
        defaults(cfg, note, data={"sources.cc_archive": "archive"}, home={"cc_claude_dir": (".claude",)})

    def task_checks(self, cfg):
        return existing_path(cfg, "cc_sync", "cc_claude_dir", "folder", "home")
