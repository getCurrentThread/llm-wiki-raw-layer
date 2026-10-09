# -*- coding: utf-8 -*-
"""The macOS support (the kakaocli port, 2026-10): the key derivation's pinned vectors and properties, the
plist userId strategies, the pure-python SQLCipher reader (full database + WAL round-trip on synthetic
SQLCipher-4 geometry, and 3/4 auto-detection), the macdb origin on a synthetic NT-schema database (message
keys and projection hashes shared with the other origins), and the macdb decryption setup provisions (the
device lookups bypassed). The SQLCipher round-trips need the sqlite3.dll of a Windows Python for the 80-byte page
reserve (kakaotalk/tests/edbfix.py) and skip elsewhere, like the edb tests; everything else runs everywhere.
Nothing here touches a real Mac, its plists or ioreg.

    python -s -B -m unittest discover -s kakaotalk/tests -t .
"""
import contextlib
import hashlib
import io
import json
import os
import plistlib
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from Crypto.Cipher import AES

from kakaotalk import kakaocli, provision
from kakaotalk.tests import edbfix as E
from kakaotalk.tests.fixtures import Fixture, T0
from kakaotalk.tests.helpers import _force, capture, manifests, records

UUID = "01234567-89ab-cdef-fedc-ba9876543210"
UID = 424242
# Pinned vectors of the port (a change in the string arithmetic changes these: guard of the Swift parity)
DB_NAME = "10193a4510167b6d5226b279b3e597c93cba0a026be261c48afcdbd6bec2009604ef77348110e2"
KEY_HEAD = "2e547ef147b3eefb9b2dc21b7510a8122162303ccd3845894921c148bd9134c1"

NT_DDL = """
CREATE TABLE NTChatMessage(logId INTEGER PRIMARY KEY, chatId INTEGER, authorId INTEGER, message TEXT,
  type INTEGER, sentAt INTEGER, attachment BLOB);
CREATE TABLE NTChatRoom(chatId INTEGER PRIMARY KEY, type INTEGER, chatName TEXT, activeMembersCount INTEGER,
  lastLogId INTEGER, lastUpdatedAt INTEGER, directChatMemberUserId INTEGER, linkId INTEGER);
CREATE TABLE NTChatMeta(chatId INTEGER, type INTEGER, content TEXT, PRIMARY KEY(chatId, type));
CREATE TABLE NTUser(userId INTEGER PRIMARY KEY, linkId INTEGER, displayName TEXT, friendNickName TEXT,
  nickName TEXT);
CREATE TABLE NTOpenLink(linkId INTEGER PRIMARY KEY, linkName TEXT);
"""


@contextlib.contextmanager
def quiet():
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        yield


class Base(unittest.TestCase):
    def setUp(self):
        self.fx = Fixture()

    def tearDown(self):
        shutil.rmtree(self.fx.base, onerror=_force)

    def set_mac(self, path):
        cfg = json.loads(Path(self.fx.config_path).read_text(encoding="utf-8"))
        cfg["mac_db"] = str(path)
        Path(self.fx.config_path).write_text(json.dumps(cfg), encoding="utf-8")


def make_macdb(path, msgs, rooms=(), users=(), meta=(), links=()):
    """A decrypted macOS database: the NT schema with made-up rows."""
    con = sqlite3.connect(path)
    try:
        con.executescript(NT_DDL)
        con.executemany("INSERT INTO NTChatMessage VALUES (?,?,?,?,?,?,?)", msgs)
        con.executemany("INSERT INTO NTChatRoom VALUES (?,?,?,?,?,?,?,?)",
                        [r if len(r) == 8 else r + (None,) * (8 - len(r)) for r in rooms])
        con.executemany("INSERT INTO NTChatMeta VALUES (?,?,?)", meta)
        con.executemany("INSERT INTO NTUser VALUES (?,?,?,?,?)", users)
        con.executemany("INSERT INTO NTOpenLink VALUES (?,?)", links)
        con.commit()
    finally:
        con.close()


def nt_msg(chat, lid, author=7, text=None, typ=1, send=None):
    return (lid, chat, author, text if text is not None else f"m{chat}-{lid}", typ,
            send if send is not None else T0 + lid * 60, None)


def sid(path):
    from rawlayer.capture import canon
    return canon.source_id(os.path.basename(str(path)))


class TestDerivation(unittest.TestCase):
    def test_pinned_vectors(self):
        """The port's derivation is pinned: these change if any string step drifts from the Swift original."""
        self.assertEqual(kakaocli.database_name(UID, UUID), DB_NAME)
        self.assertEqual(kakaocli.secure_key(UID, UUID)[:64], KEY_HEAD)
        self.assertEqual(len(kakaocli.secure_key(UID, UUID)), 256)
        self.assertEqual(kakaocli.hashed_device_uuid(UUID),
                         "tnouDovTnftjDguBiMkP1IqMwcW4lZn/cNHGi1R5V7weaNsTf1NmSUehTkQJqbzAMrEGwQ==")

    def test_selftest(self):
        self.assertTrue(kakaocli._selftest())

    def test_discover_prefers_the_derived_name(self):
        tmp = Path(tempfile.mkdtemp(prefix="llmw-cont-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        (tmp / ("0" * 78)).write_bytes(b"x")
        (tmp / DB_NAME).write_bytes(b"x")
        self.assertEqual(os.path.basename(kakaocli.discover_database_file(str(tmp), UID, UUID)), DB_NAME)
        self.assertEqual(os.path.basename(kakaocli.discover_database_file(str(tmp))), "0" * 78)
        self.assertIsNone(kakaocli.discover_database_file(str(tmp / "gone")))


class TestUserId(unittest.TestCase):
    """The plist strategies, on synthetic preference files under a fake home."""

    def home(self, mapping, name="com.kakao.KakaoTalkMac.ABCDEF.plist"):
        tmp = Path(tempfile.mkdtemp(prefix="llmw-home-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        pref = tmp / "Library" / "Containers" / "com.kakao.KakaoTalkMac" / "Data" / "Library" / "Preferences"
        pref.mkdir(parents=True)
        with (pref / name).open("wb") as fh:
            plistlib.dump(mapping, fh)
        return str(tmp)

    def test_strategy_1_transparency_suffix(self):
        self.assertEqual(kakaocli.user_id(self.home(
            {"FSChatWindowTransparency77": 1, "FSChatWindowTransparencyA77": 1, "other": 2})), 77)

    def test_strategy_2_direct_key(self):
        self.assertEqual(kakaocli.user_id(self.home({"userId": 123})), 123)
        self.assertEqual(kakaocli.user_id(self.home({"user_id": "456"})), 456)

    def test_strategy_3_candidates_then_bruteforce(self):
        h = hashlib.sha512(b"555").hexdigest()
        home = self.home({"DESIGNATEDFRIENDSREVISION:" + h: 3, "AlertKakaoIDsList": [9, 555]})
        self.assertEqual(kakaocli.user_id(home), 555)                     # verified candidate, no scan
        home = self.home({"DESIGNATEDFRIENDSREVISION:" + h: 3})           # no candidates: bounded scan
        self.assertEqual(kakaocli.user_id(home, brute_limit=1000), 555)
        home = self.home({"DESIGNATEDFRIENDSREVISION:" + kakaocli.EMPTY_SHA512: 3})   # empty account: skipped
        self.assertIsNone(kakaocli.user_id(home, brute_limit=10))
        self.assertEqual(kakaocli.recover_user_id_from_sha512(hashlib.sha512(b"42").hexdigest(), 100), 42)
        self.assertIsNone(kakaocli.recover_user_id_from_sha512(hashlib.sha512(b"42").hexdigest(), 10))

    def test_strategy_4_window_frame_suffix(self):
        self.assertEqual(kakaocli.user_id(self.home(
            {"NSWindow Frame FSChatWindowFrame_88": "1 2 3 4",
             "NSWindow Frame FSChatWindowFrame_A88": "5 6 7 8"})), 88)

    def test_no_user_id(self):
        self.assertIsNone(kakaocli.user_id(self.home({"unrelated": 1})))


@unittest.skipIf(os.name != "nt", "synthetic SQLCipher databases need the sqlite3.dll of a Windows Python")
class TestCipher(unittest.TestCase):
    """decrypt_db on synthetic SQLCipher databases (edbfix builds the 80-reserve plaintext geometry)."""

    PASS = kakaocli.secure_key(UID, UUID)

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="llmw-cip-"))

    def tearDown(self):
        shutil.rmtree(self.tmp, onerror=_force)

    def _key(self, salt):
        return hashlib.pbkdf2_hmac("sha512", self.PASS.encode(), salt, 256000, 32)

    def _open_image(self, image):
        p = self.tmp / "dec.sqlite"
        p.write_bytes(image)
        con = sqlite3.connect(p)
        rows = con.execute("SELECT logId, message FROM chatLogs ORDER BY logId").fetchall()
        con.close()
        return rows

    def test_v4_roundtrip(self):
        work = self.tmp / "room.sqlite"
        E.PlainRoom(work, [(1, 51, 1, 7, "w1", None, T0 + 60, 0, 0, 0, 0)]).close()
        salt = hashlib.sha256(b"mac-salt").digest()[:16]
        main = work.read_bytes()
        image, facts = kakaocli.decrypt_db(E.enc_main(self._key(salt), salt, main), b"", self.PASS)
        self.assertEqual(facts["cipher"], "sqlcipher4")
        self.assertEqual(facts["pages"], len(main) // 4096)
        self.assertEqual(facts["wal"]["state"], "none")
        self.assertEqual(self._open_image(image), [(1, "w1")])

    def test_v4_wal_roundtrip(self):
        work = self.tmp / "room.sqlite"
        pr = E.PlainRoom(work, [(1, 51, 1, 7, "w1", None, T0 + 60, 0, 0, 0, 0)])
        pr.add([(2, 51, 1, 7, "w2-in-wal", None, T0 + 120, 0, 0, 0, 0)])
        main, wal = pr.snapshot()
        pr.close()
        salt = hashlib.sha256(b"mac-salt").digest()[:16]
        key = self._key(salt)
        image, facts = kakaocli.decrypt_db(E.enc_main(key, salt, main), E.enc_wal(key, salt, wal), self.PASS)
        self.assertEqual((facts["cipher"], facts["wal"]["state"], facts["wal"]["frames_applied"]),
                         ("sqlcipher4", "ok", facts["wal"]["frames_total"]))
        self.assertEqual(self._open_image(image), [(1, "w1"), (2, "w2-in-wal")])

    def test_wrong_passphrase_is_detected(self):
        work = self.tmp / "room.sqlite"
        E.PlainRoom(work, [(1, 51, 1, 7, "w1", None, T0 + 60, 0, 0, 0, 0)]).close()
        salt = hashlib.sha256(b"mac-salt").digest()[:16]
        image, facts = kakaocli.decrypt_db(E.enc_main(self._key(salt), salt, work.read_bytes()), b"",
                                           kakaocli.secure_key(1, UUID))
        self.assertIsNone(image)
        self.assertIsNone(facts["cipher"])

    def test_v3_page_detection(self):
        """compatibility-3 geometry (1024/48/SHA1): page-level detection under the v3 default parameters."""
        c = kakaocli.CIPHERS[1]
        key = hashlib.pbkdf2_hmac(c["hash"], b"p", b"K" * 16, c["iters"], 32)
        body = bytearray(c["page"] - 16 - c["reserve"])
        body[0:2] = (1024).to_bytes(2, "big")
        body[2], body[3] = 1, 1
        body[4] = c["reserve"]
        body[5:8] = b"\x40\x20\x20"
        iv = bytes(range(16))
        page = b"K" * 16 + AES.new(key, AES.MODE_CBC, iv).encrypt(bytes(body)) + iv + bytes(c["reserve"] - 16)
        image, facts = kakaocli.decrypt_db(page, b"", "p")
        self.assertEqual(facts["cipher"], "sqlcipher3")
        self.assertEqual(image[16:24], bytes(body[0:8]))


class TestMacdbOrigin(Base):
    def test_reads_messages_schema_rooms(self):
        mac = self.fx.inputs / "mac" / "nt.sqlite"
        mac.parent.mkdir(parents=True, exist_ok=True)
        make_macdb(mac, [nt_msg(51, i) for i in (1, 2, 3)],
                   rooms=[(51, 1, None, 3, 3, T0 + 180, 7, 0)],
                   meta=[(51, 3, "스터디방")], users=[(7, 0, "홍길동", None, "닉네임")])
        self.set_mac(mac)
        self.assertEqual(capture(self.fx, ["macdb"]), 0)
        recs = {r["key"]: r for r in records(self.fx)}
        for i in (1, 2, 3):
            self.assertIn(f"kakao:51:msg:{i}", recs)
        self.assertEqual(recs["kakao:51:msg:1"]["native"]["message"], "m51-1")
        self.assertEqual(recs["kakao:51:msg:1"]["derived"], {"sender": "홍길동"})
        self.assertEqual(recs["kakao:51"]["derived"], {"title": "스터디방"})      # NTChatMeta type 3
        self.assertIn("kakao:*@NTChatMessage", recs)
        m = manifests(self.fx)[-1]
        self.assertEqual(m["origins"]["macdb"]["containers"][sid(mac)]["tables"]["NTChatMessage"], 3)

    def test_placeholder_without_nt_tables_is_not_mac(self):
        mac = self.fx.inputs / "mac" / "empty.sqlite"
        mac.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(mac)                    # a valid database that is not a KakaoTalk one
        con.execute("CREATE TABLE t(a)")
        con.commit()
        con.close()
        self.set_mac(mac)
        self.assertEqual(capture(self.fx, ["macdb"]), 0)
        m = manifests(self.fx)[-1]
        self.assertEqual(m["origins"]["macdb"]["notes"].get("not_mac"), 1)
        self.assertEqual(records(self.fx), [])

    def test_search_indexes_macdb_text_names_titles(self):
        """index + search over a macdb-only ledger: the row carries the text, the NTUser sender name and the
        NTChatMeta room title (kakao-3 stamps)."""
        from kakaotalk.tests import ledgerfix as L
        mac = self.fx.inputs / "mac" / "nt.sqlite"
        mac.parent.mkdir(parents=True, exist_ok=True)
        make_macdb(mac, [nt_msg(53, 1, text="macos 회의 노트", send=T0 + 60)],
                   rooms=[(53, 1, None, 1, 1, T0 + 60, None, 0)],
                   meta=[(53, 3, "맥방")], users=[(7, 0, "맥유저", None, "닉네임")])
        self.set_mac(mac)
        self.assertEqual(L.capture(self.fx, ["macdb"]), 0)
        rc, _out, err = L.run_rawsearch(["index", "--config", str(self.fx.config_path),
                                         "--source", "kakaotalk", "--json"])
        self.assertEqual(rc, 0, err)
        rc, out, err = L.run_rawsearch(["kakaotalk", "--config", str(self.fx.config_path), "--json", "macos"])
        self.assertEqual(rc, 0, err)
        env = json.loads(out)
        self.assertEqual(len(env["rows"]), 1)
        row = env["rows"][0]
        self.assertEqual((row["text"], row["sender"], row["room"], row["source"], row["origins"]),
                         ("macos 회의 노트", "맥유저", "맥방", "macdb", ["macdb"]))

    def test_unset_option_reads_nothing(self):
        self.assertEqual(capture(self.fx, ["macdb"]), 0)
        m = manifests(self.fx)[-1]
        self.assertEqual(m["origins"]["macdb"]["containers"], {})
        self.assertEqual(records(self.fx), [])


@unittest.skipIf(os.name != "nt", "synthetic SQLCipher databases need the sqlite3.dll of a Windows Python")
class TestMacdbCommand(Base):
    def setUp(self):
        super().setUp()
        self.mac = self.fx.inputs / "mac" / "nt.sqlite"
        self.mac.parent.mkdir(parents=True, exist_ok=True)
        self.enc = self.fx.inputs / "mac" / "enc.db"

    def _build_encrypted(self, uid=UID, uuid=UUID):
        plain = self.fx.inputs / "mac" / "plain.sqlite"
        E.reserve_db(plain)
        con = sqlite3.connect(plain)
        try:
            con.executescript(NT_DDL)
            con.executemany("INSERT INTO NTChatMessage VALUES (?,?,?,?,?,?,?)", [nt_msg(61, i) for i in (1, 2)])
            con.execute("INSERT INTO NTChatRoom VALUES (?,?,?,?,?,?,?,?)", (61, 1, "그룹", 2, 2, T0 + 120, 7, 0))
            con.commit()
        finally:
            con.close()
        salt = hashlib.sha256(b"mac-salt").digest()[:16]
        key = hashlib.pbkdf2_hmac("sha512", kakaocli.secure_key(uid, uuid).encode(), salt, 256000, 32)
        self.enc.write_bytes(E.enc_main(key, salt, plain.read_bytes()))
        plain.unlink()

    def run_macdb(self, *args, **kw):
        """The old macdb command as setup's provisioning runs it (the device lookups bypassed)."""
        cfg = json.loads(Path(self.fx.config_path).read_text(encoding="utf-8"))
        with quiet():
            return provision.run_macdb(cfg, lambda m: None, *args, **kw)

    def test_decrypts_and_capture_reads(self):
        self._build_encrypted()
        self.set_mac(self.mac)
        rc = self.run_macdb(uuid=UUID, user_id=UID, db=str(self.enc))
        self.assertEqual(rc, 0)
        self.assertTrue(self.mac.exists())
        self.assertEqual(capture(self.fx, ["macdb"]), 0)
        recs = {r["key"]: r for r in records(self.fx)}
        self.assertIn("kakao:61:msg:1", recs)
        self.assertEqual(recs["kakao:61"]["derived"], {"title": "그룹"})

    def test_wrong_uuid_does_not_open(self):
        self._build_encrypted()
        self.set_mac(self.mac)
        rc = self.run_macdb(uuid="99999999-9999-9999-9999-999999999999", user_id=UID, db=str(self.enc))
        self.assertEqual(rc, 3)
        self.assertFalse(self.mac.exists())

    def test_unset_option_refused(self):
        self._build_encrypted()
        rc = self.run_macdb(uuid=UUID, user_id=UID, db=str(self.enc))
        self.assertEqual(rc, 2)
