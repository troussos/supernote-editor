"""Structural edit operations on a notebook (pages, sync policy)."""
from __future__ import annotations

import datetime as _dt
import random
import re
import string

import numpy as np

from .kv import Meta
from .model import SnLayer, SnNotebook, SnPage

_ENTRY_KEY_RE = re.compile(r"^(KEYWORD_|TITLE_|LINKO_|LINKI_)(\d{4})(.*)$")


def new_page_id() -> str:
    """Device format: P + YYYYMMDDHHMMSS + 6 digits + 12 alphanumerics."""
    now = _dt.datetime.now()
    stamp = now.strftime("%Y%m%d%H%M%S") + f"{now.microsecond:06d}"
    suffix = "".join(random.choices(string.ascii_letters + string.digits, k=12))
    return f"P{stamp}{suffix}"


def sync_page_after_ink_edit(page: SnPage) -> None:
    """Reconcile page-level derived data after the ink raster changed.

    The stroke path table (TOTALPATH) and handwriting-recognition caches
    describe the old ink; keeping them would leave ghost strokes that the
    device's stroke-eraser/lasso could resurrect. Pages without them are
    normal (freshly created pages have TOTALPATH=0), so they are dropped and
    recognition is flagged as not-run so the device rebuilds it.
    """
    page.totalpath = None
    page.recognfile = None
    page.recogntext = None
    page.meta.set_if_present("RECOGNSTATUS", 0)
    page.meta.set_if_present("RECOGNFILESTATUS", 0)


def duplicate_page(nb: SnNotebook, index: int) -> SnPage:
    """Insert a copy of pages[index] after it. Returns the new page."""
    src = nb.pages[index]
    layers = {}
    for slot, l in src.layers.items():
        nl = SnLayer(
            slot=slot,
            meta=l.meta.copy(),
            bitmap=l.bitmap,
            style_key=l.style_key,
            vector_block=l.vector_block,
            recogn_block=l.recogn_block,
        )
        if l._raster is not None:
            nl._raster = np.array(l._raster, copy=True)
        layers[slot] = nl
    page = SnPage(
        meta=src.meta.copy(),
        layers=layers,
        totalpath=src.totalpath,
        recognfile=src.recognfile,
        recogntext=src.recogntext,
        idtable=src.idtable,
        externallinkinfo=src.externallinkinfo,
    )
    if page.meta.has("PAGEID"):
        page.meta.set("PAGEID", new_page_id())
    nb.pages.insert(index + 1, page)
    _shift_entry_pages(nb, first_affected=index + 2, delta=+1)
    return page


def delete_page(nb: SnNotebook, index: int) -> list[str]:
    """Delete pages[index]. Returns warnings (e.g. dropped titles/keywords)."""
    if len(nb.pages) <= 1:
        raise ValueError("a notebook must keep at least one page")
    warnings = []
    page_no = index + 1
    kept = []
    for e in nb.entries:
        m = _ENTRY_KEY_RE.match(e.footer_key)
        if m and int(m[2]) == page_no:
            warnings.append(f"dropped {e.footer_key} (was on deleted page {page_no})")
        else:
            kept.append(e)
    nb.entries = kept
    nb.pages.pop(index)
    _shift_entry_pages(nb, first_affected=page_no + 1, delta=-1)
    return warnings


def _shift_entry_pages(nb: SnNotebook, first_affected: int, delta: int) -> None:
    """Renumber the 4-digit page number embedded in entry footer keys."""
    for e in nb.entries:
        m = _ENTRY_KEY_RE.match(e.footer_key)
        if not m:
            continue
        page_no = int(m[2])
        if page_no >= first_affected:
            e.footer_key = f"{m[1]}{page_no + delta:04d}{m[3]}"
            for key in ("KEYWORDPAGE",):
                if e.meta.has(key):
                    e.meta.set(key, page_no + delta)
