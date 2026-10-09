# -*- coding: utf-8 -*-
"""rawlayer.conf.user_home: the home of the user a project belongs to (settings.HOME_DIR, and with it DATA_PARENT)
is the user profile folder that holds the project. Path.home() is only the answer when no profile does, and never
in a service account's process: the SYSTEM task would put DATA_PARENT in the systemprofile, not where
`schedule install` checked the config. The profiles are synthesised under the temp folder."""
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from rawlayer import conf
from rawlayer.capture import fsio


class TestUserHome(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp(prefix="llmw-home-")).resolve()
        self.addCleanup(shutil.rmtree, self.base, True)
        self.outer = self.base / "profiles"
        self.inner = self.outer / "u1"
        self.inner.mkdir(parents=True)

    def profiles(self, *homes):
        return mock.patch.object(conf, "_profiles", lambda with_sid=False: iter(homes))

    def service(self, on):
        return mock.patch.object(conf, "_service_account", return_value=on)

    def test_the_innermost_profile_that_holds_the_project(self):
        for on in (False, True):            # a profile that holds the project answers in any process
            with self.subTest(service_account=on), self.profiles(self.outer, self.inner), self.service(on):
                self.assertEqual(conf.user_home(self.inner / "proj"), self.inner)
                self.assertEqual(conf.user_home(self.outer / "proj"), self.outer)

    def test_no_profile_holds_the_project(self):
        elsewhere = self.base / "srv" / "proj"
        with self.profiles(self.inner), self.service(False):
            self.assertEqual(conf.user_home(elsewhere), Path.home())
        with self.profiles(self.inner), self.service(True), self.assertRaises(conf.HomeError):
            conf.user_home(elsewhere)

    def test_service_account_is_the_process_token(self):
        if os.name != "nt":
            self.assertFalse(conf._service_account())
            return
        for sid, want in (("S-1-5-18", True), ("S-1-5-19", True), ("S-1-5-20", True),
                          ("S-1-5-21-1-2-3-1001", False), (None, False)):
            with self.subTest(sid=sid), mock.patch.object(fsio, "token_user_sid", return_value=sid):
                self.assertIs(conf._service_account(), want)


if __name__ == "__main__":
    unittest.main()
