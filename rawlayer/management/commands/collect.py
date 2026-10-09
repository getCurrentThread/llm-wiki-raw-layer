# -*- coding: utf-8 -*-
from ... import collect
from ..base import BaseCommand


class Command(BaseCommand):
    help = "the capture+index pipeline: the scheduled task's body and a manual run (logged in task.log)"
    args = "[--input-set live|T1|T2] [--origins a,b] [--rebuild-index] [--config CFG]"
    app = "required"
    config = True

    def run(self, app, argv):
        return collect.main([app] + argv)
