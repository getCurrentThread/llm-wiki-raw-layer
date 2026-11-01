from rawlayer.registry import AppConfig, Option
from tools.appsetup import defaults, existing_path
from tools.archiveio import boolean, choice, text, identifiers, nonnegative


class NotionConfig(AppConfig):
    label = "notion"
    archive = "nt"
    options = (Option("nt_sync", False, boolean), Option("nt_cli", "", text),
               Option("nt_page_ids", [], identifiers), Option("nt_comments", True, boolean),
               Option("nt_max_pages", 0, nonnegative), Option("nt_source", "api", choice("api", "desktop")),
               Option("nt_desktop_path", "", text))
    wiki = ("nt", "page")
    schedule = {"task": r"\llm-wiki\notion-capture", "start": "03:29",
                "every_hours": 6, "limit_hours": 1,
                "description": "Capture Notion archive and update its search index"}

    def setup_defaults(self, cfg, note):
        # the archive in the data root; for the desktop source, Notion's database in the owner's profile (SYSTEM's
        # %APPDATA% is the system profile's), filled only when it is there
        home = {"nt_desktop_path": ("AppData", "Roaming", "Notion", "notion.db")}
        defaults(cfg, note, data={"sources.nt_archive": "archive"},
                 home=home if cfg.get("nt_source", "api") == "desktop" else None)

    def task_checks(self, cfg):
        # The existing core's scheduler uses SYSTEM, whereas ntn's keychain belongs
        # to the logged-in user. Do not silently register an unusable task.
        if not cfg.nt_sync:
            return []
        if cfg.nt_source == "api":
            return ["nt_sync requires a user-account task (ntn keychain)"]
        return existing_path(cfg, "nt_sync", "nt_desktop_path", "file", "%APPDATA%")
