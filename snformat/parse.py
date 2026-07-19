"""Parser: .note bytes -> SnNotebook.

Follows references from the trailing footer address only (device files are
log-structured and may contain stale orphan blocks anywhere, in any order).
Every metadata block is validated to be fully expressible as <KEY:VALUE>
pairs so nothing can be silently dropped on rewrite.
"""
from __future__ import annotations

import re

from .constants import (
    ADDRESS_SIZE,
    FILETYPE_LEN,
    HEADER_PREFIX_LEN,
    KNOWN_VERSIONS,
    LAYER_KEYS,
    LENGTH_FIELD_SIZE,
    SIGNATURE_PREFIX,
    TYPE_MARK,
    TYPE_NOTE,
)
from .kv import Meta, MetadataError
from .model import SnFooterEntry, SnLayer, SnNotebook, SnPage

_SIG_RE = re.compile(r"SN_FILE_VER_(\d{8})")
_PAGE_KEY_RE = re.compile(r"PAGE(\d+)$")


class ParseError(ValueError):
    pass


def _read_block(buf: bytes, addr: int, what: str) -> bytes:
    if addr < HEADER_PREFIX_LEN or addr + LENGTH_FIELD_SIZE > len(buf):
        raise ParseError(f"{what}: block address {addr} out of range (file size {len(buf)})")
    ln = int.from_bytes(buf[addr:addr + LENGTH_FIELD_SIZE], "little")
    end = addr + LENGTH_FIELD_SIZE + ln
    if end > len(buf):
        raise ParseError(f"{what}: block at {addr} (length {ln}) exceeds file size {len(buf)}")
    return buf[addr + LENGTH_FIELD_SIZE:end]


def _addr(meta: Meta, key: str, what: str) -> int:
    v = meta.get(key)
    if v is None:
        return 0
    try:
        return int(v)
    except ValueError:
        raise ParseError(f"{what}: key {key} has non-numeric address {v!r}") from None


def parse_bytes(buf: bytes, *, source_path: str | None = None) -> SnNotebook:
    if len(buf) < HEADER_PREFIX_LEN + ADDRESS_SIZE:
        raise ParseError(f"file too small ({len(buf)} bytes)")
    filetype = buf[:FILETYPE_LEN].decode("ascii", errors="replace")
    if filetype not in (TYPE_NOTE, TYPE_MARK):
        raise ParseError(f"not a Supernote file (type field {filetype!r})")
    sig = buf[FILETYPE_LEN:HEADER_PREFIX_LEN].decode("ascii", errors="replace")
    m = _SIG_RE.fullmatch(sig)
    if not m:
        raise ParseError(
            f"unsupported signature {sig!r} — only X-series files ({SIGNATURE_PREFIX}...) are supported"
        )
    warnings: list[str] = []
    version = int(m[1])
    if version not in KNOWN_VERSIONS:
        warnings.append(
            f"file version {version} is newer than any this app has been verified against; "
            "parsing will be attempted and every save is fully re-verified, but please "
            "keep the automatic .bak backups until you have confirmed the result on-device"
        )

    block_starts: set[int] = set()

    def read(addr: int, what: str) -> bytes:
        block_starts.add(addr)
        return _read_block(buf, addr, what)

    def read_meta(addr: int, what: str) -> Meta:
        try:
            return Meta.parse(read(addr, what), what=what)
        except MetadataError as e:
            raise ParseError(str(e)) from None

    footer_addr = int.from_bytes(buf[-ADDRESS_SIZE:], "little")
    footer = read_meta(footer_addr, "footer")

    header_addr = _addr(footer, "FILE_FEATURE", "footer")
    if header_addr == 0:
        raise ParseError("footer has no FILE_FEATURE (header) address")
    header = read_meta(header_addr, "header")

    # --- styles (shared background blocks) ---
    styles: dict[str, bytes] = {}
    style_addr_to_key: dict[int, str] = {}
    for k, v in footer.pairs:
        if k.startswith("STYLE_"):
            a = int(v)
            if a > 0 and k not in styles:
                styles[k] = read(a, f"footer {k}")
                style_addr_to_key.setdefault(a, k)

    # --- cover ---
    cover_key = None
    cover = None
    for k in ("COVER_2", "COVER_1"):
        if footer.has(k):
            a = _addr(footer, k, "footer")
            if a > 0:
                cover_key = k
                cover = read(a, f"footer {k}")
                break

    # --- pdf style list (address key unknown to supernote-tool) ---
    pdfstylelist = None
    a = _addr(footer, "PDFSTYLELIST", "footer")
    if a > 0:
        pdfstylelist = read(a, "footer PDFSTYLELIST")

    # --- keywords / titles / links, preserving footer order ---
    entries: list[SnFooterEntry] = []
    for k, v in footer.pairs:
        if k.startswith("KEYWORD_"):
            kind, content_key = "KEYWORD", "KEYWORDSITE"
        elif k.startswith("TITLE_"):
            kind, content_key = "TITLE", "TITLEBITMAP"
        elif k.startswith(("LINKO_", "LINKI_")):
            kind, content_key = "LINK", "LINKBITMAP"
        else:
            continue
        meta = read_meta(int(v), f"footer {k}")
        caddr = _addr(meta, content_key, k)
        content = read(caddr, f"{k} content") if caddr > 0 else None
        entries.append(SnFooterEntry(kind=kind, footer_key=k, meta=meta, content=content))

    # --- pages ---
    page_items: list[tuple[int, str, str]] = []
    for k, v in footer.pairs:
        pm = _PAGE_KEY_RE.fullmatch(k)
        if pm:
            page_items.append((int(pm[1]), k, v))
    page_items.sort(key=lambda t: t[0])
    if not page_items:
        raise ParseError("file has no pages")
    expected = list(range(1, len(page_items) + 1))
    if [n for n, _, _ in page_items] != expected:
        raise ParseError(f"non-contiguous page numbers in footer: {[n for n, _, _ in page_items]}")

    pages: list[SnPage] = []
    for num, k, v in page_items:
        pmeta = read_meta(int(v), k)
        layers: dict[str, SnLayer] = {}
        for slot in LAYER_KEYS:
            laddr = _addr(pmeta, slot, k)
            if laddr == 0:
                continue
            lmeta = read_meta(laddr, f"{k}.{slot}")
            layer = SnLayer(slot=slot, meta=lmeta)
            baddr = _addr(lmeta, "LAYERBITMAP", f"{k}.{slot}")
            if baddr > 0:
                layer.bitmap = read(baddr, f"{k}.{slot}.LAYERBITMAP")
                layer.style_key = style_addr_to_key.get(baddr)
            vaddr = _addr(lmeta, "LAYERVECTORGRAPH", f"{k}.{slot}")
            if vaddr > 0:
                layer.vector_block = read(vaddr, f"{k}.{slot}.LAYERVECTORGRAPH")
            raddr = _addr(lmeta, "LAYERRECOGN", f"{k}.{slot}")
            if raddr > 0:
                layer.recogn_block = read(raddr, f"{k}.{slot}.LAYERRECOGN")
            layers[slot] = layer
        page = SnPage(meta=pmeta, layers=layers)
        for attr, key in (
            ("totalpath", "TOTALPATH"),
            ("recognfile", "RECOGNFILE"),
            ("recogntext", "RECOGNTEXT"),
            ("idtable", "IDTABLE"),
            ("externallinkinfo", "EXTERNALLINKINFO"),
        ):
            a = _addr(pmeta, key, k)
            if a > 0:
                setattr(page, attr, read(a, f"{k}.{key}"))
        pages.append(page)

    nb = SnNotebook(
        filetype=filetype,
        signature=sig,
        header=header,
        footer=footer,
        pages=pages,
        entries=entries,
        styles=styles,
        cover_key=cover_key,
        cover=cover,
        pdfstylelist=pdfstylelist,
        source_path=source_path,
        source_size=len(buf),
        source_block_starts=frozenset(block_starts),
        warnings=warnings,
    )

    # Decode every editable raster now: a file we cannot fully decode is a
    # file we must not edit, and we want to know at open time, not save time.
    for i, page in enumerate(nb.pages):
        for slot, layer in page.layers.items():
            if layer.protocol == "RATTA_RLE" and not (
                slot == "BGLAYER" and page.style.startswith("user_")
            ):
                nb.layer_raster(page, slot)   # raises RleError on failure
    return nb


def parse_file(path: str) -> SnNotebook:
    with open(path, "rb") as f:
        buf = f.read()
    return parse_bytes(buf, source_path=path)
