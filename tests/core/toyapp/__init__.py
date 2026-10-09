# -*- coding: utf-8 -*-
"""A synthetic app with one origin per core rule, for the framework's own tests.

  toy_a  plain Origin: a key that disappears is 'vanished' (suspect); a state table of its own (toy_last);
         estimate_peak and requires_system hooks
  toy_b  PendingAbsentMixin: two-phase absent behind a (small) shrink guard; containers 'clean' or 'partial'
  toy_m  MultiVersionMixin: one stable pass, versions judged together

Sources live under config "sources" -> "toy_dir" (see readers.py for the file shapes)."""
