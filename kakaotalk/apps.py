# -*- coding: utf-8 -*-
import os

from rawlayer.registry import AppConfig, Option


def _mac_db(v):
    """The macOS client's decrypted database (kakaocli port): unset (null) on machines without macOS inputs,
    an absolute path when set — a top-level option, not a source, so Windows-only configs stay valid without a
    placeholder. The file itself need not exist yet: the macdb command writes it (its parent must)."""
    if v is None:
        return None
    if not isinstance(v, str) or not os.path.isabs(v) or not os.path.isdir(os.path.dirname(v)):
        raise ValueError("mac_db")
    return v


class KakaoConfig(AppConfig):
    label = "kakaotalk"
    # config.json "sources": absolute paths that must exist
    source_keys = ("backup_decrypted", "kakao_user_dir", "keystore")
    options = (Option("mac_db", None, _mac_db),)
    # The scheduled task `manage.py schedule install kakaotalk` registers: `manage.py collect kakaotalk --config
    # <CFG>` as SYSTEM, daily from `start` (local time) and every `every_hours` hours after it, at most `limit_hours`
    # hours per run. 01:14, 04:14, ... stay clear of kb-corpus-refresh (:44), kb-curate (05:40, 17:40), kb-brief
    # (06:00) and mattermost-capture (:14 of the other hours).
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
        # the inputs no command can read anywhere else: the root key, the keystore, the decrypted backup and
        # (on a Mac) the client database
        from . import provision  # noqa: PLC0415
        return provision.provision(cfg, note, restart_client, scan=scan, root_file=root)
