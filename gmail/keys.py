import re

# Last millisecond whose KST day is representable by the core's calendar.
MAX_MS = 253402268399999


def gm_id(value):
    return value if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{1,16}", value) else None
