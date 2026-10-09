# -*- coding: utf-8 -*-
"""The kakatok port that makes the app self-sufficient (2026-10): the deterministic crypto vectors of
kakaotalk/keychain.py and kakaotalk/keystore_v2.py, the keystore derivation setup provisions against a
synthetic appstate.dat wrapping the keys of synthetic SQLCipher-4 rooms (kakaotalk/tests/edbfix.py), and the
backup decryption against a synthetic EvaSQLite-encrypted backup user directory. Nothing here touches the
real client, its files or the registry: read_deviceinfo and get_userid_live are patched, and every input is
built in code under the fixture's temp directory. Run from the project root, like every app suite:

    python -s -B -m unittest discover -s kakaotalk/tests -t .
"""
import contextlib
import io
import json
import shutil
import sqlite3
import unittest
from unittest import mock

from Crypto.Cipher import AES

from kakaotalk import backup as backup_mod
from kakaotalk import katok_decrypt as kd
from kakaotalk import keychain, keystore_v2, provision
from kakaotalk.readers.edb import load_keystore
from kakaotalk.tests import edbfix as E
from kakaotalk.tests.fixtures import Fixture
from kakaotalk.tests.helpers import _force, capture, manifests, records

ROOT = bytes(range(0x40, 0x60))
SALT = bytes(range(32))
DEV = {"dev_id": "dev-1", "sys_uuid": "uuid-1", "hdd_model": "model-x", "hdd_serial": "ser-9", "dev_id_type": ""}
UID = "424242"


@contextlib.contextmanager
def quiet():
    """Keep the commands' progress and refusal prints out of the unittest output (helpers.capture's pattern)."""
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        yield


# ----------------------------------------------------------------- test-only builders
def _cbor_head(major, n):
    if n < 24:
        return bytes([major | n])
    if n < 256:
        return bytes([major | 24, n])
    if n < 65536:
        return bytes([major | 25]) + n.to_bytes(2, "big")
    return bytes([major | 26]) + n.to_bytes(4, "big")


def _cbor_enc(v):
    """The subset load_store reads: dicts, arrays, byte/text strings, small ints."""
    if isinstance(v, dict):
        return _cbor_head(0xA0, len(v)) + b"".join(_cbor_enc(k) + _cbor_enc(x) for k, x in v.items())
    if isinstance(v, (list, tuple)):
        return _cbor_head(0x80, len(v)) + b"".join(_cbor_enc(x) for x in v)
    if isinstance(v, bytes):
        return _cbor_head(0x40, len(v)) + v
    if isinstance(v, str):
        b = v.encode("latin1")
        return _cbor_head(0x60, len(b)) + b
    if isinstance(v, int) and 0 <= v < 256:
        return bytes([v]) if v < 24 else bytes([0x18, v])
    raise TypeError(type(v))


def wrap_dek(relpath, dek):
    """The client-side wrap of one room key: AES-KW under HKDF(root, salt, info='v2:'+relpath verbatim)."""
    return keychain.aes_kw_wrap(
        keychain.hkdf_sha256(ROOT, SALT, ("v2:" + relpath).encode("latin1"), 32), dek)


def write_appstate(udir, wrapped):
    store = {"info_prefix": b"v2:", "salt": SALT,
             "wrapped_dek_map": [[rel, w] for rel, w in wrapped.items()]}
    (udir / "appstate.dat").write_bytes(_cbor_enc(store))


def eva_encrypt(data, dbtype, pragma, userid):
    """Inverse of katok_decrypt.eva_decrypt: page-wise AES-CBC under the fixed EvaSQLite key/iv."""
    key, iv = kd.eva_key_iv(kd.eva_passphrase(dbtype, pragma, userid))
    out = bytearray()
    for pos in range(0, len(data), kd.PAGE):
        blk = data[pos:pos + kd.PAGE]
        n = len(blk) - (len(blk) % 16)
        if n:
            out += AES.new(key, AES.MODE_CBC, iv).encrypt(blk[:n])
        out += blk[n:]
    return bytes(out)


def make_backup_room(bak, chat, rows):
    plain = bak.parent / "plain"
    plain.mkdir(parents=True, exist_ok=True)
    src = plain / f"{chat}.sqlite"
    E.plain_backup_room(src, rows)
    (bak / "chat_data" / f"chatLogs_{chat}.edb").write_bytes(
        eva_encrypt(src.read_bytes(), 3, kd.make_pragma(DEV), UID))
    src.unlink()


class Base(unittest.TestCase):
    def setUp(self):
        self.fx = Fixture()

    def tearDown(self):
        shutil.rmtree(self.fx.base, onerror=_force)


class TestVectors(unittest.TestCase):
    def test_keychain_selftest(self):
        """RFC 5869/3394 vectors, the unwrap chain round-trip and the relpath-verbatim contract."""
        self.assertTrue(keychain._selftest())

    def test_keystore_v2_selftest(self):
        """RFC 8949 decode vectors and the kakatok uint8-array byte coercion."""
        self.assertTrue(keystore_v2._selftest())


class TestKeystoreDerivation(Base):
    """the keystore derivation (setup's provisioning, the old keys command): root unwrap of a synthetic
    appstate.dat -> the keystore the edb origin captures with."""

    def setUp(self):
        super().setUp()
        for chat in (31, 32):
            E.place_room(self.fx, chat, [E.msg(chat, i) for i in (1, 2, 3)], E.key_for(chat))
        wrapped = {f"chat_data\\chatLogs_{c}.edb": wrap_dek(f"chat_data\\chatLogs_{c}.edb", E.key_for(c))
                   for c in (31, 32)}
        wrapped["chat_data\\ocii_v2.edb"] = wrap_dek("chat_data\\ocii_v2.edb", b"\x01" * 32)   # non-room entry
        write_appstate(self.fx.user_dir, wrapped)
        self.keys_dir = self.fx.data_root / "keys"
        self.keys_dir.mkdir(parents=True, exist_ok=True)
        (self.keys_dir / "root_v2.hex").write_text(ROOT.hex())

    def run_derive(self, scan=False):
        """write_keystore_for as provision runs it: the fixture's config dict, store and installed root."""
        cfg = {"data_root": str(self.fx.data_root),
               "sources": {"kakao_user_dir": str(self.fx.user_dir), "keystore": str(self.fx.keystore)}}
        with quiet():
            return provision.write_keystore_for(
                cfg, lambda m: None, keychain.read_root_hex(str(self.keys_dir / "root_v2.hex")),
                keystore_v2.load_store(str(self.fx.user_dir / "appstate.dat")), str(self.fx.user_dir), scan=scan)

    def test_derives_keystore_and_capture_reads(self):
        from rawlayer.capture.config import Config, InputSet
        self.assertEqual(self.run_derive(), [])
        d = json.loads(self.fx.keystore.read_text(encoding="utf-8"))
        self.assertEqual(sorted(d), ["chat_data\\chatLogs_31.edb", "chat_data\\chatLogs_32.edb",
                                     "chat_data\\ocii_v2.edb"])
        for chat in (31, 32):
            self.assertEqual(d[f"chat_data\\chatLogs_{chat}.edb"]["key_hex"], E.key_for(chat).hex())
            self.assertIs(d[f"chat_data\\chatLogs_{chat}.edb"]["header_ok"], True)
        self.assertEqual(d["chat_data\\ocii_v2.edb"]["chatId"], None)          # carried, ignored by load_keystore
        inputs = InputSet(Config(self.fx.config_path), "live")
        keys, _all, ok_ids = load_keystore(inputs.keystore)
        self.assertEqual(ok_ids, {"31", "32"})
        self.assertEqual(keys["31"], E.key_for(31))
        self.assertTrue((self.keys_dir / "rotation_state.json").exists())      # rotation baseline kept
        self.assertEqual(capture(self.fx, ["edb"]), 0)
        recs = {r["key"] for r in records(self.fx)}
        self.assertIn("kakao:31:msg:1", recs)
        self.assertIn("kakao:32:msg:3", recs)

    def test_room_without_a_store_entry_stays_no_key(self):
        """A room on disk the store does not know: no bogus entry is written, so capture reads the keyed rooms
        and leaves that one no_key (the container fact; test_resilience covers the all-unkeyed suspect case)."""
        from rawlayer.capture import canon
        self.fx.user_dir.joinpath("chat_data", "chatLogs_33.edb").write_bytes(b"not a room")
        self.assertEqual(self.run_derive(), [])
        self.assertNotIn("chat_data\\chatLogs_33.edb",
                         json.loads(self.fx.keystore.read_text(encoding="utf-8")))
        self.assertEqual(capture(self.fx, ["edb"]), 0)
        m = manifests(self.fx)[-1]
        self.assertEqual(m["origins"]["edb"]["containers"][canon.source_id("chat_data/chatLogs_33.edb")]["status"],
                         "no_key")
        self.assertNotIn("kakao:33:msg:1", {r["key"] for r in records(self.fx)})

    def test_rotated_root_is_an_attention(self):
        (self.keys_dir / "root_v2.hex").write_text((b"\x22" * 32).hex())
        self.assertTrue(self.run_derive())                                     # the rotation attention line
        self.assertEqual(json.loads(self.fx.keystore.read_text(encoding="utf-8")), {})


class TestBackupProvision(Base):
    """the backup decryption (setup's provisioning, the old backup command): EvaSQLite decryption of a
    synthetic *_backup_* user directory -> backup_decrypted."""

    def setUp(self):
        super().setUp()
        self.bak = self.fx.inputs / "kakao_user_backup_20261006"
        (self.bak / "chat_data").mkdir(parents=True)
        self.fx.backup.joinpath("999.sqlite").write_bytes(b"stale from an earlier run")

    def run_backup(self, dev=DEV):
        cfg = {"sources": {"kakao_user_dir": str(self.fx.user_dir), "backup_decrypted": str(self.fx.backup)}}
        with mock.patch.object(kd, "read_deviceinfo", return_value=dev), \
                mock.patch.object(kd, "get_userid_live", return_value=UID), quiet():
            return backup_mod.decrypt_backup(cfg, lambda m: None)

    def test_decrypts_rooms_and_manifest(self):
        make_backup_room(self.bak, 41, [E.msg(41, 1), E.msg(41, 2)])
        self.assertEqual(self.run_backup(), 0)
        room = self.fx.backup / "41.sqlite"
        self.assertTrue(room.exists())
        con = sqlite3.connect(room)
        self.assertEqual(con.execute("SELECT COUNT(*) FROM chatLogs").fetchone()[0], 2)
        con.close()
        man = json.loads(self.fx.backup.joinpath("_manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(man["decrypted"],
                         [{"chatId": "41", "rows": 2, "from": "chatLogs_41.edb", "to": "41.sqlite"}])
        self.assertFalse(self.fx.backup.joinpath("999.sqlite").exists())       # room gone from the backup
        self.assertEqual(capture(self.fx, ["backup_decrypted"]), 0)
        recs = {r["key"]: r for r in records(self.fx)}
        self.assertIn("kakao:41:msg:1", recs)
        self.assertEqual(recs["kakao:41"]["derived"]["rows"], 2)

    def test_wrong_pragma_removes_stale_and_is_suspect(self):
        make_backup_room(self.bak, 41, [E.msg(41, 1)])
        self.assertEqual(self.run_backup(), 0)
        self.assertTrue(self.fx.backup.joinpath("41.sqlite").exists())
        rc = self.run_backup(dict(DEV, hdd_serial="other-disk"))              # a different machine's pragma
        self.assertEqual(rc, 3)
        self.assertFalse(self.fx.backup.joinpath("41.sqlite").exists())       # the stale copy did not survive
        man = json.loads(self.fx.backup.joinpath("_manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(man["decrypted"], [])

    def test_no_backup_directory_skips(self):
        shutil.rmtree(self.bak)
        self.assertEqual(self.run_backup(), 2)
