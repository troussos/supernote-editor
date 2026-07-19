"""Semantic equivalence verification between a notebook and serialized bytes.

This is the no-corruption gate: a save is only allowed to reach disk if the
serialized bytes re-parse into a notebook whose every metadata pair, opaque
block, and decoded raster matches the in-memory model (modulo rewritten
block addresses, which are compared through the reference graph instead).
"""
from __future__ import annotations

import re

from .model import SnNotebook
from .parse import parse_bytes
from .write import _ENTRY_PREFIXES, _REWRITTEN_LAYER_KEYS, _REWRITTEN_PAGE_KEYS

_PAGE_KEY_RE = re.compile(r"PAGE(\d+)$")
_FOOTER_ADDR_KEYS = ("FILE_FEATURE", "PDFSTYLELIST")


def _diff_meta(a, b, skip: set[str], where: str, problems: list[str]) -> None:
    if [p[0] for p in a.pairs] != [p[0] for p in b.pairs]:
        problems.append(f"{where}: key sequence differs\n  src={a.keys()}\n  out={b.keys()}")
        return
    for (ka, va), (kb, vb) in zip(a.pairs, b.pairs):
        if ka in skip:
            continue
        if va != vb:
            problems.append(f"{where}: value of {ka} differs: {va!r} -> {vb!r}")


def _diff_bytes(a: bytes | None, b: bytes | None, where: str, problems: list[str]) -> None:
    if (a is None) != (b is None):
        problems.append(f"{where}: presence differs (src {a is not None}, out {b is not None})")
    elif a is not None and a != b:
        problems.append(f"{where}: content differs ({len(a)} vs {len(b)} bytes)")


def verify_equivalent(src: SnNotebook, out_bytes: bytes) -> list[str]:
    """Returns a list of problems; empty list means the output is faithful."""
    problems: list[str] = []
    try:
        out = parse_bytes(bytes(out_bytes))
    except Exception as e:
        return [f"serialized file failed to re-parse: {type(e).__name__}: {e}"]

    if out.filetype != src.filetype:
        problems.append(f"filetype {src.filetype} -> {out.filetype}")
    if out.signature != src.signature:
        problems.append(f"signature {src.signature} -> {out.signature}")

    _diff_meta(src.header, out.header, set(), "header", problems)

    # footer: non-group pairs must survive verbatim and in order; the PAGE /
    # keyword / title / link groups are validated against the model (they
    # legitimately change shape when pages are added or removed)
    def footer_nongroup_pairs(nb):
        fixed = []
        for k, v in nb.footer.pairs:
            if _PAGE_KEY_RE.fullmatch(k) or k.startswith(_ENTRY_PREFIXES):
                continue
            if k.startswith("STYLE_") or k in _FOOTER_ADDR_KEYS or k == nb.cover_key:
                fixed.append((k, None))   # key survives, value via graph
            else:
                fixed.append((k, v))
        return fixed

    if footer_nongroup_pairs(src) != footer_nongroup_pairs(out):
        problems.append(
            "footer: preserved pairs differ\n"
            f"  src={footer_nongroup_pairs(src)}\n  out={footer_nongroup_pairs(out)}"
        )
    out_page_keys = [k for k in out.footer.keys() if _PAGE_KEY_RE.fullmatch(k)]
    if out_page_keys != [f"PAGE{i + 1}" for i in range(len(src.pages))]:
        problems.append(f"footer: page keys {out_page_keys} do not match model page count {len(src.pages)}")
    out_entry_keys = [k for k in out.footer.keys() if k.startswith(_ENTRY_PREFIXES)]
    if out_entry_keys != [e.footer_key for e in src.entries]:
        problems.append(
            f"footer: entry keys differ from model\n"
            f"  model={[e.footer_key for e in src.entries]}\n  out={out_entry_keys}"
        )

    if list(src.styles.keys()) != list(out.styles.keys()):
        problems.append(f"styles keys differ: {list(src.styles)} -> {list(out.styles)}")
    else:
        for k in src.styles:
            _diff_bytes(src.styles[k], out.styles[k], f"style {k}", problems)
    _diff_bytes(src.cover, out.cover, "cover", problems)
    if src.cover is not None and src.cover_key != out.cover_key:
        problems.append(f"cover key {src.cover_key} -> {out.cover_key}")
    _diff_bytes(src.pdfstylelist, out.pdfstylelist, "PDFSTYLELIST", problems)

    if len(src.entries) != len(out.entries):
        problems.append(f"entry count {len(src.entries)} -> {len(out.entries)}")
    else:
        for i, (ea, eb) in enumerate(zip(src.entries, out.entries)):
            where = f"entry {i} ({ea.footer_key})"
            if ea.footer_key != eb.footer_key or ea.kind != eb.kind:
                problems.append(f"{where}: identity differs -> {eb.footer_key}/{eb.kind}")
                continue
            _diff_meta(ea.meta, eb.meta, {"KEYWORDSITE", "TITLEBITMAP", "LINKBITMAP"}, where, problems)
            _diff_bytes(ea.content, eb.content, f"{where} content", problems)

    if len(src.pages) != len(out.pages):
        problems.append(f"page count {len(src.pages)} -> {len(out.pages)}")
        return problems

    for pi, (pa, pb) in enumerate(zip(src.pages, out.pages)):
        where = f"page {pi + 1}"
        _diff_meta(pa.meta, pb.meta, _REWRITTEN_PAGE_KEYS, where, problems)
        for key in ("totalpath", "recognfile", "recogntext", "idtable", "externallinkinfo"):
            _diff_bytes(getattr(pa, key), getattr(pb, key), f"{where}.{key.upper()}", problems)
        if set(pa.layers) != set(pb.layers):
            problems.append(f"{where}: layer slots {sorted(pa.layers)} -> {sorted(pb.layers)}")
            continue
        for slot, la in pa.layers.items():
            lb = pb.layers[slot]
            lwhere = f"{where}.{slot}"
            _diff_meta(la.meta, lb.meta, _REWRITTEN_LAYER_KEYS, lwhere, problems)
            _diff_bytes(la.vector_block, lb.vector_block, f"{lwhere}.LAYERVECTORGRAPH", problems)
            _diff_bytes(la.recogn_block, lb.recogn_block, f"{lwhere}.LAYERRECOGN", problems)
            if la.style_key is not None:
                if lb.style_key != la.style_key:
                    problems.append(f"{lwhere}: style link {la.style_key} -> {lb.style_key}")
            elif la.dirty:
                # edited layer: decoded raster must match the model exactly
                ra = src.layer_raster(pa, slot)
                rb = out.layer_raster(pb, slot)
                if rb is None or ra is None or ra.shape != rb.shape or not (ra == rb).all():
                    problems.append(f"{lwhere}: edited raster does not survive re-encoding")
            else:
                _diff_bytes(la.bitmap, lb.bitmap, f"{lwhere}.LAYERBITMAP", problems)
    return problems
