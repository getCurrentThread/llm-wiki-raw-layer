from tools.archiveio import ArchiveOrigin
from rawlayer.capture.origins import MultiVersionMixin
from .readers import archive


class CcArchive(MultiVersionMixin, ArchiveOrigin):
    name = "cc_archive"
    kinds = day_kinds = frozenset({"msg"})
    peak = (("", "**/*.json"),)

    def read_once(self, ctx):
        self.fetch(ctx)
        return archive.read_once(ctx.inputs.cc_archive, ctx.denylist, self.name)


ORIGINS = (CcArchive,)
