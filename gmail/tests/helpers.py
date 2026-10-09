from tests.core import appfix
records = appfix.records
by_key = appfix.by_key
manifests = appfix.manifests
invariants = appfix.invariants
replay_diff = appfix.replay_diff
run_rawsearch = appfix.run_rawsearch
force_rmtree = appfix.force_rmtree


def capture(fx):
    return appfix.capture(fx, ["gm_archive"])
