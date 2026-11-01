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
    options = (Option("gm_sync", False, boolean), Option("gm_imap_user", "", text),
               Option("gm_imap_password_file", "", text), Option("gm_exclude_labels", EXCLUDED, identifiers),
               Option("gm_volatile_labels", ["UNREAD"], identifiers),
               Option("gm_body_max_bytes", 131072, body_bytes),
               Option("gm_partition_max_messages", 1000, nonnegative))
    wiki = ("gm", "mail")
    schedule = {"task": r"\llm-wiki\gmail-capture", "start": "00:44",
                "every_hours": 3, "limit_hours": 1,
                "description": "Capture Gmail archive and update its search index"}

    def setup_defaults(self, cfg, note):
        # the archive and the app password file, both in the data root
        defaults(cfg, note, data={"sources.gm_archive": "archive", "gm_imap_password_file": "app_password.txt"})

    def task_checks(self, cfg):
        if not cfg.gm_sync:
            return []
        if not cfg.gm_imap_user:
            return ["gm_sync requires gm_imap_user"]
        from appkit.archive import credential_path
        try:
            if not credential_path(cfg.gm_imap_password_file).is_file():
                return ["gm_sync requires the app password file"]
        except ValueError:
            return ["gm_sync requires a protected app password path"]
        return []
