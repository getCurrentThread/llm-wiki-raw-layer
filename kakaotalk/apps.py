# -*- coding: utf-8 -*-
from rawlayer.registry import AppConfig


class KakaoConfig(AppConfig):
    label = "kakaotalk"
    # config.json "sources": absolute paths that must exist
    source_keys = ("backup_decrypted", "kakao_user_dir", "keystore")
    wiki = (None, "chat")       # `manage.py collect`: a chat layout, no config keys (settings: the command line)
    # The scheduled task `manage.py schedule install kakaotalk` registers: `manage.py capture kakaotalk --config
    # <CFG>` as SYSTEM, daily from `start` (local time) and every `every_hours` hours after it, at most `limit_hours`
    # hours per run. Start minutes are staggered across the apps: README.md, "Scheduled tasks".
    schedule = {
        "task": r"\llm-wiki\kakaotalk-capture",
        "start": "01:14",
        "every_hours": 3,
        "limit_hours": 2,
        "description": "llm-wiki-raw-layer kakaotalk: capture the KakaoTalk inputs into the raw ledger every 3 h, "
                       "then update the search index",
    }

    def setup_defaults(self, cfg, note):
        # the placeholders this app resolves itself: the client's user directory and its own outputs under
        # the data root
        from . import provision  # noqa: PLC0415
        provision.fill_defaults(cfg, note=note)

    def setup_provision(self, cfg, note, restart_client=False, scan=False, root=None):
        # the inputs no command can read anywhere else: the root key, the keystore, the decrypted backup
        from . import provision  # noqa: PLC0415
        return provision.provision(cfg, note, restart_client, scan=scan, root_file=root)
