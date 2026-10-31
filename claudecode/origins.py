from tools.archiveio import sync_first
from rawlayer.capture.origins import MultiVersionMixin, Origin
from .readers import archive


class CcArchive(MultiVersionMixin, Origin):
    name = "cc_archive"
    kinds = day_kinds = frozenset({"msg"})

    def read_once(self, ctx):
        sync_first(ctx, ctx.cfg.cc_sync, ctx.inputs.cc_archive, "claudecode")
        return archive.read_once(ctx.inputs.cc_archive, ctx.denylist, self.name)

    def estimate_peak(self, inputs):
        return sum(p.stat().st_size for p in inputs.cc_archive.rglob("*.json") if p.is_file())


ORIGINS = (CcArchive,)
