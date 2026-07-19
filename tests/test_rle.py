"""RATTA_RLE codec property and edge-case tests."""
import numpy as np
import pytest

from snformat import rle


def roundtrip(arr):
    encoded = rle.encode(arr)
    decoded = rle.decode(encoded, arr.size)
    assert decoded.dtype == np.uint8
    assert (decoded == arr.reshape(-1)).all()
    return encoded


def test_single_pixel():
    roundtrip(np.array([0x61], dtype=np.uint8))


def test_full_blank_page():
    arr = np.full(1404 * 1872, 0x62, dtype=np.uint8)
    encoded = roundtrip(arr)
    assert len(encoded) < 1500


def test_alternating_worst_case():
    arr = np.tile(np.array([0x61, 0x62], dtype=np.uint8), 5000)
    roundtrip(arr)


@pytest.mark.parametrize("n", [1, 2, 127, 128, 129, 255, 256, 16383, 16384, 16385,
                               16512, 16513, 32768, 100000, 1404 * 1872])
def test_run_length_boundaries(n):
    arr = np.full(n, 0x9D, dtype=np.uint8)
    roundtrip(arr)
    # run embedded between different colors
    arr2 = np.concatenate([
        np.full(3, 0x61, dtype=np.uint8), np.full(n, 0x62, dtype=np.uint8),
        np.full(5, 0xC9, dtype=np.uint8),
    ])
    roundtrip(arr2)


@pytest.mark.parametrize("seed", range(8))
def test_random_rasters(seed):
    rng = np.random.default_rng(seed)
    # random run-structured data over full byte range (X2 direct grayscale)
    runs = rng.integers(1, 40000, size=200)
    codes = rng.integers(0, 256, size=200).astype(np.uint8)
    arr = np.repeat(codes, runs)
    roundtrip(arr)


def test_random_noise():
    rng = np.random.default_rng(42)
    arr = rng.integers(0, 256, size=1404 * 100).astype(np.uint8)
    roundtrip(arr)


def test_encoder_never_emits_special_marker():
    # 0xFF as a length byte decodes differently in blank mode; the encoder
    # must never produce it so output is mode-independent
    for n in [16384, 16385, 32768, 1404 * 1872]:
        arr = np.full(n, 0x62, dtype=np.uint8)
        encoded = rle.encode(arr)
        lengths = encoded[1::2]
        assert 0xFF not in lengths
        # and both decode modes agree
        a = rle.decode(encoded, n, all_blank=False)
        b = rle.decode(encoded, n, all_blank=True)
        assert (a == b).all()


def test_decode_device_special_length():
    # devices encode blank pages using the 0xFF marker
    data = bytes([0x62, 0xFF] * 160 + [0x62, 0x80 | 52, 0x62, 63])
    decoded = rle.decode(data, 1404 * 1872)
    assert (decoded == 0x62).all()


def test_decode_blank_mode_special_length():
    data = bytes([0x62, 0xFF] * 4)
    decoded = rle.decode(data, 4 * 0x400, all_blank=True)
    assert decoded.size == 4 * 0x400


def test_decode_lone_holder_tail_clipping():
    # trailing held pair is clipped by powers of two to fit the page
    data = bytes([0x61, 0x00, 0x62, 0x85])
    # holder would give (5+1)<<7 = 768; expected total 1 + 384 forces one halving
    decoded = rle.decode(data, 1 + 384)
    assert decoded.size == 385
    assert decoded[0] == 0x61
    assert (decoded[1:] == 0x62).all()


def test_decode_rejects_odd_stream():
    with pytest.raises(rle.RleError):
        rle.decode(b"\x61", 1)


def test_decode_rejects_wrong_total():
    with pytest.raises(rle.RleError):
        rle.decode(bytes([0x61, 0x00]), 2)


def test_encode_rejects_empty():
    with pytest.raises(rle.RleError):
        rle.encode(np.empty(0, dtype=np.uint8))


def test_2d_input():
    arr = np.full((100, 50), 0x61, dtype=np.uint8)
    arr[10:20, 5:30] = 0x62
    encoded = rle.encode(arr)
    decoded = rle.decode(encoded, arr.size).reshape(arr.shape)
    assert (decoded == arr).all()
