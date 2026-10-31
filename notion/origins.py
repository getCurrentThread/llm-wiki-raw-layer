from tools.archiveio import sync_first
from rawlayer.capture.origins import Origin
from .readers import archive


class NtArchive(Origin):
    name = "nt_archive"
    kinds = frozenset({"page", "block", "comment", "ds", "user"})
    day_kinds = frozenset({"block"})

    def read(self, ctx):
        sync_first(ctx, ctx.cfg.nt_sync, ctx.inputs.nt_archive, "notion")
        return archive.containers(ctx.inputs.nt_archive, self.name)

    def estimate_peak(self, inputs):
        return sum(p.stat().st_size for p in inputs.nt_archive.rglob("*.json") if p.is_file())


ORIGINS = (NtArchive,)
