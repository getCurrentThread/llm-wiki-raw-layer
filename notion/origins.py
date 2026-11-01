from appkit.archive import ArchiveOrigin
from .readers import archive


class NtArchive(ArchiveOrigin):
    name = "nt_archive"
    kinds = frozenset({"page", "block", "comment", "ds", "user"})
    day_kinds = frozenset({"block"})
    peak = (("", "**/*.json"),)

    def read(self, ctx):
        self.fetch(ctx)
        return archive.containers(ctx.inputs.nt_archive, self.name)


ORIGINS = (NtArchive,)
