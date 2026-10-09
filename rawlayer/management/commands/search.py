# -*- coding: utf-8 -*-
from ...search import cli
from ..base import BaseCommand


class Command(BaseCommand):
    help = "search the app's index (manage.py search <app> --help for the query language and options)"
    args = "[QUERY] [options] [--config CFG]"
    app = "required"
    config = True

    def run(self, app, argv):
        return cli.main(["search", app] + argv)
