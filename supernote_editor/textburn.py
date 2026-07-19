"""Text-box rasterization ("burning") and the editable-text sidecar.

.note files have no native typed-text object (typed text on the device is
ink), so text boxes are vector objects during the session and are rasterized
into their ink layer at save time. A sidecar JSON next to the .note stores
the text, fonts, and the raster pixels that were underneath, keyed to the
saved file's hash — reopening in this editor lifts the text back into
editable objects; every other consumer (device, official apps) just sees ink.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import zlib

import numpy as np
from PySide6.QtCore import Qt, QRectF
from PySide6.QtGui import QColor, QFont, QImage, QPainter

from snformat.constants import COLOR_BACKGROUND
from .document import CommandGroup, Document, RasterPatch, TextCommand, TextObject

SIDECAR_VERSION = 1


def make_font(obj: TextObject) -> QFont:
    font = QFont(obj.family)
    font.setPixelSize(max(4, round(obj.point_size)))
    return font


def render_text_patch(obj: TextObject, highres: bool) -> tuple[np.ndarray, np.ndarray] | None:
    """Rasterize a text object.

    Returns (codes, mask) where codes holds native color codes and mask marks
    pixels the text touches; None if the text renders to nothing.
    """
    font = make_font(obj)
    probe = QImage(1, 1, QImage.Format_ARGB32)
    p = QPainter(probe)
    p.setFont(font)
    rect = p.boundingRect(QRectF(0, 0, 8192, 8192), Qt.TextFlag.TextExpandTabs, obj.text)
    p.end()
    w, h = int(np.ceil(rect.width())) + 2, int(np.ceil(rect.height())) + 2
    if w <= 2 or h <= 2 or not obj.text.strip():
        return None
    img = QImage(w, h, QImage.Format_ARGB32)
    img.fill(0)
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
    p.setFont(font)
    p.setPen(QColor(0, 0, 0))
    p.drawText(QRectF(1, 1, w, h), Qt.TextFlag.TextExpandTabs, obj.text)
    p.end()
    buf = np.frombuffer(img.constBits(), dtype=np.uint8).reshape(h, img.bytesPerLine() // 4, 4)
    alpha = buf[:, :w, 3].astype(np.int32)

    codes = np.full((h, w), COLOR_BACKGROUND, dtype=np.uint8)
    if highres:
        # antialias with direct grayscale toward white, like device AA ink
        from snformat.render import make_lut
        ink_gray = int(make_lut(True)[obj.code])
        mask = alpha > 16
        aa = (255 - (alpha * (255 - ink_gray)) // 255).astype(np.uint8)
        full = alpha >= 240
        codes[mask] = aa[mask]
        codes[full] = obj.code
        # direct-gray bytes that collide with reserved codes shift by one
        for reserved in (0x61, 0x62, 0x65, 0x66, 0x9D, 0x9E, 0xC9, 0xCA, 0xFF):
            hit = (codes == reserved) & mask & ~full
            codes[hit] = reserved - 1
    else:
        mask = alpha >= 128
        codes[mask] = obj.code
    if not mask.any():
        return None
    return codes, mask


def burn_text_objects(doc: Document) -> dict:
    """Rasterize all text objects into their layers as ONE undoable group and
    return sidecar data capturing the text + underlying pixels."""
    texts = []
    unders = []
    commands = []
    for obj in sorted(doc.text_objects.values(), key=lambda t: t.uid):
        rendered = render_text_patch(obj, doc.nb.is_highres)
        if rendered is not None:
            codes, mask = rendered
            h, w = codes.shape
            x, y = int(round(obj.x)), int(round(obj.y))
            rows, cols = doc.page_shape(obj.page_index)
            x0, y0 = max(0, x), max(0, y)
            x1, y1 = min(cols, x + w), min(rows, y + h)
            if x1 > x0 and y1 > y0:
                raster = doc.raster(obj.page_index, obj.slot)
                sub_codes = codes[y0 - y:y1 - y, x0 - x:x1 - x]
                sub_mask = mask[y0 - y:y1 - y, x0 - x:x1 - x]
                before = raster[y0:y1, x0:x1].copy()
                after = before.copy()
                after[sub_mask] = sub_codes[sub_mask]
                doc.nb.mark_ink_edited(doc.nb.pages[obj.page_index], obj.slot)
                commands.append(RasterPatch(obj.page_index, obj.slot, x0, y0, before, after))
                unders.append({
                    "page_id": doc.nb.pages[obj.page_index].page_id or f"index:{obj.page_index}",
                    "slot": obj.slot, "x": x0, "y": y0,
                    "pixels": _pack_array(before),
                })
        commands.append(TextCommand("remove", obj))
        texts.append({
            "page_id": doc.nb.pages[obj.page_index].page_id or f"index:{obj.page_index}",
            "page_index": obj.page_index, "slot": obj.slot,
            "x": obj.x, "y": obj.y, "text": obj.text,
            "family": obj.family, "point_size": obj.point_size, "code": obj.code,
        })
    group = CommandGroup(commands)
    group.apply(doc, True)
    doc.undo_stack.append(group)
    doc.redo_stack.clear()
    return {"version": SIDECAR_VERSION, "texts": texts, "unders": unders}


def finalize_sidecar(sidecar: dict, saved_path: str) -> dict:
    sidecar["file_sha256"] = _file_hash(saved_path)
    return sidecar


def try_restore_sidecar(doc: Document) -> int:
    """If a matching sidecar exists, lift burned text back into editable
    objects. Returns the number of restored text boxes (0 if none)."""
    path = Document.sidecar_path(doc.path)
    if not os.path.exists(path):
        return 0
    try:
        with open(path, "r", encoding="utf-8") as f:
            sidecar = json.load(f)
        if sidecar.get("version") != SIDECAR_VERSION:
            return 0
        if sidecar.get("file_sha256") != _file_hash(doc.path):
            return 0   # file was edited elsewhere; burned text stays ink
        id_to_index = {p.page_id: i for i, p in enumerate(doc.nb.pages) if p.page_id}
        commands = []
        restored = 0
        for under in sidecar.get("unders", []):
            idx = _resolve_page(under["page_id"], id_to_index)
            if idx is None or idx >= doc.page_count:
                continue
            pixels = _unpack_array(under["pixels"])
            raster = doc.raster(idx, under["slot"])
            h, w = pixels.shape
            x, y = under["x"], under["y"]
            before = raster[y:y + h, x:x + w].copy()
            doc.nb.mark_ink_edited(doc.nb.pages[idx], under["slot"])
            commands.append(RasterPatch(idx, under["slot"], x, y, before, pixels))
        for t in sidecar.get("texts", []):
            idx = _resolve_page(t["page_id"], id_to_index)
            if idx is None:
                idx = t.get("page_index", 0)
            if idx >= doc.page_count:
                continue
            obj = TextObject(page_index=idx, slot=t["slot"], x=t["x"], y=t["y"],
                             text=t["text"], family=t["family"],
                             point_size=t["point_size"], code=t["code"],
                             uid=doc._next_text_uid)
            doc._next_text_uid += 1
            commands.append(TextCommand("add", obj))
            restored += 1
        if not commands:
            return 0
        group = CommandGroup(commands)
        group.apply(doc, True)
        doc.undo_stack.append(group)
        doc.saved_undo_depth = -1   # differs from disk until saved again
        return restored
    except Exception:
        return 0


def _resolve_page(page_id: str, id_to_index: dict) -> int | None:
    if page_id.startswith("index:"):
        return int(page_id.split(":", 1)[1])
    return id_to_index.get(page_id)


def _pack_array(arr: np.ndarray) -> dict:
    return {
        "shape": list(arr.shape),
        "zdata": base64.b64encode(zlib.compress(arr.tobytes(), 6)).decode("ascii"),
    }


def _unpack_array(packed: dict) -> np.ndarray:
    data = zlib.decompress(base64.b64decode(packed["zdata"]))
    return np.frombuffer(data, dtype=np.uint8).reshape(packed["shape"]).copy()


def _file_hash(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()
