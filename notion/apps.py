from rawlayer.registry import AppConfig, Option
from appkit.setup import defaults, existing_path
from appkit.archive import boolean, text, identifiers, nonnegative


class NotionConfig(AppConfig):
    label = "notion"
    archive = "nt"
    options = (Option("nt_sync", False, boolean), Option("nt_page_ids", [], identifiers),
               Option("nt_max_pages", 0, nonnegative), Option("nt_desktop_path", "", text))
    wiki = ("nt", "page")
    schedule = {"task": r"\llm-wiki\notion-capture", "start": "03:29",
                "every_hours": 6, "limit_hours": 1,
                "description": "Capture Notion archive and update its search index"}

    def setup_defaults(self, cfg, note):
        # the archive in the data root; Notion's database in the owner's profile (SYSTEM's %APPDATA% is the system
        # profile's), filled only when it is there
        defaults(cfg, note, data={"sources.nt_archive": "archive"},
                 home={"nt_desktop_path": ("AppData", "Roaming", "Notion", "notion.db")})

    def task_checks(self, cfg):
        return existing_path(cfg, "nt_sync", "nt_desktop_path", "file", "%APPDATA%")
