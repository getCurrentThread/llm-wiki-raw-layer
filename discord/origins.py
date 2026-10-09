from rawlayer.capture.origins import MultiVersionMixin, Origin
import os
from .readers import archive


class DcArchive(MultiVersionMixin, Origin):
    name = "dc_archive"
    kinds = frozenset({"guild", "ch", "msg", "user"})
    day_kinds = frozenset({"msg"})

    def read_once(self, ctx):
        if ctx.cfg.dc_sync and ctx.inputs.name == "live" and os.environ.get("RAWLAYER_ARCHIVE_READY") != "1":
            from .collector import sync_config
            from tools.archiveio import CollectorError
            from rawlayer.capture.precheck import check
            check(ctx.inputs.dc_archive)
            if sync_config(ctx.cfg).get("errors"):
                raise CollectorError("collection_incomplete")
        return archive.read_once(ctx.inputs.dc_archive, [p.casefold() for p in ctx.cfg.dc_exclude],
                                 ctx.denylist, self.name)

    def estimate_peak(self, inputs):
        return sum(p.stat().st_size for p in (inputs.dc_archive / "exports").rglob("*.json") if p.is_file())


ORIGINS = (DcArchive,)
