# -*- coding: utf-8 -*-
"""Full-text search over the raw ledger, one FTS5 index per app (<data_root>/index/<label>.sqlite).

An app's search.py is a source module: SPEC (an index.SourceIndex; index.SimpleIndex for the usual shape), HELP,
add_arguments(parser) and run(args) (delegated to a driver.SearchDriver). The indexing side (index: catch-up by
manifest seq, rebuild, lock) and the tokenizer and query compiler (fts) are shared, so indexing and querying
cannot drift apart."""
