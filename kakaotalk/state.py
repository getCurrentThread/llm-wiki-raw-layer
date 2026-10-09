# -*- coding: utf-8 -*-
"""The KakaoTalk tables of state.sqlite (folded from committed records, so replay rebuilds them):

  schema_base  the column list of each room-DB table's first schema record (SPEC 8.4 drift is judged against it)
  edb_salt     the salt fingerprint of each edb room (a header that fails under a new salt is 'rekeyed')"""
import json

SCHEMA_BASE = ("schema_base", "CREATE TABLE IF NOT EXISTS schema_base(origin TEXT NOT NULL, key TEXT NOT NULL,\n"
                              "  cols TEXT NOT NULL, PRIMARY KEY(origin, key)) WITHOUT ROWID;")
EDB_SALT = ("edb_salt", "CREATE TABLE IF NOT EXISTS edb_salt(chat_id TEXT PRIMARY KEY, salt_fp TEXT) WITHOUT ROWID;")


def apply_schema_base(cur, rec):
    if rec["kind"] == "schema" and rec["rev"] == 1:
        cols = [c[0] for c in rec["native"]["columns"]]
        cur.execute("INSERT OR REPLACE INTO schema_base(origin,key,cols) VALUES(?,?,?)",
                    (rec["origin"], rec["key"], json.dumps(cols)))


def apply_edb_salt(cur, rec):
    if rec["kind"] == "room":
        cur.execute("INSERT OR REPLACE INTO edb_salt(chat_id,salt_fp) VALUES(?,?)",
                    (rec["native"]["chatId"], rec["native"]["salt_fp"]))


def schema_base(con, origin, key):
    r = con.execute("SELECT cols FROM schema_base WHERE origin=? AND key=?", (origin, key)).fetchone()
    return json.loads(r[0]) if r else None


def edb_salt(con, chat_id):
    r = con.execute("SELECT salt_fp FROM edb_salt WHERE chat_id=?", (chat_id,)).fetchone()
    return r[0] if r else None


def base_for(ctx, origin):
    """schema-key -> base column list of `origin`, cached for the run (ctx.scratch)."""
    cache = ctx.scratch.setdefault("kakao.schema_base", {})

    def f(skey):
        k = (origin, skey)
        if k not in cache:
            cache[k] = schema_base(ctx.state, origin, skey)
        return cache[k]
    return f
