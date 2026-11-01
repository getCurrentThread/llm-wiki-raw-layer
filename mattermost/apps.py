# -*- coding: utf-8 -*-
from rawlayer.registry import AppConfig, Option
from appkit.setup import defaults
from appkit.archive import boolean, identifiers, nonnegative, text


def _url(v):
    if not isinstance(v, str) or (v and not v.startswith("https://")):
        raise ValueError("mm_url")
    return v


class MatterConfig(AppConfig):
    label = "mattermost"
    # config.json "sources": the archive directory (mattertok's format; the app's fetch step keeps it current)
    archive = "mm"
    # channels whose display name or name contains one of these (casefolded) are not read (mattertok's rule).
    # mm_sync: live capture runs the app's fetch step first (mattermost/fetch.py); mm_url is the server and
    # mm_token_file a private file under the data parent holding the session token `--login` writes.
    # mm_files_max_mb: the fetch step also keeps each attached file of at most this many MB (0: none).
    options = (Option("mm_exclude_patterns", [], identifiers, attr="mm_exclude"),
               Option("mm_sync", False, boolean), Option("mm_url", "", _url),
               Option("mm_token_file", "", text), Option("mm_files_max_mb", 0, nonnegative))
    wiki = ("mm", "chat")
    # The scheduled task `manage.py schedule install mattermost` registers: `manage.py capture mattermost --config
    # <CFG>` as SYSTEM, daily from `start` (local time) and every `every_hours` hours after it, at most `limit_hours`
    # hours per run. Start minutes are staggered across the apps: README.md, "Scheduled tasks".
    schedule = {
        "task": r"\llm-wiki\mattermost-capture",
        "start": "02:14",
        "every_hours": 3,
        "limit_hours": 1,
        "description": "llm-wiki-raw-layer mattermost: capture the Mattermost archive into the raw ledger every 3 h, "
                       "then update the search index",
    }

    def setup_defaults(self, cfg, note):
        # the archive and the token file --login writes, in the data root; mm_url and mm_exclude_patterns are the
        # owner's to give (an organisation's values, never defaults)
        defaults(cfg, note, data={"sources.mm_archive": "archive", "mm_token_file": "token.txt"})

    def task_checks(self, cfg):
        if not cfg.mm_sync:
            return []
        from rawlayer.capture.config import ConfigError
        from .fetch import credential_path
        if not cfg.mm_url:
            return ["mm_sync requires mm_url"]
        try:
            if not credential_path(cfg.mm_token_file).is_file():
                return ["mm_sync requires a token file"]
        except (ValueError, ConfigError):
            return ["mm_sync requires a protected token path"]
        return []
