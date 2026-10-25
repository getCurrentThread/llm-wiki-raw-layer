from .fixtures import CID, row
from .helpers import capture


def rounds(fx):
    for text in ("synthetic deployment", "edited deployment", "synthetic deployment"):
        fx.write({CID: [[row(text=text)]]})
        assert capture(fx) == 0
