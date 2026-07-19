"""Writer: SnNotebook -> .note bytes.

Rebuilds the file from the reference graph in the canonical block order used
by Supernote devices and supernote-tool's device-proven merge feature:

  note+signature | header | cover | keyword/title/link (content, meta) |
  STYLE_* blocks | PDFSTYLELIST | per page (per layer: bitmap, layer-meta;
  then TOTALPATH, RECOGNFILE, RECOGNTEXT, IDTABLE, EXTERNALLINKINFO,
  page-meta) | footer | b'tail' | footer address (LE32)

All references point backward, so addresses are final on first placement.
Stale orphan blocks from the device's log-structured writes are dropped
(exactly what the device itself does on a full rewrite). Only layers whose
raster was actually edited are re-encoded; every other block is emitted
byte-for-byte from the source file.
"""
from __future__ import annotations

import re

from . import rle
from .constants import (
    LAYER_KEYS,
    LENGTH_FIELD_SIZE,
    TAIL_MARKER,
)
from .kv import Meta
from .model import SnNotebook

_PAGE_KEY_RE = re.compile(r"PAGE(\d+)$")
_ENTRY_PREFIXES = ("KEYWORD_", "TITLE_", "LINKO_", "LINKI_")

# Keys whose values the writer rewrites; anything else with a numeric value
# matching a source block offset triggers an "unknown address key" warning.
_REWRITTEN_PAGE_KEYS = {
    "MAINLAYER", "LAYER1", "LAYER2", "LAYER3", "BGLAYER",
    "TOTALPATH", "RECOGNFILE", "RECOGNTEXT", "IDTABLE", "EXTERNALLINKINFO",
}
_REWRITTEN_LAYER_KEYS = {"LAYERBITMAP", "LAYERVECTORGRAPH", "LAYERRECOGN"}
_ENTRY_CONTENT_KEYS = {"KEYWORD": "KEYWORDSITE", "TITLE": "TITLEBITMAP", "LINK": "LINKBITMAP"}


class WriteError(ValueError):
    pass


class _Builder:
    def __init__(self):
        self.chunks: list[bytes] = []
        self.size = 0

    def raw(self, data: bytes) -> int:
        addr = self.size
        self.chunks.append(data)
        self.size += len(data)
        return addr

    def block(self, payload: bytes) -> int:
        addr = self.size
        self.chunks.append(len(payload).to_bytes(LENGTH_FIELD_SIZE, "little"))
        self.chunks.append(payload)
        self.size += LENGTH_FIELD_SIZE + len(payload)
        return addr

    def build(self) -> bytes:
        return b"".join(self.chunks)


def serialize(nb: SnNotebook) -> tuple[bytes, dict[tuple[int, str], bytes], list[str]]:
    """Serialize a notebook.

    Returns (file bytes, {(page_index, slot): re-encoded bitmap} for dirty
    layers, warnings).
    """
    warnings: list[str] = []
    b = _Builder()
    b.raw(nb.filetype.encode("ascii"))
    b.raw(nb.signature.encode("ascii"))

    header_addr = b.block(nb.header.serialize())

    cover_addr = 0
    if nb.cover is not None:
        cover_addr = b.block(nb.cover)

    # keywords / titles / links
    entry_addrs: list[int] = []
    for entry in nb.entries:
        content_key = _ENTRY_CONTENT_KEYS[entry.kind]
        content_addr = b.block(entry.content) if entry.content is not None else 0
        emeta = entry.meta.copy()
        if emeta.has(content_key) or content_addr:
            emeta.set(content_key, content_addr)
        _audit_meta(emeta, {content_key}, nb, f"{entry.footer_key} metadata", warnings)
        entry_addrs.append(b.block(emeta.serialize()))

    # shared style / background blocks
    style_addrs: dict[str, int] = {}
    for key, content in nb.styles.items():
        style_addrs[key] = b.block(content)

    pdfstylelist_addr = b.block(nb.pdfstylelist) if nb.pdfstylelist is not None else 0

    # pages
    encoded: dict[tuple[int, str], bytes] = {}
    page_meta_addrs: list[int] = []
    for pi, page in enumerate(nb.pages):
        layer_meta_addrs: dict[str, int] = {}
        for slot in LAYER_KEYS:
            layer = page.layers.get(slot)
            if layer is None:
                continue
            if layer.dirty:
                raster = nb.layer_raster(page, slot)
                if raster is None:
                    raise WriteError(f"page {pi + 1} layer {slot} marked dirty but has no raster")
                bitmap = rle.encode(raster)
                # self-check before anything touches disk
                redecoded = rle.decode(bitmap, raster.size)
                if not (redecoded.reshape(raster.shape) == raster).all():
                    raise WriteError(f"RLE round-trip self-check failed for page {pi + 1} {slot}")
                encoded[(pi, slot)] = bitmap
            else:
                bitmap = layer.bitmap

            if bitmap is None:
                bitmap_addr = 0
            elif layer.style_key is not None:
                bitmap_addr = style_addrs.get(layer.style_key)
                if bitmap_addr is None:
                    raise WriteError(f"page {pi + 1} {slot} references missing style {layer.style_key}")
                if layer.dirty:
                    raise WriteError(f"page {pi + 1} {slot} is style-shared and cannot be edited")
            else:
                bitmap_addr = b.block(bitmap)

            vec_addr = b.block(layer.vector_block) if layer.vector_block is not None else 0
            rec_addr = b.block(layer.recogn_block) if layer.recogn_block is not None else 0

            lmeta = layer.meta.copy()
            lmeta.set("LAYERBITMAP", bitmap_addr)
            lmeta.set_if_present("LAYERVECTORGRAPH", vec_addr)
            lmeta.set_if_present("LAYERRECOGN", rec_addr)
            _audit_meta(lmeta, _REWRITTEN_LAYER_KEYS, nb, f"page {pi + 1} {slot} metadata", warnings)
            layer_meta_addrs[slot] = b.block(lmeta.serialize())

        aux_addrs = {}
        for key, data in (
            ("TOTALPATH", page.totalpath),
            ("RECOGNFILE", page.recognfile),
            ("RECOGNTEXT", page.recogntext),
            ("IDTABLE", page.idtable),
            ("EXTERNALLINKINFO", page.externallinkinfo),
        ):
            aux_addrs[key] = b.block(data) if data is not None else 0

        pmeta = page.meta.copy()
        for slot in LAYER_KEYS:
            if slot in layer_meta_addrs:
                pmeta.set(slot, layer_meta_addrs[slot])
            else:
                pmeta.set_if_present(slot, 0)
        for key, addr in aux_addrs.items():
            if addr or pmeta.has(key):
                pmeta.set(key, addr)
        _audit_meta(pmeta, _REWRITTEN_PAGE_KEYS, nb, f"page {pi + 1} metadata", warnings)
        page_meta_addrs.append(b.block(pmeta.serialize()))

    footer = _rebuild_footer(
        nb, header_addr, cover_addr, entry_addrs, style_addrs, pdfstylelist_addr,
        page_meta_addrs, warnings,
    )
    footer_addr = b.block(footer.serialize())
    b.raw(TAIL_MARKER)
    b.raw(footer_addr.to_bytes(4, "little"))
    return b.build(), encoded, warnings


def _rebuild_footer(
    nb: SnNotebook,
    header_addr: int,
    cover_addr: int,
    entry_addrs: list[int],
    style_addrs: dict[str, int],
    pdfstylelist_addr: int,
    page_meta_addrs: list[int],
    warnings: list[str],
) -> Meta:
    new_pairs: list[list[str]] = []
    pages_emitted = False
    entries_emitted = False

    def emit_pages():
        nonlocal pages_emitted
        if not pages_emitted:
            for i, addr in enumerate(page_meta_addrs):
                new_pairs.append([f"PAGE{i + 1}", str(addr)])
            pages_emitted = True

    def emit_entries():
        nonlocal entries_emitted
        if not entries_emitted:
            for entry, addr in zip(nb.entries, entry_addrs):
                new_pairs.append([entry.footer_key, str(addr)])
            entries_emitted = True

    rewritten = {"FILE_FEATURE", "PDFSTYLELIST"}
    for k, v in nb.footer.pairs:
        if _PAGE_KEY_RE.fullmatch(k):
            emit_pages()
        elif k.startswith(_ENTRY_PREFIXES):
            emit_entries()
        elif k == "FILE_FEATURE":
            new_pairs.append([k, str(header_addr)])
        elif k == "PDFSTYLELIST" and pdfstylelist_addr:
            new_pairs.append([k, str(pdfstylelist_addr)])
        elif k.startswith("STYLE_"):
            addr = style_addrs.get(k)
            new_pairs.append([k, str(addr) if addr is not None else v])
            rewritten.add(k)
        elif k == nb.cover_key and cover_addr:
            new_pairs.append([k, str(cover_addr)])
            rewritten.add(k)
        else:
            new_pairs.append([k, v])
    # groups that existed in no original pair still must be written
    emit_pages()
    emit_entries()

    footer = Meta(new_pairs)
    audit_skip = rewritten | {p[0] for p in new_pairs if _PAGE_KEY_RE.fullmatch(p[0]) or p[0].startswith(_ENTRY_PREFIXES)}
    _audit_meta(footer, audit_skip, nb, "footer", warnings)
    return footer


def _audit_meta(meta: Meta, rewritten_keys: set[str], nb: SnNotebook, what: str, warnings: list[str]) -> None:
    """Flag preserved-verbatim values that look like source block addresses.

    Guards against a future firmware adding an address-bearing key we don't
    know about: a stale address surviving into the output would dangle, so
    surface it loudly (the value could also be an ordinary number that
    happens to collide, hence a warning rather than an error).
    """
    for k, v in meta.pairs:
        if k in rewritten_keys:
            continue
        if v.isdigit() and len(v) >= 4 and int(v) in nb.source_block_starts:
            warnings.append(
                f"{what}: key {k}={v} matches a block address in the source file but is not "
                f"a known address key — if this key references data, that reference is now stale"
            )
