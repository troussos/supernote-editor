"""Rendering: native color-code rasters -> displayable images.

Matches supernote-tool's palette semantics exactly (verified by the oracle
tests): X1-era files map the eight classic codes; X2-era files (version >=
20230015) additionally treat unmapped byte values as direct 8-bit grayscale
and remap the legacy gray codes to their on-device compat values.
"""
from __future__ import annotations

import io

import numpy as np

from .constants import (
    DISPLAY_BLACK,
    DISPLAY_DARK_GRAY,
    DISPLAY_DARK_GRAY_COMPAT,
    DISPLAY_GRAY,
    DISPLAY_GRAY_COMPAT,
    DISPLAY_TRANSPARENT,
    DISPLAY_WHITE,
)
from .model import SnNotebook, SnPage


def make_lut(highres: bool) -> np.ndarray:
    """256-entry LUT from native code -> display gray (0xFF = transparent)."""
    if highres:
        lut = np.arange(256, dtype=np.uint8)   # unmapped bytes are direct gray
        lut[0x61] = DISPLAY_BLACK
        lut[0x62] = DISPLAY_TRANSPARENT
        lut[0x63] = DISPLAY_DARK_GRAY_COMPAT
        lut[0x64] = DISPLAY_GRAY_COMPAT
        lut[0x65] = DISPLAY_WHITE
        lut[0x66] = DISPLAY_BLACK
        lut[0x9D] = DISPLAY_DARK_GRAY
        lut[0x9E] = DISPLAY_DARK_GRAY
        lut[0xC9] = DISPLAY_GRAY
        lut[0xCA] = DISPLAY_GRAY
    else:
        lut = np.arange(256, dtype=np.uint8)
        lut[0x61] = DISPLAY_BLACK
        lut[0x62] = DISPLAY_TRANSPARENT
        lut[0x63] = DISPLAY_DARK_GRAY
        lut[0x64] = DISPLAY_GRAY
        lut[0x65] = DISPLAY_WHITE
        lut[0x66] = DISPLAY_BLACK
        lut[0x67] = DISPLAY_DARK_GRAY
        lut[0x68] = DISPLAY_GRAY
    return lut


def layer_display(nb: SnNotebook, page: SnPage, slot: str) -> tuple[np.ndarray, np.ndarray] | None:
    """(gray, painted_mask) for a layer, or None if it renders nothing.

    RLE layers paint wherever the native code is not the transparent
    background 0x62; PNG template backgrounds are fully opaque.
    """
    layer = page.layers.get(slot)
    if layer is None or layer.bitmap is None:
        return None
    if slot == "BGLAYER" and page.style.startswith("user_"):
        gray = _png_background_gray(nb, page, layer.bitmap)
        if gray is None:
            return None
        return gray, np.ones(gray.shape, dtype=bool)
    raster = nb.layer_raster(page, slot)
    if raster is None:
        return None
    gray = make_lut(nb.is_highres)[raster]
    return gray, gray != DISPLAY_TRANSPARENT


def _png_background_gray(nb: SnNotebook, page: SnPage, data: bytes) -> np.ndarray | None:
    from PIL import Image
    try:
        img = Image.open(io.BytesIO(data))
        rows, cols = nb.raster_shape(page)
        if img.size != (cols, rows):
            img = img.resize((cols, rows))
        if img.mode != "RGB":
            # transparent template pixels count as white paper
            white = Image.new("RGBA", img.size, (255, 255, 255, 255))
            rgba = img.convert("RGBA")
            white.paste(rgba, mask=rgba)
            img = white
        return np.asarray(img.convert("L"), dtype=np.uint8).copy()
    except Exception:
        return None


def composite_page(
    nb: SnNotebook,
    page: SnPage,
    *,
    visibility_override: dict[str, bool] | None = None,
) -> np.ndarray:
    """Flatten a page bottom-up over white paper. Returns uint8 (rows, cols)."""
    rows, cols = nb.raster_shape(page)
    out = np.full((rows, cols), 0xFF, dtype=np.uint8)
    visibility = page.visibility()
    if visibility_override:
        visibility = {**visibility, **visibility_override}
    for slot in reversed(page.layer_order()):
        if not visibility.get(slot, True):
            continue
        rendered = layer_display(nb, page, slot)
        if rendered is None:
            continue
        gray, mask = rendered
        out[mask] = gray[mask]
    return out
