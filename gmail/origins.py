from tools.archiveio import sync_first
from rawlayer.capture.origins import Origin
from .readers import archive


class GmArchive(Origin):
    name = "gm_archive"
    kinds = frozenset({"msg", "lbls", "label"})
    day_kinds = frozenset({"msg"})

    def read(self, ctx):
        sync_first(ctx, ctx.cfg.gm_sync, ctx.inputs.gm_archive, "gmail")
        return archive.containers(ctx.inputs.gm_archive, self.name)

    def estimate_peak(self, inputs):
        return sum(p.stat().st_size for folder in ("exports", "state")
                   for p in (inputs.gm_archive / folder).glob("*.json") if p.is_file())


ORIGINS = (GmArchive,)
