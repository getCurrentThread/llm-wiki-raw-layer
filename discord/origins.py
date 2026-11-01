from appkit.archive import ArchiveOrigin
from rawlayer.capture.origins import MultiVersionMixin
from .readers import archive


class DcArchive(MultiVersionMixin, ArchiveOrigin):
    name = "dc_archive"
    kinds = frozenset({"guild", "ch", "msg", "user"})
    day_kinds = frozenset({"msg"})
    peak = (("exports", "**/*.json"),)

    def read_once(self, ctx):
        self.fetch(ctx)
        return archive.read_once(ctx.inputs.dc_archive, [p.casefold() for p in ctx.cfg.dc_exclude],
                                 ctx.denylist, self.name)


ORIGINS = (DcArchive,)
