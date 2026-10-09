# -*- coding: utf-8 -*-
"""The mattermost app's tests: capture of mm_archive (M2, hardening) and the mattermost search index.
Importing the package selects the app (rawlayer/registry.py), so every test module of the suite runs
with it selected."""
from rawlayer import registry

registry.select("mattermost")
