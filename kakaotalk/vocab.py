# -*- coding: utf-8 -*-
"""Strings the KakaoTalk readers put in manifests (origin names, kinds and statuses come from origins.py)."""

WORDS = (
    # room-DB and kakatok tables (container facts "tables")
    "chatLogs", "chatLogHistory", "chatLogMeta", "chatLog_attachment", "chatLogDrafts", "chatLogOutdated",
    "chatLogTagMapping", "chatLogTagRevision", "chatLogMetaPendingRestoreRanges", "messages", "rooms", "cursors",
    # macOS client tables (macdb, the kakaocli port)
    "NTChatMessage", "NTChatRoom", "NTChatMeta", "NTUser", "NTOpenLink",
    # edb WAL facts
    "frames_total", "frames_valid", "frames_applied", "commits", "zero_filled", "bad_header",
    # notes
    "ts_out_of_range", "pk_bad", "no_pk", "missing_table", "not_room", "count_mismatch", "quick_check",
    "gate_header", "main_not_page_multiple", "sidecar_left", "room_gone", "not_mac",
)

PATTERNS = (
    r"^kakao:[0-9]+(:[a-z]+(:-?[0-9]+)+)?$",                                     # key (manifest pending lists)
    r"^kakao:[0-9]+@(chatLogs|chatLogHistory|chatLogMeta|chatLog_attachment)$",  # room-DB schema key (Q10)
    r"^t_[0-9a-f]{8}$",                                                          # hashed table name
)
