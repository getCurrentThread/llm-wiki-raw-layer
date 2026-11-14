# -*- coding: utf-8 -*-
"""The kakatok port that makes the app self-sufficient (2026-10): the deterministic crypto vectors of
kakaotalk/keychain.py and kakaotalk/keystore.py, the keystore derivation setup provisions against a
synthetic appstate.dat wrapping the keys of synthetic SQLCipher-4 rooms (kakaotalk/tests/edbfix.py). Nothing
here touches the real client or its files: every input is built in code under the fixture's temp directory."""
import contextlib
import io
import json
import shutil
import unittest


from kakaotalk import keychain, keystore, provision
from kakaotalk.readers.edb import load_keystore
from kakaotalk.tests import edbfix as E
from kakaotalk.tests.fixtures import Fixture
from kakaotalk.tests.helpers import _force, capture, records

ROOT = bytes(range(0x40, 0x60))
SALT = bytes(range(32))


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


class Base(unittest.TestCase):
    def setUp(self):
        self.fx = Fixture()

    def tearDown(self):
        shutil.rmtree(self.fx.base, onerror=_force)


class TestVectors(unittest.TestCase):
    def test_keychain_vectors(self):
        """RFC 5869/3394 vectors, the unwrap chain round-trip and the relpath-verbatim contract."""
        # RFC 5869 Test Case 1 (SHA-256)
        okm = keychain.hkdf_sha256(b"\x0b" * 22, bytes.fromhex("000102030405060708090a0b0c"),
                          bytes.fromhex("f0f1f2f3f4f5f6f7f8f9"), 42)
        assert okm == bytes.fromhex(
            "3cb25f25faacd57a90434f64d0362f2a"
            "2d2d0a90cf1a5a4c5db02d56ecc4c5bf"
            "34007208d5b887185865"), "HKDF RFC5869 TC1"

        # RFC 3394 4.3 — 256-bit KEK, 128-bit key data
        kek = bytes.fromhex("000102030405060708090A0B0C0D0E0F101112131415161718191A1B1C1D1E1F")
        kd16 = bytes.fromhex("00112233445566778899AABBCCDDEEFF")
        assert keychain.aes_kw_wrap(kek, kd16) == bytes.fromhex(
            "64E8C3F9CE0F5BA2"
            "63E9777905818A2A"
            "93C8191E7D6E8AE7"), "KW wrap RFC3394 4.3"
        assert keychain.aes_kw_unwrap(kek, keychain.aes_kw_wrap(kek, kd16)) == kd16

        # RFC 3394 4.6 — 256-bit KEK, 256-bit key data (카톡 DEK 와 같은 32B→40B)
        kd32 = bytes.fromhex("00112233445566778899AABBCCDDEEFF000102030405060708090A0B0C0D0E0F")
        assert keychain.aes_kw_wrap(kek, kd32) == bytes.fromhex(
            "28C9F404C4B810F4"
            "CBCCB35CFB87F826"
            "3F5786E2D80ED326"
            "CBC7F0E71A99F43B"
            "FB988B9B7A02DD21"), "KW wrap RFC3394 4.6"
        assert keychain.aes_kw_unwrap(kek, keychain.aes_kw_wrap(kek, kd32)) == kd32

        # 카톡 체인 모형: 합성 store 에 대해 root→KEK→wrap→unwrap 왕복
        store = {"version": "v2:", "salt": bytes(range(32)),
                 "map": {r"chat_data\chatLogs_9.edb": keychain.aes_kw_wrap(
                     keychain.hkdf_sha256(b"\x11" * 32, bytes(range(32)), b"v2:chat_data\\chatLogs_9.edb", 32),
                     b"\xab" * 32)}}
        assert keychain.derive_dek(b"\x11" * 32, store, r"chat_data\chatLogs_9.edb") == b"\xab" * 32
        # relpath 를 정규화(/ 치환)한 KEK 로는 원본 wrapped 가 안 풀려야 한다 — 원문 그대로가 계약
        wrong_kek = keychain.hkdf_sha256(b"\x11" * 32, bytes(range(32)),
                                b"v2:chat_data/chatLogs_9.edb", 32)
        with self.assertRaises(ValueError):
            keychain.aes_kw_unwrap(wrong_kek, store["map"][r"chat_data\chatLogs_9.edb"])
        # 잘못된 루트 → 전멸
        assert keychain.unwrap_store(b"\x22" * 32, store) == {}

    def test_keystore_vectors(self):
        """RFC 8949 decode vectors and the kakatok uint8-array byte coercion."""
        # RFC 8949 예시들
        assert keystore._cbor_decode(bytes.fromhex("00")) == 0
        assert keystore._cbor_decode(bytes.fromhex("1818")) == 24
        assert keystore._cbor_decode(bytes.fromhex("190100")) == 256
        assert keystore._cbor_decode(bytes.fromhex("20")) == -1
        assert keystore._cbor_decode(bytes.fromhex("6161")) == "a"
        assert keystore._cbor_decode(bytes.fromhex("83010203")) == [1, 2, 3]
        assert keystore._cbor_decode(bytes.fromhex("a26161016162820203")) == {"a": 1, "b": [2, 3]}
        assert keystore._cbor_decode(bytes.fromhex("4401020304")) == b"\x01\x02\x03\x04"
        # 무한길이 배열
        assert keystore._cbor_decode(bytes.fromhex("9f018202039f0405ffff")) == [1, [2, 3], [4, 5]]
        # 카톡식: 바이트열을 uint8 배열로 — info_prefix "v2:" = [0x76,0x32,0x3a]
        # 실제 구조 흉내: {info_prefix:[118,50,58], salt:[1,2]}
        synth = bytes.fromhex("a2")  # map(2)
        synth += bytes([0x6b]) + b"info_prefix" + bytes([0x83, 0x18, 118, 0x18, 50, 0x18, 58])
        synth += bytes([0x64]) + b"salt" + bytes([0x82, 0x01, 0x02])
        d = keystore._cbor_decode(synth)
        assert keystore._to_bytes(d["info_prefix"]) == b"v2:", keystore._to_bytes(d["info_prefix"])
        assert keystore._to_bytes(d["salt"]) == b"\x01\x02"
        # chatId 추출
        assert keystore.chatid_of_relpath("chat_data\\chatLogs_12345.edb") == "12345"
        assert keystore.chatid_of_relpath("Contacts\\mpi_v2.edb") is None


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
                keystore.load_store(str(self.fx.user_dir / "appstate.dat")), str(self.fx.user_dir), scan=scan)

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
        inputs = InputSet(Config(self.fx.config_path))
        keys, _all, ok_ids = load_keystore(inputs.keystore)
        self.assertEqual(ok_ids, {"31", "32"})
        self.assertEqual(keys["31"], E.key_for(31))
        self.assertEqual(capture(self.fx, ["edb"]), 0)
        recs = {r["key"] for r in records(self.fx)}
        self.assertIn("kakao:31:msg:1", recs)
        self.assertIn("kakao:32:msg:3", recs)

    def test_rotated_root_is_an_attention(self):
        (self.keys_dir / "root_v2.hex").write_text((b"\x22" * 32).hex())
        self.assertTrue(self.run_derive())                                     # the rotation attention line
        self.assertEqual(json.loads(self.fx.keystore.read_text(encoding="utf-8")), {})
