# -*- coding: utf-8 -*-
from ...search import dense
from ..base import BaseCommand


class Command(BaseCommand):
    help = "embed the app's index into its dense sidecar for search --hybrid (local Ollama + sqlite-vec, llm.md 12)"
    args = "[--model M] [--dims D] [--url U] [--limit N] [--dry-run] [--rebuild] [--json] [--config CFG]"
    app = "required"
    config = True

    def run(self, app, argv):
        return dense.main([app] + argv)
