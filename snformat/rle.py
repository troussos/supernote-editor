"""RATTA_RLE codec.

Stream = pairs of (colorcode byte, length byte):
  - length 0x00..0x7F        -> run of length+1 pixels (1..128)
  - length 0xFF              -> run of 0x4000 pixels (0x400 in the special
                                "blank background" decode mode)
  - length 0x80..0xFE        -> multi-byte form: this pair is held; if the next
                                pair has the SAME colorcode, total run =
                                1 + next_length + (((held & 0x7F) + 1) << 7);
                                if a DIFFERENT colorcode follows (or stream
                                ends), the held pair alone contributes
                                ((held & 0x7F) + 1) << 7 pixels (stream end:
                                clipped down by powers of two to fit).

The encoder deliberately emits only the <=128 single-pair form and complete
two-pair forms (max 0x4000 px per unit) and never the ambiguous 0xFF marker,
so its output decodes identically under both decode modes and never depends
on the tail-clipping heuristic. Encoding is not byte-identical to the
device's own compressor, but decodes to the identical raster (verified by
round-trip tests) and is accepted by every known decoder implementation.
"""
from __future__ import annotations

import numpy as np

from .constants import (
    RLE_SPECIAL_LENGTH,
    RLE_SPECIAL_LENGTH_FOR_BLANK,
    RLE_SPECIAL_LENGTH_MARKER,
)


class RleError(ValueError):
    pass


def decode(data: bytes, expected_pixels: int, *, all_blank: bool = False) -> np.ndarray:
    """Decode RATTA_RLE bytes to a flat uint8 array of native color codes."""
    if len(data) % 2 != 0:
        raise RleError(f"RLE stream has odd byte count {len(data)}")
    codes = np.empty(len(data) // 2 + 2, dtype=np.uint8)
    lengths = np.empty(len(data) // 2 + 2, dtype=np.int64)
    n_runs = 0
    total = 0
    holder = None
    special = RLE_SPECIAL_LENGTH_FOR_BLANK if all_blank else RLE_SPECIAL_LENGTH

    for i in range(0, len(data), 2):
        code = data[i]
        length = data[i + 1]
        pushed = False
        if holder is not None:
            pcode, plen = holder
            holder = None
            if code == pcode:
                length = 1 + length + (((plen & 0x7F) + 1) << 7)
                codes[n_runs] = code
                lengths[n_runs] = length
                n_runs += 1
                total += length
                pushed = True
            else:
                plen2 = ((plen & 0x7F) + 1) << 7
                codes[n_runs] = pcode
                lengths[n_runs] = plen2
                n_runs += 1
                total += plen2
        if not pushed:
            if length == RLE_SPECIAL_LENGTH_MARKER:
                codes[n_runs] = code
                lengths[n_runs] = special
                n_runs += 1
                total += special
            elif length & 0x80:
                holder = (code, length)
            else:
                codes[n_runs] = code
                lengths[n_runs] = length + 1
                n_runs += 1
                total += length + 1

    if holder is not None:
        code, length = holder
        gap = expected_pixels - total
        for i in reversed(range(8)):
            l = ((length & 0x7F) + 1) << i
            if l <= gap:
                codes[n_runs] = code
                lengths[n_runs] = l
                n_runs += 1
                total += l
                break

    if total != expected_pixels:
        raise RleError(f"decoded {total} pixels, expected {expected_pixels}")
    return np.repeat(codes[:n_runs], lengths[:n_runs])


# Maximum pixels representable by one complete two-pair unit:
# first byte 0x80|126 (k=127) + second byte 0x7F -> 127*128 + 128 = 0x4000.
_MAX_UNIT = 0x4000


def encode(raster: np.ndarray) -> bytes:
    """Encode a flat (or 2-D) uint8 array of native color codes to RATTA_RLE."""
    flat = np.ascontiguousarray(raster, dtype=np.uint8).reshape(-1)
    if flat.size == 0:
        raise RleError("cannot encode empty raster")
    # maximal runs: boundaries where value changes
    change = np.flatnonzero(flat[1:] != flat[:-1])
    starts = np.concatenate(([0], change + 1))
    ends = np.concatenate((change + 1, [flat.size]))
    run_codes = flat[starts]
    run_lens = ends - starts

    out = bytearray()
    for code, n in zip(run_codes.tolist(), run_lens.tolist()):
        while n > 0:
            if n <= 128:
                out.append(code)
                out.append(n - 1)
                n = 0
            else:
                chunk = min(n, _MAX_UNIT)
                # avoid leaving a remainder of 0 < r < 1 (impossible) — any
                # remainder >= 1 is encodable, so no lookahead needed
                k = (chunk - 1) // 128          # 1..127
                m = (chunk - 1) % 128           # 0..127
                out.append(code)
                out.append(0x80 | (k - 1))
                out.append(code)
                out.append(m)
                n -= chunk
    return bytes(out)
