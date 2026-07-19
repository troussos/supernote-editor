"""Cross-validation against supernotelib as an independent implementation."""
import io

import numpy as np
import pytest

supernotelib = pytest.importorskip("supernotelib")
from supernotelib.converter import ImageConverter  # noqa: E402

from snformat import parse_bytes, serialize  # noqa: E402
from snformat.render import composite_page  # noqa: E402


def test_oracle_accepts_and_renders_rewritten_file(note_path):
    raw = open(note_path, "rb").read()
    data, _, _ = serialize(parse_bytes(raw))
    n1 = supernotelib.load(io.BytesIO(raw))
    n2 = supernotelib.load(io.BytesIO(data))
    assert n2.get_total_pages() == n1.get_total_pages()
    c1, c2 = ImageConverter(n1), ImageConverter(n2)
    for p in range(n1.get_total_pages()):
        a1 = np.asarray(c1.convert(p))
        a2 = np.asarray(c2.convert(p))
        assert a1.shape == a2.shape
        assert (a1 == a2).all(), f"page {p} render differs after rewrite"


def test_my_renderer_matches_oracle(note_path):
    raw = open(note_path, "rb").read()
    nb = parse_bytes(raw)
    oracle = supernotelib.load(io.BytesIO(raw))
    conv = ImageConverter(oracle)
    for p in range(oracle.get_total_pages()):
        mine = composite_page(nb, nb.pages[p])
        og = np.asarray(conv.convert(p).convert("L"))
        if nb.pages[p].orientation == "1270":
            # stock supernotelib shapes 1270 pages as portrait; the flat
            # pixel sequence is identical (allenporter snapshot renders
            # confirm landscape is correct)
            og = og.reshape(mine.shape)
        assert mine.shape == og.shape
        assert (mine == og).all(), f"page {p} differs from oracle render"
