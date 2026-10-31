from tools.archiveio import sync_first
from rawlayer.capture.origins import MultiVersionMixin, Origin
from .readers import archive


class DcArchive(MultiVersionMixin, Origin):
    name = "dc_archive"
    kinds = frozenset({"guild", "ch", "msg", "user"})
    day_kinds = frozenset({"msg"})

    def read_once(self, ctx):
        sync_first(ctx, ctx.cfg.dc_sync, ctx.inputs.dc_archive, "discord")
        return archive.read_once(ctx.inputs.dc_archive, [p.casefold() for p in ctx.cfg.dc_exclude],
                                 ctx.denylist, self.name)

    def estimate_peak(self, inputs):
        return sum(p.stat().st_size for p in (inputs.dc_archive / "exports").rglob("*.json") if p.is_file())


ORIGINS = (DcArchive,)
