"""Editing engine: undoable raster operations over an SnNotebook.

Pure numpy/python — no Qt — so every operation is unit-testable. Raster
edits are recorded as rectangular before/after patches per (page, layer).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

import numpy as np

from snformat import SnNotebook, parse_file, save_notebook
from snformat.constants import COLOR_BACKGROUND, INK_LAYER_KEYS
from snformat.edit_ops import delete_page, duplicate_page, sync_page_after_ink_edit


# ---------------------------------------------------------------- commands

@dataclass
class RasterPatch:
    page_index: int
    slot: str
    x0: int
    y0: int
    before: np.ndarray
    after: np.ndarray

    def apply(self, doc: "Document", forward: bool) -> None:
        arr = self.after if forward else self.before
        raster = doc.raster(self.page_index, self.slot)
        h, w = arr.shape
        raster[self.y0:self.y0 + h, self.x0:self.x0 + w] = arr
        # undo/redo after a save must re-flag the layer or the next save
        # would silently write the stale encoded bitmap
        doc.nb.mark_ink_edited(doc.nb.pages[self.page_index], self.slot)
        doc._raster_changed(self.page_index, self.slot,
                            (self.x0, self.y0, w, h))


@dataclass
class TextObject:
    page_index: int
    slot: str
    x: float
    y: float
    text: str
    family: str
    point_size: float
    code: int                     # native color code to draw with
    uid: int = 0


@dataclass
class TextCommand:
    kind: str                     # 'add' | 'remove' | 'modify'
    obj: TextObject
    before: TextObject | None = None

    def apply(self, doc: "Document", forward: bool) -> None:
        kind = self.kind if forward else {"add": "remove", "remove": "add", "modify": "modify"}[self.kind]
        if kind == "add":
            doc.text_objects[self.obj.uid] = self.obj
        elif kind == "remove":
            doc.text_objects.pop(self.obj.uid, None)
        else:
            src = self.obj if forward else self.before
            doc.text_objects[src.uid] = src
        doc._text_changed(self.obj.page_index)


@dataclass
class CommandGroup:
    commands: list

    def apply(self, doc: "Document", forward: bool) -> None:
        for cmd in (self.commands if forward else reversed(self.commands)):
            cmd.apply(doc, forward)


# ---------------------------------------------------------------- document

class Document:
    """A notebook open for editing."""

    MAX_UNDO = 200

    def __init__(self, nb: SnNotebook, path: str):
        self.nb = nb
        self.path = path
        self.undo_stack: list = []
        self.redo_stack: list = []
        self.saved_undo_depth = 0
        self.text_objects: dict[int, TextObject] = {}
        self._next_text_uid = 1
        # listeners: fn(page_index, slot, rect_or_None)
        self.raster_listeners: list = []
        self.text_listeners: list = []
        self.structure_listeners: list = []

    # -------- lifecycle --------
    @classmethod
    def open(cls, path: str) -> "Document":
        return cls(parse_file(path), path)

    @property
    def page_count(self) -> int:
        return len(self.nb.pages)

    def is_modified(self) -> bool:
        return len(self.undo_stack) != self.saved_undo_depth or bool(self.text_objects)

    # -------- raster access --------
    def raster(self, page_index: int, slot: str) -> np.ndarray:
        r = self.nb.layer_raster(self.nb.pages[page_index], slot)
        if r is None:
            raise ValueError(f"page {page_index + 1} has no raster for {slot}")
        return r

    def page_shape(self, page_index: int) -> tuple[int, int]:
        return self.nb.raster_shape(self.nb.pages[page_index])

    def editable_slots(self, page_index: int) -> list[str]:
        page = self.nb.pages[page_index]
        return [s for s in INK_LAYER_KEYS
                if s in page.layers and page.layers[s].is_editable()]

    def _raster_changed(self, page_index: int, slot: str, rect) -> None:
        for fn in self.raster_listeners:
            fn(page_index, slot, rect)

    def _text_changed(self, page_index: int) -> None:
        for fn in self.text_listeners:
            fn(page_index)

    def _structure_changed(self) -> None:
        for fn in self.structure_listeners:
            fn()

    # -------- undo core --------
    def _push(self, cmd) -> None:
        cmd.apply(self, True)
        self.undo_stack.append(cmd)
        if len(self.undo_stack) > self.MAX_UNDO:
            self.undo_stack.pop(0)
            self.saved_undo_depth = max(-1, self.saved_undo_depth - 1)
        self.redo_stack.clear()

    def undo(self):
        if not self.undo_stack:
            return None
        cmd = self.undo_stack.pop()
        cmd.apply(self, False)
        self.redo_stack.append(cmd)
        return cmd

    def redo(self):
        if not self.redo_stack:
            return None
        cmd = self.redo_stack.pop()
        cmd.apply(self, True)
        self.undo_stack.append(cmd)
        return cmd

    # -------- raster edit API (used by tools) --------
    def apply_raster_edit(self, page_index: int, slot: str,
                          rect: tuple[int, int, int, int],
                          new_pixels: np.ndarray) -> None:
        """Record + apply one undoable raster patch. rect=(x, y, w, h)."""
        x, y, w, h = self._clip_rect(page_index, rect)
        if w <= 0 or h <= 0:
            return
        raster = self.raster(page_index, slot)
        before = raster[y:y + h, x:x + w].copy()
        after = np.asarray(new_pixels[:h, :w], dtype=np.uint8)
        if (before == after).all():
            return
        self.nb.mark_ink_edited(self.nb.pages[page_index], slot)
        self._push(RasterPatch(page_index, slot, x, y, before, after))

    def begin_stroke(self, page_index: int, slot: str) -> "StrokeRecorder":
        return StrokeRecorder(self, page_index, slot)

    def _clip_rect(self, page_index, rect):
        rows, cols = self.page_shape(page_index)
        x, y, w, h = rect
        x2, y2 = min(x + w, cols), min(y + h, rows)
        x, y = max(0, x), max(0, y)
        return x, y, max(0, x2 - x), max(0, y2 - y)

    # -------- selection ops --------
    def copy_region(self, page_index: int, slot: str, rect) -> np.ndarray | None:
        x, y, w, h = self._clip_rect(page_index, rect)
        if w <= 0 or h <= 0:
            return None
        return self.raster(page_index, slot)[y:y + h, x:x + w].copy()

    def clear_region(self, page_index: int, slot: str, rect) -> None:
        x, y, w, h = self._clip_rect(page_index, rect)
        if w <= 0 or h <= 0:
            return
        self.apply_raster_edit(page_index, slot, (x, y, w, h),
                               np.full((h, w), COLOR_BACKGROUND, dtype=np.uint8))

    def paste_pixels(self, page_index: int, slot: str, x: int, y: int,
                     pixels: np.ndarray, *, clear_rect=None) -> None:
        """Stamp non-background pixels at (x, y); optionally clear a source
        rect in the same gesture (= move) as one undo step."""
        rows, cols = self.page_shape(page_index)
        raster = self.raster(page_index, slot)
        commands = []
        self.nb.mark_ink_edited(self.nb.pages[page_index], slot)
        if clear_rect is not None:
            cx, cy, cw, ch = self._clip_rect(page_index, clear_rect)
            if cw > 0 and ch > 0:
                before = raster[cy:cy + ch, cx:cx + cw].copy()
                cmd = RasterPatch(page_index, slot, cx, cy, before,
                                  np.full((ch, cw), COLOR_BACKGROUND, dtype=np.uint8))
                cmd.apply(self, True)
                commands.append(cmd)
        ph, pw = pixels.shape
        x0, y0 = max(0, x), max(0, y)
        x1, y1 = min(cols, x + pw), min(rows, y + ph)
        if x1 > x0 and y1 > y0:
            sub = pixels[y0 - y:y1 - y, x0 - x:x1 - x]
            before = raster[y0:y1, x0:x1].copy()
            after = before.copy()
            mask = sub != COLOR_BACKGROUND
            after[mask] = sub[mask]
            cmd = RasterPatch(page_index, slot, x0, y0, before, after)
            cmd.apply(self, True)
            commands.append(cmd)
        if commands:
            self.undo_stack.append(CommandGroup(commands))
            self.redo_stack.clear()

    def crop_to_region(self, page_index: int, rect, slots: list[str]) -> None:
        """Clear everything outside rect on the given ink layers (page size
        is fixed by the device, so 'crop' keeps geometry and clears)."""
        x, y, w, h = self._clip_rect(page_index, rect)
        rows, cols = self.page_shape(page_index)
        commands = []
        for slot in slots:
            raster = self.raster(page_index, slot)
            before = raster.copy()
            after = np.full((rows, cols), COLOR_BACKGROUND, dtype=np.uint8)
            if w > 0 and h > 0:
                after[y:y + h, x:x + w] = before[y:y + h, x:x + w]
            if (before == after).all():
                continue
            self.nb.mark_ink_edited(self.nb.pages[page_index], slot)
            cmd = RasterPatch(page_index, slot, 0, 0, before, after)
            cmd.apply(self, True)
            commands.append(cmd)
        if commands:
            self.undo_stack.append(CommandGroup(commands))
            self.redo_stack.clear()

    # -------- text objects --------
    def add_text(self, obj: TextObject) -> TextObject:
        obj.uid = self._next_text_uid
        self._next_text_uid += 1
        self._push(TextCommand("add", obj))
        return obj

    def modify_text(self, uid: int, **changes) -> None:
        import dataclasses
        old = self.text_objects[uid]
        new = dataclasses.replace(old, **changes)
        new.uid = uid
        self._push(TextCommand("modify", new, before=old))

    def remove_text(self, uid: int) -> None:
        self._push(TextCommand("remove", self.text_objects[uid]))

    def texts_on_page(self, page_index: int) -> list[TextObject]:
        return [t for t in self.text_objects.values() if t.page_index == page_index]

    # -------- page structure --------
    def duplicate_page(self, page_index: int) -> None:
        duplicate_page(self.nb, page_index)
        for t in self.text_objects.values():
            if t.page_index > page_index:
                t.page_index += 1
        self._reset_undo("page duplicated")
        self._structure_changed()

    def delete_page(self, page_index: int) -> list[str]:
        warnings = delete_page(self.nb, page_index)
        self.text_objects = {u: t for u, t in self.text_objects.items()
                             if t.page_index != page_index}
        for t in self.text_objects.values():
            if t.page_index > page_index:
                t.page_index -= 1
        self._reset_undo("page deleted")
        self._structure_changed()
        return warnings

    def _reset_undo(self, why: str) -> None:
        # structural ops invalidate raster patch coordinates across pages;
        # clearing the stacks is the safe, honest option
        self.undo_stack.clear()
        self.redo_stack.clear()
        self.saved_undo_depth = -1   # unmatchable -> counts as modified

    # -------- save --------
    def save(self, path: str | None = None, *, burn_text) -> list[str]:
        """Save (optionally to a new path). burn_text(document) rasterizes
        text objects into their layers and returns sidecar data; injected so
        this module stays Qt-free."""
        target = path or self.path
        sidecar = burn_text(self) if self.text_objects else None
        for page in self.nb.pages:
            if page.ink_dirty:
                sync_page_after_ink_edit(page)
        warnings = save_notebook(self.nb, target)
        self.path = target
        self.saved_undo_depth = len(self.undo_stack)
        if sidecar is not None and sidecar.get("texts"):
            import hashlib
            h = hashlib.sha256()
            with open(target, "rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    h.update(chunk)
            sidecar["file_sha256"] = h.hexdigest()
            self._write_sidecar(target, sidecar)
        else:
            self._remove_sidecar(target)
        return warnings

    # -------- text sidecar --------
    @staticmethod
    def sidecar_path(note_path: str) -> str:
        return note_path + ".snedit.json"

    def _write_sidecar(self, note_path: str, sidecar: dict) -> None:
        import json
        tmp = self.sidecar_path(note_path) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(sidecar, f)
        os.replace(tmp, self.sidecar_path(note_path))

    def _remove_sidecar(self, note_path: str) -> None:
        try:
            os.remove(self.sidecar_path(note_path))
        except OSError:
            pass


class StrokeRecorder:
    """Accumulates freehand stamping into one undo command.

    Tools stamp directly into the raster for live feedback; the recorder
    tracks the touched bounding box and snapshots 'before' lazily by tile so
    the whole gesture undoes as a unit.
    """

    TILE = 64

    def __init__(self, doc: Document, page_index: int, slot: str):
        self.doc = doc
        self.page_index = page_index
        self.slot = slot
        self.raster = doc.raster(page_index, slot)
        self.tiles: dict[tuple[int, int], np.ndarray] = {}
        self.bounds = None   # x0, y0, x1, y1

    def snapshot_rect(self, x: int, y: int, w: int, h: int) -> None:
        rows, cols = self.raster.shape
        x0, y0 = max(0, x), max(0, y)
        x1, y1 = min(cols, x + w), min(rows, y + h)
        if x1 <= x0 or y1 <= y0:
            return
        t = self.TILE
        for ty in range(y0 // t, (y1 - 1) // t + 1):
            for tx in range(x0 // t, (x1 - 1) // t + 1):
                key = (tx, ty)
                if key not in self.tiles:
                    self.tiles[key] = self.raster[ty * t:(ty + 1) * t,
                                                  tx * t:(tx + 1) * t].copy()
        if self.bounds is None:
            self.bounds = [x0, y0, x1, y1]
        else:
            b = self.bounds
            b[0], b[1] = min(b[0], x0), min(b[1], y0)
            b[2], b[3] = max(b[2], x1), max(b[3], y1)

    def finish(self) -> None:
        if self.bounds is None:
            return
        x0, y0, x1, y1 = self.bounds
        after = self.raster[y0:y1, x0:x1].copy()
        # rebuild 'before' from tiles
        before = after.copy()
        t = self.TILE
        for (tx, ty), tile in self.tiles.items():
            bx0, by0 = tx * t, ty * t
            ix0, iy0 = max(bx0, x0), max(by0, y0)
            ix1 = min(bx0 + tile.shape[1], x1)
            iy1 = min(by0 + tile.shape[0], y1)
            if ix1 > ix0 and iy1 > iy0:
                before[iy0 - y0:iy1 - y0, ix0 - x0:ix1 - x0] = \
                    tile[iy0 - by0:iy1 - by0, ix0 - bx0:ix1 - bx0]
        if (before == after).all():
            return
        self.doc.nb.mark_ink_edited(self.doc.nb.pages[self.page_index], self.slot)
        self.doc.undo_stack.append(
            RasterPatch(self.page_index, self.slot, x0, y0, before, after))
        self.doc.redo_stack.clear()
