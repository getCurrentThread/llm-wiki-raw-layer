# -*- coding: utf-8 -*-
from ...search import steps
from ..base import BaseCommand


class Command(BaseCommand):
    help = "search the app's index (manage.py search <app> --help for the query language and options)"
    args = "[QUERY] [options] [--config CFG]"
    app = "required"
    config = True

    def run(self, app, argv):
        return steps.main(["search", app] + argv)
