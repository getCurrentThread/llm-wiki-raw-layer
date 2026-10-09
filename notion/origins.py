from rawlayer.capture.origins import Origin
import os
from .readers import archive


class NtArchive(Origin):
    name = "nt_archive"
    kinds = frozenset({"page", "block", "comment", "ds", "user"})
    day_kinds = frozenset({"block"})

    def read(self, ctx):
        if ctx.cfg.nt_sync and ctx.inputs.name == "live" and os.environ.get("RAWLAYER_ARCHIVE_READY") != "1":
            from .collector import sync_config
            from tools.archiveio import CollectorError
            from rawlayer.capture.precheck import check
            check(ctx.inputs.nt_archive)
            if sync_config(ctx.cfg).get("errors"):
                raise CollectorError("collection_incomplete")
        return archive.containers(ctx.inputs.nt_archive, self.name)

    def estimate_peak(self, inputs):
        return sum(p.stat().st_size for p in inputs.nt_archive.rglob("*.json") if p.is_file())


ORIGINS = (NtArchive,)
