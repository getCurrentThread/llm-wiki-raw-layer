# -*- coding: utf-8 -*-
"""python -s -B manage.py <command> ... -- the project's single entry point (rawlayer/management)."""
import sys

from rawlayer.management import main

if __name__ == "__main__":
    sys.exit(main())
