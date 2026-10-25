import os
from rawlayer.capture.origins import MultiVersionMixin, Origin
from .readers import archive


class CcArchive(MultiVersionMixin, Origin):
    name = "cc_archive"
    kinds = day_kinds = frozenset({"msg"})

    def read_once(self, ctx):
        if ctx.cfg.cc_sync and ctx.inputs.name == "live" and os.environ.get("RAWLAYER_ARCHIVE_READY") != "1":
            from .collector import sync_config
            from tools.archiveio import CollectorError
            from rawlayer.capture.precheck import check
            check(ctx.inputs.cc_archive)
            if sync_config(ctx.cfg).get("errors"):
                raise CollectorError("collection_incomplete")
        return archive.read_once(ctx.inputs.cc_archive, ctx.denylist, self.name)

    def estimate_peak(self, inputs):
        return sum(p.stat().st_size for p in inputs.cc_archive.rglob("*.json") if p.is_file())


ORIGINS = (CcArchive,)
