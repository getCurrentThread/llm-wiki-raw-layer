# -*- coding: utf-8 -*-
"""rawlayer -- the raw layer framework: an append-only ledger of everything a project's apps read (capture) and
per-app full-text indexes over it (search).

The framework is source-agnostic. Each source is an app (a folder <label>/ next to manage.py, found by discovery;
the project's settings are config/settings.py) with the same modules -- apps.py, origins.py, inputs.py, vocab.py
and optionally search.py -- see rawlayer/registry.py. "SPEC n" / "Qn" in the docstrings refer to llm-wiki's SPEC.md and
SPEC-CLARIFICATIONS.md, the contract the ledger format was written under."""
