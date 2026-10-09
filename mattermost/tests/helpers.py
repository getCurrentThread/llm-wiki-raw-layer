# -*- coding: utf-8 -*-
"""Helpers of the mattermost app tests: the app's origins and the shared app-test preamble
(tests/core/appfix.py: one capture run, the ledger read back, the ledger invariants and the replay
comparison)."""
from tests.core import appfix

MM = ["mm_archive"]

preserved_env = appfix.preserved_env
records = appfix.records
by_key = appfix.by_key
manifests = appfix.manifests
export = appfix.export
invariants = appfix.invariants
replay_diff = appfix.replay_diff
_force = appfix._force


def capture(fx, origins=MM):
    return appfix.capture(fx, origins)
