from rawlayer.registry import AppConfig, Option
from appkit.setup import defaults, existing_path
from appkit.archive import boolean, text, identifiers


class DiscordConfig(AppConfig):
    label = "discord"
    archive = "dc"
    options = (Option("dc_exclude_patterns", [], identifiers, attr="dc_exclude"),
               Option("dc_sync", False, boolean), Option("dc_channel_ids", [], identifiers),
               Option("dc_desktop_path", "", text))
    wiki = ("dc", "chat")
    schedule = {"task": r"\llm-wiki\discord-capture", "start": "00:14",
                "every_hours": 3, "limit_hours": 1,
                "description": "Capture Discord archive and update its search index"}

    def setup_defaults(self, cfg, note):
        # the archive in the data root; the desktop cache folder in the owner's profile (SYSTEM's %APPDATA% is the
        # system profile's), filled only when it is there
        defaults(cfg, note, data={"sources.dc_archive": "archive"},
                 home={"dc_desktop_path": ("AppData", "Roaming", "discord")})

    def task_checks(self, cfg):
        # the task runs as SYSTEM, whose %APPDATA% is the system profile's: the desktop cache must be named
        return existing_path(cfg, "dc_sync", "dc_desktop_path", "folder", "%APPDATA%")
