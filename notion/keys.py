import datetime
import uuid


def nt_id(value):
    if not isinstance(value, str):
        return None
    try:
        return str(uuid.UUID(value))
    except ValueError:
        return None


def iso_ms(value):
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
        return int(dt.timestamp() * 1000) if dt.tzinfo is not None else None
    except (ValueError, OverflowError, OSError):
        return None
