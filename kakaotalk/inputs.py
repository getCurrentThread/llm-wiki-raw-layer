# -*- coding: utf-8 -*-
"""Where the KakaoTalk sources are for an input set: `live` reads config.json "sources"; a frozen set reads its
copy of the client's chat_data under <inputs_root>/<set> (the static room DBs are frozen in place, by hash)."""
import os
from pathlib import Path


def resolve(cfg, input_set, frozen_root):
    s = cfg.sources
    out = {"backup_decrypted": Path(s["backup_decrypted"]), "keystore": Path(s["keystore"]),
           # the static root of keystore (static_roots below): the folder of config.json's own string, as
           # always -- os.path.dirname keeps a trailing "." part that Path(...).parent would drop, and Config
           # accepts such a value
           "keystore_dir": Path(os.path.dirname(s["keystore"]))}
    # the mac_db option (the macOS client's decrypted database): set only on machines with macOS inputs; the
    # decrypted file is static at its configured path for live and frozen sets alike
    mac_db = getattr(cfg, "mac_db", None)
    if mac_db is not None:
        out["mac_db"] = Path(mac_db)
        out["mac_db_dir"] = Path(os.path.dirname(mac_db))
    if frozen_root is None:
        out["edb_dir"] = Path(s["kakao_user_dir"]) / "chat_data"
    else:
        out["edb_dir"] = Path(frozen_root) / "kakao_user" / "chat_data"
    return out


def static_roots(inputs):
    """freeze.json static label -> (root, closed): a closed root may not gain files after the freeze."""
    roots = {"backup_decrypted": (inputs.backup_decrypted, True),
             "keystore": (inputs.keystore_dir, False)}
    if getattr(inputs, "mac_db_dir", None) is not None:
        roots["macdb"] = (inputs.mac_db_dir, False)
    return roots
