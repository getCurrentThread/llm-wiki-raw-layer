from tools.archiveio import ArchiveOrigin
from .readers import archive


class GmArchive(ArchiveOrigin):
    name = "gm_archive"
    kinds = frozenset({"msg", "lbls", "label"})
    day_kinds = frozenset({"msg"})
    peak = (("exports", "*.json"), ("state", "*.json"))

    def read(self, ctx):
        self.fetch(ctx)
        return archive.containers(ctx.inputs.gm_archive, self.name)


ORIGINS = (GmArchive,)
