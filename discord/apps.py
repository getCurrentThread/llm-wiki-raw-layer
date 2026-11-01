from rawlayer.registry import AppConfig, Option
from appkit.setup import defaults, existing_path
from appkit.archive import boolean, choice, text, nonnegative, identifiers


class DiscordConfig(AppConfig):
    label = "discord"
    archive = "dc"
    options = (Option("dc_exclude_patterns", [], identifiers, attr="dc_exclude"),
               Option("dc_sync", False, boolean), Option("dc_token_file", "", text),
               Option("dc_token_type", "auto", choice("auto", "bot", "user")),
               Option("dc_channel_ids", [], identifiers), Option("dc_refresh_days", 7, nonnegative),
               Option("dc_source", "api", choice("api", "desktop")), Option("dc_desktop_path", "", text))
    wiki = ("dc", "chat")
    schedule = {"task": r"\llm-wiki\discord-capture", "start": "00:14",
                "every_hours": 3, "limit_hours": 1,
                "description": "Capture Discord archive and update its search index"}

    def setup_defaults(self, cfg, note):
        # the archive in the data root; the desktop source's cache folder in the owner's profile (SYSTEM's %APPDATA%
        # is the system profile's), filled only when it is there; the api source's token file in the data root
        if cfg.get("dc_source", "api") == "desktop":
            defaults(cfg, note, data={"sources.dc_archive": "archive"},
                     home={"dc_desktop_path": ("AppData", "Roaming", "discord")})
        else:
            defaults(cfg, note, data={"sources.dc_archive": "archive", "dc_token_file": "token.txt"})

    def task_checks(self, cfg):
        # the task runs as SYSTEM: its %APPDATA% is the system profile's and it sees no user variables, so the
        # desktop cache and the token file must be named; the token file is checked as the fetch step reads it, and a
        # machine-wide DISCORD_BOT_TOKEN / DISCORD_TOKEN (which SYSTEM does see) would be used in its place
        if not cfg.dc_sync:
            return []
        if cfg.dc_source == "desktop":
            return existing_path(cfg, "dc_sync", "dc_desktop_path", "folder", "%APPDATA%")
        from rawlayer.capture.config import ConfigError
        from appkit.archive import FetchError, credential_path
        from .fetch import machine_token_vars
        try:
            p = credential_path(cfg.dc_token_file)
        except (FetchError, ConfigError):
            return ["dc_sync requires a protected token path (dc_token_file under the data parent)"]
        problems = [] if p.is_file() else ["dc_sync requires a token file"]
        found = machine_token_vars()
        if found:
            problems.append(f"a machine-wide {' and '.join(found)} would replace dc_token_file in the SYSTEM task")
        return problems
