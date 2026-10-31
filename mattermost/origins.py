# -*- coding: utf-8 -*-
"""The Mattermost origin: the mattertok-format archive, read in one stable pass and diffed as a whole (multi-version).
With mm_sync, a live read runs the app's collector first."""

from tools.archiveio import sync_first
from rawlayer.capture.origins import MultiVersionMixin, Origin

from .readers import archive


class MmArchive(MultiVersionMixin, Origin):
    name = "mm_archive"
    kinds = frozenset({"ch", "post", "file", "user", "blob"})
    day_kinds = frozenset({"post"})

    def read_once(self, ctx):
        sync_first(ctx, ctx.cfg.mm_sync, ctx.inputs.mm_archive, "mattermost")
        patterns = [p.casefold() for p in ctx.cfg.mm_exclude]
        return archive.read_once(ctx.inputs.mm_archive, patterns, ctx.denylist, self.name)


ORIGINS = (MmArchive,)
