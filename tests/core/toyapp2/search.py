# -*- coding: utf-8 -*-
"""The second toy index: <data_root>\\index\\toy2.sqlite, built like the toy index."""
from tests.core.toyapp import search as toy

HELP = "search the second toy index"


class Toy2Index(toy.ToyIndex):
    name = "toy2"


SPEC = Toy2Index()
add_arguments = toy.add_arguments


def run(a):
    return toy.run(a, SPEC)
