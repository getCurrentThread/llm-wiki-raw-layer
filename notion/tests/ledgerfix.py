from .fixtures import page, block, BID, OTHER
from .helpers import capture


def rounds(fx):
    for value in (page(), page(blocks={BID: block(text="edited deployment"), OTHER: block(OTHER, "inserted")}),
                  page(blocks={BID: block(text="edited deployment"), OTHER: block(OTHER, "inserted")},
                       order=[[OTHER, 0, None], [BID, 1, OTHER]])):
        fx.write(value)
        assert capture(fx) == 0
