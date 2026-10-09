# -*- coding: utf-8 -*-
"""Mattermost value rules (SPEC 6)."""


def mm_ts_ms(create_at):
    if isinstance(create_at, bool) or not isinstance(create_at, int) or create_at <= 0:
        return None
    return create_at
