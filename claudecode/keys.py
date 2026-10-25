import datetime as dt
from rawlayer.capture.canon import pk_text


def cc_id(value):
    return pk_text(value) if isinstance(value, str) else None


def iso_ms(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return None
        # timedelta arithmetic: float timestamp() * 1000 can land 1 ms low
        return (parsed - dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)) // dt.timedelta(milliseconds=1)
    except (ValueError, OverflowError, OSError):
        return None
