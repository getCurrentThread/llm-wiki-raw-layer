# -*- coding: utf-8 -*-
"""Full-text search over the raw ledger, one FTS5 index per app (<data_root>/index/<label>.sqlite).

An app's search.py is a source module: SPEC (a common.SourceIndex; common.SimpleIndex for the usual shape), HELP,
add_arguments(parser) and run(args) (delegated to a search.SearchDriver). The indexing side (common: catch-up by
manifest seq, rebuild, lock) and the tokenizer and query compiler (textindex) are shared, so indexing and querying
cannot drift apart."""
