# -*- coding: utf-8 -*-
"""kakaotalk's capture side: where its sources are (resolve), the strings its
readers put in manifests (WORDS, PATTERNS) and ORIGINS; the readers are in readers/.

Where the KakaoTalk sources are: config.json "sources".

Strings the KakaoTalk readers put in manifests (origin names, kinds and statuses come from origins.py).

The KakaoTalk origins, in ledger order. Every origin keys messages the same way (kakao:<chatId>:msg:<logId>),
so a message seen by several of them can be matched across origins."""
from pathlib import Path
from rawlayer.capture import fsio
from rawlayer.capture.origins import Origin, PendingAbsentMixin
from . import state as kstate
from .readers import out


def resolve(cfg):
    s = cfg.sources
    return {"backup_decrypted": Path(s["backup_decrypted"]), "keystore": Path(s["keystore"]),
            "edb_dir": Path(s["kakao_user_dir"]) / "chat_data"}


WORDS = (
    # room-DB tables (container facts "tables")
    "chatLogs", "chatLogHistory", "chatLogMeta", "chatLog_attachment", "chatLogDrafts", "chatLogOutdated",
    "chatLogTagMapping", "chatLogTagRevision", "chatLogMetaPendingRestoreRanges",
    # edb WAL facts
    "frames_total", "frames_valid", "frames_applied", "commits", "zero_filled", "bad_header",
    # notes
    "ts_out_of_range", "pk_bad", "no_pk", "missing_table", "not_room", "count_mismatch", "quick_check",
    "gate_header", "main_not_page_multiple", "sidecar_left", "room_gone", "titled", "named",
)

PATTERNS = (
    r"^kakao:[0-9]+(:[a-z]+(:-?[0-9]+)+)?$",                                     # key (manifest pending lists)
    r"^kakao:[0-9]+@(chatLogs|chatLogHistory|chatLogMeta|chatLog_attachment)$",  # room-DB schema key (Q10)
    r"^t_[0-9a-f]{8}$",                                                          # hashed table name
)


class KakaoOrigin(Origin):
    kinds = frozenset({"schema", "msg", "room", "hist", "meta", "attachment"})
    day_kinds = frozenset({"msg"})
    state_tables = (kstate.SCHEMA_BASE,)

    def apply_put(self, cur, rec):
        kstate.apply_schema_base(cur, rec)


class BackupDecrypted(KakaoOrigin):
    """Decrypted talk-backup room DBs, <chatId>.sqlite (static), titled and named by the backup's own
    client databases through the manifest and the sibling TalkUserDB.sqlite."""
    name = "backup_decrypted"

    def read(self, ctx):
        return out.backup_containers(ctx)


class Edb(PendingAbsentMixin, KakaoOrigin):
    """The PC client's encrypted room DBs, chat_data/chatLogs_<chatId>.edb (SPEC 10.3, 10.4): a room that does not
    read completely is routine (no key yet, rekeyed, mid-write), so missing keys go through pending -> absent
    behind the shrink guard. The client's own chat-list and user databases beside the rooms title the rooms
    and name the senders."""
    name = "edb"
    statuses = PendingAbsentMixin.statuses | {"rekeyed", "no_key", "unreadable"}
    fail_statuses = frozenset({"unstable", "rekeyed", "no_key", "unreadable"})
    fail_default = "unreadable"
    state_tables = KakaoOrigin.state_tables + (kstate.EDB_SALT,)

    def read(self, ctx):
        from .readers import edb  # noqa: PLC0415 -- pycryptodome is needed only when edb is read
        return edb.edb_containers(ctx)

    def apply_put(self, cur, rec):
        super().apply_put(cur, rec)
        kstate.apply_edb_salt(cur, rec)

    def estimate_peak(self, inputs):
        # two copies of the largest room (main + WAL): the image and its plaintext temp file
        room = 0
        d = Path(inputs.edb_dir)
        if d.is_dir():
            for p in d.iterdir():
                if p.name.startswith("chatLogs_") and p.name.endswith(".edb"):
                    room = max(room, fsio.size(p) + fsio.size(p.with_name(p.name + "-wal")))
        return 2 * room


ORIGINS = (BackupDecrypted, Edb)
