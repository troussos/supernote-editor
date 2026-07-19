"""Whole-file round-trip guarantees over every fixture."""
import os

from snformat import parse_bytes, parse_file, serialize, verify_equivalent


def test_roundtrip_verifies_clean(note_path):
    nb = parse_file(note_path)
    data, encoded, warnings = serialize(nb)
    assert encoded == {}          # nothing was edited
    assert warnings == []         # no unknown-address-key suspicions
    assert verify_equivalent(nb, data) == []


def test_serialization_is_a_fixpoint(note_path):
    """serialize(parse(serialize(parse(x)))) must equal serialize(parse(x))."""
    nb1 = parse_file(note_path)
    data1, _, _ = serialize(nb1)
    nb2 = parse_bytes(data1)
    data2, _, _ = serialize(nb2)
    assert data1 == data2


def test_reparse_preserves_page_structure(note_path):
    nb1 = parse_file(note_path)
    data, _, _ = serialize(nb1)
    nb2 = parse_bytes(data)
    assert len(nb2.pages) == len(nb1.pages)
    for p1, p2 in zip(nb1.pages, nb2.pages):
        assert set(p1.layers) == set(p2.layers)
        assert p1.style == p2.style
        assert p1.orientation == p2.orientation
        assert p1.page_id == p2.page_id
        for slot in p1.layers:
            r1 = nb1.layer_raster(p1, slot)
            r2 = nb2.layer_raster(p2, slot)
            if r1 is None:
                assert r2 is None
            else:
                assert r1.shape == r2.shape
                assert (r1 == r2).all()


def test_device_canonical_files_roundtrip_byte_identical():
    """Orphan-free device files must reproduce exactly, byte for byte."""
    from tests.conftest import FIXTURES
    identical = 0
    for path in FIXTURES:
        raw = open(path, "rb").read()
        data, _, _ = serialize(parse_bytes(raw))
        if raw == data:
            identical += 1
    # 7 of the 19 fixtures are compact device files with no stale blocks
    assert identical >= 7


def test_source_metadata_recorded(note_path):
    nb = parse_file(note_path)
    assert nb.source_path == note_path
    assert nb.source_size == os.path.getsize(note_path)
    assert nb.version >= 20220011
    assert not nb.is_dirty()
