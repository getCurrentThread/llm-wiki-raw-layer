import os
from rawlayer.capture.origins import Origin
from .readers import archive


class GmArchive(Origin):
    name = "gm_archive"
    kinds = frozenset({"msg", "lbls", "label"})
    day_kinds = frozenset({"msg"})

    def read(self, ctx):
        if ctx.cfg.gm_sync and ctx.inputs.name == "live" and os.environ.get("RAWLAYER_ARCHIVE_READY") != "1":
            from .collector import sync_config
            from tools.archiveio import CollectorError
            from rawlayer.capture.precheck import check
            check(ctx.inputs.gm_archive)
            if sync_config(ctx.cfg).get("errors"):
                raise CollectorError("collection_failed")
        return archive.containers(ctx.inputs.gm_archive, self.name)

    def estimate_peak(self, inputs):
        return sum(p.stat().st_size for folder in ("exports", "state")
                   for p in (inputs.gm_archive / folder).glob("*.json") if p.is_file())


ORIGINS = (GmArchive,)
