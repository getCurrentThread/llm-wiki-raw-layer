from rawlayer.registry import AppConfig, Option
from tools.archiveio import boolean, text, identifiers, nonnegative
from tools.wikiexport import options as wiki_options


def _source(value):
    if value not in ("api", "desktop"):
        raise ValueError("nt_source")
    return value


class NotionConfig(AppConfig):
    label = "notion"
    source_keys = ("nt_archive",)
    options = (Option("nt_sync", False, boolean), Option("nt_cli", "", text),
               Option("nt_page_ids", [], identifiers), Option("nt_comments", True, boolean),
               Option("nt_max_pages", 0, nonnegative), Option("nt_source", "api", _source),
               Option("nt_desktop_path", "", text)) + wiki_options("nt")
    schedule = {"task": r"\llm-wiki\notion-capture", "start": "03:29",
                "every_hours": 6, "limit_hours": 1,
                "description": "Capture Notion archive and update its search index"}

    def setup_defaults(self, cfg, note):
        # the archive in the data root; for the desktop source, Notion's database in the owner's profile (SYSTEM's
        # %APPDATA% is the system profile's), filled only when it is there
        from pathlib import Path
        from tools.appsetup import fill, profile_path
        root = cfg.get("data_root")
        if not root:
            return
        values = {"sources.nt_archive": Path(root, "archive")}
        if cfg.get("nt_source", "api") == "desktop":
            values["nt_desktop_path"] = profile_path("AppData", "Roaming", "Notion", "notion.db")
        fill(cfg, note, values, folders=("sources.nt_archive",))

    def task_checks(self, cfg):
        # The existing core's scheduler uses SYSTEM, whereas ntn's keychain belongs
        # to the logged-in user. Do not silently register an unusable task.
        if not cfg.nt_sync:
            return []
        if cfg.nt_source == "api":
            return ["nt_sync requires a user-account task (ntn keychain)"]
        import os
        p = cfg.nt_desktop_path
        if not p or not os.path.isabs(p) or not os.path.isfile(p):
            return ["nt_sync requires an absolute nt_desktop_path to an existing file (SYSTEM has another %APPDATA%)"]
        return []
