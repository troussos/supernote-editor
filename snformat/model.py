"""In-memory model of a .note file.

Everything not understood is preserved verbatim as bytes; every metadata
block keeps its original key order. Rasters live in the native color-code
domain (uint8, 0x62 = transparent background) and are only re-encoded if
`dirty` was set by an actual edit.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from . import rle
from .constants import (
    DEFAULT_PAGE_SIZE,
    EQUIPMENT_SIZES,
    HIGHRES_GRAYSCALE_VERSION,
    LANDSCAPE_ORIENTATIONS,
    PROTOCOL_RLE,
    SPECIAL_WHITE_STYLE_BLOCK_SIZE,
)
from .kv import Meta


@dataclass
class SnLayer:
    slot: str                     # page-metadata key: MAINLAYER / LAYER1..3 / BGLAYER
    meta: Meta
    bitmap: bytes | None = None   # original compressed bytes (None if LAYERBITMAP=0)
    style_key: str | None = None  # if bitmap is shared with a footer STYLE_* block
    vector_block: bytes | None = None   # LAYERVECTORGRAPH block, opaque
    recogn_block: bytes | None = None   # LAYERRECOGN block, opaque
    dirty: bool = False
    _raster: np.ndarray | None = field(default=None, repr=False)

    @property
    def name(self) -> str:
        return self.meta.get("LAYERNAME", self.slot)

    @property
    def protocol(self) -> str:
        return self.meta.get("LAYERPROTOCOL", "")

    def is_editable(self) -> bool:
        return self.slot != "BGLAYER" and self.protocol == PROTOCOL_RLE and self.bitmap is not None


@dataclass
class SnPage:
    meta: Meta
    layers: dict[str, SnLayer]          # keyed by slot, only present slots
    totalpath: bytes | None = None
    recognfile: bytes | None = None
    recogntext: bytes | None = None
    idtable: bytes | None = None
    externallinkinfo: bytes | None = None
    ink_dirty: bool = False             # any ink layer raster modified

    @property
    def style(self) -> str:
        return self.meta.get("PAGESTYLE", "")

    @property
    def orientation(self) -> str:
        return self.meta.get("ORIENTATION", "1000")

    @property
    def page_id(self) -> str | None:
        return self.meta.get("PAGEID")

    def layer_order(self) -> list[str]:
        seq = self.meta.get("LAYERSEQ")
        if seq:
            return seq.split(",")
        return [s for s in ("MAINLAYER", "BGLAYER") if s in self.layers]

    def visibility(self) -> dict[str, bool]:
        """Layer visibility from LAYERINFO (device-side setting)."""
        import base64
        import json
        vis: dict[str, bool] = {}
        raw = self.meta.get("LAYERINFO")
        if raw and raw != "none":
            try:
                try:
                    info = json.loads(raw.replace("#", ":"))
                except json.JSONDecodeError:
                    info = json.loads(base64.b64decode(raw).decode().replace("#", ":"))
                for entry in info:
                    lid = entry.get("layerId")
                    if entry.get("isBackgroundLayer"):
                        name = "BGLAYER"
                    elif lid == 0:
                        name = "MAINLAYER"
                    else:
                        name = f"LAYER{lid}"
                    vis[name] = bool(entry.get("isVisible", True))
            except Exception:
                pass
        vis.setdefault("MAINLAYER", True)
        for slot in self.layers:
            vis.setdefault(slot, True)
        return vis


@dataclass
class SnFooterEntry:
    """A keyword / title / link: footer key + metadata block + content block."""
    kind: str                    # 'KEYWORD' | 'TITLE' | 'LINK'
    footer_key: str              # e.g. 'TITLE_00010057', 'LINKO_000201110609'
    meta: Meta
    content: bytes | None


@dataclass
class SnNotebook:
    filetype: str
    signature: str
    header: Meta
    footer: Meta                       # original footer (addresses stale; writer rebuilds)
    pages: list[SnPage]
    entries: list[SnFooterEntry]       # keywords/titles/links in footer order
    styles: dict[str, bytes]           # footer STYLE_* key -> content
    cover_key: str | None = None       # 'COVER_1' / 'COVER_2' (None if COVER_0/absent)
    cover: bytes | None = None
    pdfstylelist: bytes | None = None
    source_path: str | None = None
    source_size: int = 0
    source_block_starts: frozenset[int] = frozenset()
    warnings: list[str] = field(default_factory=list)

    @property
    def version(self) -> int:
        return int(self.signature[-8:])

    @property
    def is_highres(self) -> bool:
        return self.version >= HIGHRES_GRAYSCALE_VERSION

    @property
    def equipment(self) -> str:
        return self.header.get("APPLY_EQUIPMENT", "")

    def page_size(self) -> tuple[int, int]:
        """(width, height) in portrait orientation."""
        return EQUIPMENT_SIZES.get(self.equipment, DEFAULT_PAGE_SIZE)

    def raster_shape(self, page: SnPage) -> tuple[int, int]:
        """(rows, cols) of the stored raster for a page."""
        w, h = self.page_size()
        if page.orientation in LANDSCAPE_ORIENTATIONS:
            return (w, h)   # landscape: rows=w, cols=h
        return (h, w)

    # ---- raster access ----
    def layer_raster(self, page: SnPage, slot: str) -> np.ndarray | None:
        """Decoded native-code raster for a layer (cached). None if no bitmap."""
        layer = page.layers.get(slot)
        if layer is None or layer.bitmap is None:
            return None
        if layer._raster is None:
            rows, cols = self.raster_shape(page)
            all_blank = (
                layer.slot == "BGLAYER"
                and page.style == "style_white"
                and len(layer.bitmap) == SPECIAL_WHITE_STYLE_BLOCK_SIZE
            )
            if layer.protocol != PROTOCOL_RLE or (
                layer.slot == "BGLAYER" and page.style.startswith("user_")
            ):
                return None   # PNG or unknown protocol: no editable raster
            flat = rle.decode(layer.bitmap, rows * cols, all_blank=all_blank)
            layer._raster = flat.reshape(rows, cols)
        return layer._raster

    def mark_ink_edited(self, page: SnPage, slot: str) -> None:
        layer = page.layers[slot]
        if not layer.is_editable():
            raise ValueError(f"layer {slot} is not editable")
        layer.dirty = True
        page.ink_dirty = True

    def is_dirty(self) -> bool:
        return any(p.ink_dirty or any(l.dirty for l in p.layers.values()) for p in self.pages)
