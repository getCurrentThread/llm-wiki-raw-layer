from rawlayer.registry import AppConfig, Option
from appkit.setup import defaults
from appkit.archive import boolean, text, identifiers, nonnegative

EXCLUDED = ["SPAM", "TRASH", "DRAFT", "CATEGORY_PROMOTIONS", "CATEGORY_SOCIAL", "CATEGORY_UPDATES"]


def body_bytes(value):
    # archive.nonnegative caps integers at 100000; the body default is 128 KiB.
    if type(value) is not int or not 0 <= value <= 16 * 1024 * 1024:
        raise ValueError("body byte limit")
    return value


class GmailConfig(AppConfig):
    label = "gmail"
    archive = "gm"
    options = (Option("gm_sync", False, boolean), Option("gm_client_file", "", text),
               Option("gm_token_file", "", text), Option("gm_exclude_labels", EXCLUDED, identifiers),
               Option("gm_volatile_labels", ["UNREAD"], identifiers),
               Option("gm_body_max_bytes", 131072, body_bytes),
               Option("gm_partition_max_messages", 1000, nonnegative))
    wiki = ("gm", "mail")
    schedule = {"task": r"\llm-wiki\gmail-capture", "start": "00:44",
                "every_hours": 3, "limit_hours": 1,
                "description": "Capture Gmail archive and update its search index"}

    def setup_defaults(self, cfg, note):
        # the archive, the downloaded Desktop client JSON and the token --auth writes, all in the data root
        defaults(cfg, note, data={"sources.gm_archive": "archive", "gm_client_file": "client.json",
                                  "gm_token_file": "token.json"})

    def task_checks(self, cfg):
        if not cfg.gm_sync:
            return []
        from .auth import credential_path
        try:
            for value in (cfg.gm_client_file, cfg.gm_token_file):
                if not credential_path(value).is_file():
                    return ["gm_sync requires client and token files"]
        except ValueError:
            return ["gm_sync requires protected client and token paths"]
        return []
