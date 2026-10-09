# -*- coding: utf-8 -*-
"""The kakaotalk app's tests: capture of the Kakao origins (M1, M3, M4, Q10, hardening, resilience), the
kakaotalk search index, and setup's provisioning (root, keystore, backup, the macOS database). Importing the
package selects the app, so every test module of the suite runs with it selected."""
from rawlayer import registry

registry.select("kakaotalk")
