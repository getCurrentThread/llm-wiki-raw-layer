# -*- coding: utf-8 -*-
from ... import schedule
from ..base import BaseCommand


class Command(BaseCommand):
    help = "the apps' scheduled tasks (collect as SYSTEM, through schtasks.exe): install | status | run | remove"
    args = ("install [<app>] [--config CFG] [--python EXE] [--run-now | --dry-run] | status [<app>] [--config CFG] "
            "| run <app> | remove <app>")

    def run(self, app, argv):
        return schedule.main(argv)
