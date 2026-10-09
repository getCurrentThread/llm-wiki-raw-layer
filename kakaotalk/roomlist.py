# -*- coding: utf-8 -*-
"""Room titles and sender names from the client's own databases (chatListInfo.edb and TalkUserDB.edb,
decrypted): the way the client labels rooms and people on Windows. The macOS reader names rooms the client's
way (chatName -> the NTChatMeta type-3 title -> the open-link name -> the direct partner, kakaocli's rule); the
Windows client keeps the effective display title materialised, so the title chain here is:

  1. chatRoomList.chatRoomTitle -- the title the client shows (every observed row holds one; a custom name
     where the room has one, the members' names where it has not -- for a 1:1 room the peer's name);
  2. the chatMetas type-3 content -- the room-title meta, the NTChatMeta rule (sparse: rooms whose list row
     carries no title);
  3. chatRoomList.titleDisplayMembers -- the member names the client displays for unnamed rooms.

Volatile columns (lastChatMessage, lastUpdatedAt, ...) are never read: a room record changes only when the
title itself changes. Everything is defensive against schema drift across client versions -- a missing table
or column yields no titles for that step, never an error."""
from __future__ import annotations

import sqlite3


def _has_table(con, table):
    return con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None


def _cols(con, table):
    return {r[1] for r in con.execute(f'PRAGMA table_info("{table}")')}


def _clean(v):
    return v.strip() if isinstance(v, str) and v.strip() else None


def chat_titles(con) -> dict:
    """{chatId(str): title} from a decrypted chatListInfo database (the chain in the module docstring)."""
    out: dict[str, str] = {}
    if _has_table(con, "chatRoomList"):
        cols = _cols(con, "chatRoomList")
        if "chatId" in cols and "chatRoomTitle" in cols:
            for cid, title in con.execute("SELECT chatId, chatRoomTitle FROM chatRoomList"):
                t = _clean(title)
                if t is not None and cid is not None:
                    out[str(cid)] = t
    if _has_table(con, "chatMetas"):
        if {"chatId", "type", "content"} <= _cols(con, "chatMetas"):
            for cid, content in con.execute("SELECT chatId, content FROM chatMetas WHERE type=3"):
                t = _clean(content)
                if t is not None and cid is not None:
                    out.setdefault(str(cid), t)
    if _has_table(con, "chatRoomList"):
        cols = _cols(con, "chatRoomList")
        if "chatId" in cols and "titleDisplayMembers" in cols:
            for cid, members in con.execute("SELECT chatId, titleDisplayMembers FROM chatRoomList"):
                t = _clean(members)
                if t is not None and cid is not None:
                    out.setdefault(str(cid), t)
    return out


def user_names(con) -> dict:
    """{str(userId): name} from a decrypted TalkUserDB: nickName -- the account's display name, the join the
    kakatok research documented as the sender reconstruction -- with friendNickName (the owner's own alias
    for a friend) only where the account has no nickname. Defensive like chat_titles."""
    try:
        if not _has_table(con, "talkUser"):
            return {}
        cols = _cols(con, "talkUser")
    except sqlite3.Error:
        return {}
    if "userId" not in cols:
        return {}
    picks = [c for c in ("nickName", "friendNickName") if c in cols]
    if not picks:
        return {}
    out: dict[str, str] = {}
    try:
        for row in con.execute('SELECT "userId", ' + ", ".join(f'"{c}"' for c in picks) + ' FROM "talkUser"'):
            for v in row[1:]:
                if isinstance(v, str) and v.strip():
                    out[str(row[0])] = v.strip()
                    break
    except sqlite3.Error:
        return {}
    return out
