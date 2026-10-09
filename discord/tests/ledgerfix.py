from .fixtures import CID, message
from .helpers import capture


def rounds(fx):
    for text in ("synthetic deployment", "edited deployment", "synthetic deployment"):
        fx.write({CID: [[message(text=text)]]})
        assert capture(fx) == 0
