# -*- coding: utf-8 -*-
"""Where the KakaoTalk sources are: config.json "sources" (and the mac_db option)."""
from pathlib import Path


def resolve(cfg):
    s = cfg.sources
    out = {"backup_decrypted": Path(s["backup_decrypted"]), "keystore": Path(s["keystore"]),
           "edb_dir": Path(s["kakao_user_dir"]) / "chat_data"}
    # the mac_db option (the macOS client's decrypted database): set only on machines with macOS inputs
    mac_db = getattr(cfg, "mac_db", None)
    if mac_db is not None:
        out["mac_db"] = Path(mac_db)
    return out
