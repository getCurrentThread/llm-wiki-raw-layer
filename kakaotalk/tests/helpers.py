# -*- coding: utf-8 -*-
"""Helpers of the kakaotalk app tests: the app's origins and the shared app-test preamble
(tests/core/appfix.py: one capture run, the ledger read back, the ledger invariants and the replay
comparison)."""
from tests.core import appfix

STATIC = ["merged_out", "backup_decrypted", "decrypted_live"]
KAKAO = STATIC + ["live_out", "edb"]

preserved_env = appfix.preserved_env
records = appfix.records
by_key = appfix.by_key
manifests = appfix.manifests
export = appfix.export
invariants = appfix.invariants
replay_diff = appfix.replay_diff
_force = appfix._force


def capture(fx, origins=STATIC):
    return appfix.capture(fx, origins)
