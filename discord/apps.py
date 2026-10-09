from rawlayer.registry import AppConfig, Option
from tools.archiveio import boolean, text, nonnegative, identifiers


def _patterns(value):
    if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
        raise ValueError("dc_exclude_patterns")
    return list(value)


def _token_type(value):
    if value not in ("auto", "bot", "user"):
        raise ValueError("dc_token_type")
    return value


def _source(value):
    if value not in ("api", "desktop"):
        raise ValueError("dc_source")
    return value


class DiscordConfig(AppConfig):
    label = "discord"
    source_keys = ("dc_archive",)
    options = (Option("dc_exclude_patterns", [], _patterns, attr="dc_exclude"),
               Option("dc_sync", False, boolean), Option("dc_token_file", "", text),
               Option("dc_token_type", "auto", _token_type), Option("dc_channel_ids", [], identifiers),
               Option("dc_refresh_days", 7, nonnegative), Option("dc_source", "api", _source),
               Option("dc_desktop_path", "", text))
    schedule = {"task": r"\llm-wiki\discord-capture", "start": "00:14",
                "every_hours": 3, "limit_hours": 1,
                "description": "Capture Discord archive and update its search index"}

    def setup_defaults(self, cfg, note):
        # the archive in the data root; the desktop source's cache folder in the owner's profile (SYSTEM's %APPDATA%
        # is the system profile's), filled only when it is there; the api source's token file in the data root
        from pathlib import Path
        from tools.appsetup import fill, profile_path
        root = cfg.get("data_root")
        if not root:
            return
        values = {"sources.dc_archive": Path(root, "archive")}
        if cfg.get("dc_source", "api") == "desktop":
            values["dc_desktop_path"] = profile_path("AppData", "Roaming", "discord")
        else:
            values["dc_token_file"] = Path(root, "token.txt")
        fill(cfg, note, values, folders=("sources.dc_archive",))

    def task_checks(self, cfg):
        # the task runs as SYSTEM: its %APPDATA% is the system profile's and it sees no user variables, so the
        # desktop cache and the token file must be named; the token file is checked as the collector reads it, and a
        # machine-wide DISCORD_BOT_TOKEN / DISCORD_TOKEN (which SYSTEM does see) would be used in its place
        if not cfg.dc_sync:
            return []
        import os
        if cfg.dc_source == "desktop":
            p = cfg.dc_desktop_path
            if not p or not os.path.isabs(p) or not os.path.isdir(p):
                return ["dc_sync requires an absolute dc_desktop_path to an existing folder (SYSTEM has another "
                        "%APPDATA%)"]
            return []
        from rawlayer.capture.config import ConfigError
        from tools.archiveio import CollectorError
        from .collector import machine_token_vars, token_path
        try:
            p = token_path(cfg.dc_token_file)
        except (CollectorError, ConfigError):
            return ["dc_sync requires a protected token path (dc_token_file under the data parent)"]
        problems = [] if p.is_file() else ["dc_sync requires a token file"]
        found = machine_token_vars()
        if found:
            problems.append(f"a machine-wide {' and '.join(found)} would replace dc_token_file in the SYSTEM task")
        return problems
