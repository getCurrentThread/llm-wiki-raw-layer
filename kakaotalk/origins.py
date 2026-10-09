# -*- coding: utf-8 -*-
"""The KakaoTalk origins, in ledger order. Every origin keys messages the same way (kakao:<chatId>:msg:<logId>),
so a message seen by several of them can be matched across origins."""
from pathlib import Path

from rawlayer.capture import fsio
from rawlayer.capture.origins import Origin, PendingAbsentMixin

from . import state as kstate
from .readers import out


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


class MacDb(KakaoOrigin):
    """The macOS client's database (kakaocli port): the single decrypted NT-schema SQLite the macdb command
    writes to the mac_db option path. The option is unset on machines without macOS inputs, so there the
    origin reads nothing (no containers, nothing in the ledger)."""

    name = "macdb"

    def read(self, ctx):
        p = getattr(ctx.inputs, "mac_db", None)
        if p is None or not Path(p).is_file():
            return []
        from .readers import macdb  # noqa: PLC0415
        return macdb.macdb_containers(ctx)

    def estimate_peak(self, inputs):
        p = getattr(inputs, "mac_db", None)
        return fsio.size(p) if p is not None else 0


ORIGINS = (BackupDecrypted, Edb, MacDb)
