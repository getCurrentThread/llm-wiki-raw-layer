# -*- coding: utf-8 -*-
"""KakaoTalk value rules (SPEC 6): the message timestamp."""


def kakao_ts_ms(send_at):
    """(ts_ms, out_of_range). ts_ms is None unless 1e9 <= sendAt < 1e10 (integer)."""
    v = send_at
    if isinstance(v, bool) or not isinstance(v, int):
        return None, True
    if 1_000_000_000 <= v < 10_000_000_000:
        return v * 1000, False
    return None, True
