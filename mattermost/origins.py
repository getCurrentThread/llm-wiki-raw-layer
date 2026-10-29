# -*- coding: utf-8 -*-
"""The Mattermost origin: the mattertok-format archive, read in one stable pass and diffed as a whole (multi-version).
With mm_sync, a live read runs the app's collector first."""
import os

from rawlayer.capture.origins import MultiVersionMixin, Origin

from .readers import archive


class MmArchive(MultiVersionMixin, Origin):
    name = "mm_archive"
    kinds = frozenset({"ch", "post", "file", "user", "blob"})
    day_kinds = frozenset({"post"})

    def read_once(self, ctx):
        if ctx.cfg.mm_sync and ctx.inputs.name == "live" and os.environ.get("RAWLAYER_ARCHIVE_READY") != "1":
            from .collector import sync_config
            from tools.archiveio import CollectorError
            from rawlayer.capture.precheck import check
            check(ctx.inputs.mm_archive)
            if sync_config(ctx.cfg).get("errors"):
                raise CollectorError("collection_incomplete")
        patterns = [p.casefold() for p in ctx.cfg.mm_exclude]
        return archive.read_once(ctx.inputs.mm_archive, patterns, ctx.denylist, self.name)


ORIGINS = (MmArchive,)
