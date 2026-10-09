"""Strip temporary file signatures while preserving document content."""
from urllib.parse import urlsplit, urlunsplit
from ..keys import nt_id


def project(value, field="", media=False):
    media = media or field in ("image", "file", "pdf", "video", "audio", "icon", "cover", "files")
    if isinstance(value, list):
        return [project(v, field, media) for v in value]
    if isinstance(value, dict):
        # Formula and rollup are computed, not authored content.
        return {k: project(v, k, media if k not in ("rich_text", "caption", "text", "link") else False)
                for k, v in value.items()
                if not (k == "expiry_time" and media)
                and not (k in ("formula", "rollup") and value.get("type") == k
                         and isinstance(v, dict) and "type" in v)}
    if isinstance(value, str):
        if field in ("id", "page_id", "block_id", "database_id", "data_source_id", "workspace_id"):
            return nt_id(value) or value
        if media and field == "url" and value.startswith(("https://", "http://")):
            url = urlsplit(value)
            return urlunsplit((url.scheme, url.netloc, url.path, "", ""))
    return value
