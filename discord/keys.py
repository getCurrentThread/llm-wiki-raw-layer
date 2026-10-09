from rawlayer.capture.canon import pk_text


def dc_id(value):
    return pk_text(value) if isinstance(value, str) else None


def snowflake_ms(value):
    if not isinstance(value, str) or len(value) > 19 or not value.isascii() or not value.isdecimal():
        return None
    ident = int(value)
    return (ident >> 22) + 1420070400000 if 0 < ident < 2 ** 63 else None
