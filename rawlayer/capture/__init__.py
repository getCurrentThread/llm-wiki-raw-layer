# -*- coding: utf-8 -*-
"""capture: the append-only raw ledger (SPEC 3-13). run.capture reads every selected origin of the installed apps,
diffs it against state.sqlite and commits one manifest; replay and export-state read the ledger back."""
