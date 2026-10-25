from rawlayer.registry import AppConfig, Option
from tools.archiveio import boolean, text


class ClaudeCodeConfig(AppConfig):
    label = "claudecode"
    source_keys = ("cc_archive",)
    options = (Option("cc_sync", False, boolean), Option("cc_claude_dir", "", text))
    schedule = {"task": r"\llm-wiki\claudecode-capture", "start": "02:44",
                "every_hours": 3, "limit_hours": 1,
                "description": "Capture Claude Code transcripts and update its search index"}

    def setup_defaults(self, cfg, note):
        from pathlib import Path
        from tools.appsetup import fill, profile_path
        root = cfg.get("data_root")
        if not root:
            return
        fill(cfg, note, {"sources.cc_archive": Path(root, "archive"),
                         "cc_claude_dir": profile_path(".claude")}, folders=("sources.cc_archive",))

    def task_checks(self, cfg):
        import os
        if cfg.cc_sync and (not cfg.cc_claude_dir or not os.path.isabs(cfg.cc_claude_dir)
                            or not os.path.isdir(cfg.cc_claude_dir)):
            return ["cc_sync requires an absolute cc_claude_dir to an existing folder (SYSTEM has another home)"]
        return []
