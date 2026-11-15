# -*- coding: utf-8 -*-
"""Where the KakaoTalk sources are: config.json "sources"."""
from pathlib import Path


def resolve(cfg):
    s = cfg.sources
    return {"backup_decrypted": Path(s["backup_decrypted"]), "keystore": Path(s["keystore"]),
            "edb_dir": Path(s["kakao_user_dir"]) / "chat_data"}
