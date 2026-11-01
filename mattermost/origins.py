# -*- coding: utf-8 -*-
"""The Mattermost origin: the mattertok-format archive, read in one stable pass and diffed as a whole (multi-version).
With mm_sync, a live read runs the app's fetch step first."""

from appkit.archive import ArchiveOrigin
from rawlayer.capture.origins import MultiVersionMixin

from .readers import archive


class MmArchive(MultiVersionMixin, ArchiveOrigin):
    name = "mm_archive"
    kinds = frozenset({"ch", "post", "file", "user", "blob"})
    day_kinds = frozenset({"post"})

    def read_once(self, ctx):
        self.fetch(ctx)
        patterns = [p.casefold() for p in ctx.cfg.mm_exclude]
        return archive.read_once(ctx.inputs.mm_archive, patterns, ctx.denylist, self.name)


ORIGINS = (MmArchive,)
