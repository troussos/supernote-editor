"""Editing tools operating in page-raster coordinates."""
from __future__ import annotations

import numpy as np
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QCursor

from snformat.constants import COLOR_BACKGROUND
from .document import CommandGroup, TextObject

# ---------------------------------------------------------------- stamping


_DISK_CACHE: dict[int, np.ndarray] = {}


def disk_mask(radius: int) -> np.ndarray:
    m = _DISK_CACHE.get(radius)
    if m is None:
        r = max(0, radius)
        yy, xx = np.mgrid[-r:r + 1, -r:r + 1]
        m = (xx * xx + yy * yy) <= r * r + max(0, r // 2)
        _DISK_CACHE[radius] = m
    return m


def stamp_disk(recorder, cx: int, cy: int, radius: int, code: int) -> tuple:
    """Stamp a filled disk; returns the touched rect (x, y, w, h)."""
    raster = recorder.raster
    rows, cols = raster.shape
    r = max(0, radius)
    x0, y0 = cx - r, cy - r
    x1, y1 = cx + r + 1, cy + r + 1
    gx0, gy0 = max(0, x0), max(0, y0)
    gx1, gy1 = min(cols, x1), min(rows, y1)
    if gx1 <= gx0 or gy1 <= gy0:
        return (0, 0, 0, 0)
    recorder.snapshot_rect(gx0, gy0, gx1 - gx0, gy1 - gy0)
    mask = disk_mask(r)[gy0 - y0:gy1 - y0, gx0 - x0:gx1 - x0]
    region = raster[gy0:gy1, gx0:gx1]
    region[mask] = code
    return (gx0, gy0, gx1 - gx0, gy1 - gy0)


def stamp_segment(recorder, x0f, y0f, x1f, y1f, radius: int, code: int) -> tuple:
    """Stamp disks along a segment; returns combined touched rect."""
    dx, dy = x1f - x0f, y1f - y0f
    dist = max(abs(dx), abs(dy))
    steps = max(1, int(dist / max(1.0, radius * 0.45)))
    touched = None
    for i in range(steps + 1):
        t = i / steps
        rect = stamp_disk(recorder, round(x0f + dx * t), round(y0f + dy * t), radius, code)
        if rect[2] > 0:
            touched = rect if touched is None else _union(touched, rect)
    return touched or (0, 0, 0, 0)


def _union(a, b):
    ax0, ay0 = min(a[0], b[0]), min(a[1], b[1])
    ax1 = max(a[0] + a[2], b[0] + b[2])
    ay1 = max(a[1] + a[3], b[1] + b[3])
    return (ax0, ay0, ax1 - ax0, ay1 - ay0)


# ---------------------------------------------------------------- base


class Tool:
    name = "tool"
    status_hint = ""

    def __init__(self, window):
        self.window = window

    @property
    def canvas(self):
        return self.window.canvas

    @property
    def doc(self):
        return self.window.doc

    def activate(self):
        pass

    def deactivate(self):
        pass

    def cursor(self) -> QCursor:
        return QCursor(Qt.CursorShape.CrossCursor)

    def mouse_press(self, pos: QPointF, event) -> None: ...
    def mouse_move(self, pos: QPointF, event, pressed: bool) -> None: ...
    def mouse_release(self, pos: QPointF, event) -> None: ...

    def key_press(self, event) -> bool:
        return False


class BrushTool(Tool):
    """Shared machinery for pen / highlighter / eraser."""
    erases = False

    def __init__(self, window):
        super().__init__(window)
        self._recorder = None
        self._last = None

    def _code(self) -> int:
        raise NotImplementedError

    def _radius(self, event) -> int:
        base = self.window.tool_size(self)
        pressure = getattr(event, "pressure", None)
        if callable(pressure):
            p = pressure()
            if p and p > 0:
                return max(1, round(base * (0.55 + 0.45 * p)))
        return max(1, base)

    def mouse_press(self, pos, event):
        slot = self.window.active_slot()
        if slot is None:
            self.window.flash_status("This page has no editable ink layer selected")
            return
        self._recorder = self.doc.begin_stroke(self.window.page_index, slot)
        r = self._radius(event)
        rect = stamp_disk(self._recorder, round(pos.x()), round(pos.y()), r, self._code())
        self.canvas.refresh_layer_rect(slot, rect)
        self._last = (pos.x(), pos.y())

    def mouse_move(self, pos, event, pressed):
        if not pressed or self._recorder is None:
            return
        x0, y0 = self._last
        rect = stamp_segment(self._recorder, x0, y0, pos.x(), pos.y(),
                             self._radius(event), self._code())
        self.canvas.refresh_layer_rect(self._recorder.slot, rect)
        self._last = (pos.x(), pos.y())

    def mouse_release(self, pos, event):
        if self._recorder is not None:
            self._recorder.finish()
            self.window.after_edit()
            self._recorder = None
        self._last = None


class PenTool(BrushTool):
    name = "pen"
    status_hint = "Draw on the active layer. [ and ] change size."

    def _code(self):
        return self.window.pen_code()


class HighlighterTool(BrushTool):
    name = "highlighter"
    status_hint = "Highlighter ink (device marker codes). [ and ] change size."

    def _code(self):
        return self.window.marker_code()


class EraserTool(BrushTool):
    name = "eraser"
    status_hint = "Erase to transparent background. [ and ] change size."
    erases = True

    def _code(self):
        return COLOR_BACKGROUND


# ---------------------------------------------------------------- marquee


class MarqueeTool(Tool):
    name = "marquee"
    status_hint = ("Drag to select. Drag inside selection to move; "
                   "Ctrl+C/X/V, Del, Esc. Arrows nudge a floating selection.")

    def __init__(self, window):
        super().__init__(window)
        self.mode = "idle"        # idle | selecting | selected | floating
        self.rect = None          # (x, y, w, h) in raster coords
        self.float_pixels = None
        self.float_pos = None     # top-left of floating pixels
        self.float_source_cmd = None   # clear command for a lift-move
        self._press = None
        self._float_grab = None

    def deactivate(self):
        self.anchor_float()
        self._set_idle()

    def cursor(self):
        return QCursor(Qt.CursorShape.CrossCursor)

    def _set_idle(self):
        self.mode = "idle"
        self.rect = None
        self.canvas.set_selection_rect(None)
        self.canvas.set_float(None, None)

    # ---- gestures ----
    def mouse_press(self, pos, event):
        x, y = pos.x(), pos.y()
        if self.mode == "floating":
            fx, fy = self.float_pos
            fh, fw = self.float_pixels.shape
            if fx <= x < fx + fw and fy <= y < fy + fh:
                self._float_grab = (x - fx, y - fy)
                return
            self.anchor_float()
            self._set_idle()
            return
        if self.mode == "selected" and self.rect and _contains(self.rect, x, y):
            self._start_lift(x, y)
            return
        self.mode = "selecting"
        self._press = (x, y)
        self.canvas.set_selection_rect((x, y, 0, 0))

    def mouse_move(self, pos, event, pressed):
        if not pressed:
            return
        x, y = pos.x(), pos.y()
        if self.mode == "selecting" and self._press:
            px, py = self._press
            r = _norm_rect(px, py, x, y)
            self.rect = r
            self.canvas.set_selection_rect(r)
        elif self.mode == "floating" and self._float_grab:
            gx, gy = self._float_grab
            self.float_pos = (round(x - gx), round(y - gy))
            self.canvas.set_float(self.float_pixels, self.float_pos)

    def mouse_release(self, pos, event):
        if self.mode == "selecting":
            if self.rect and self.rect[2] >= 2 and self.rect[3] >= 2:
                self.mode = "selected"
                self.window.flash_status(
                    f"Selected {self.rect[2]}x{self.rect[3]} px — drag to move, Ctrl+C copy, Del clear")
            else:
                self._set_idle()
        self._press = None
        self._float_grab = None

    # ---- operations ----
    def _slot(self):
        slot = self.window.active_slot()
        if slot is None:
            self.window.flash_status("No editable ink layer selected")
        return slot

    def _start_lift(self, x, y):
        slot = self._slot()
        if slot is None:
            return
        pixels = self.doc.copy_region(self.window.page_index, slot, _int_rect(self.rect))
        if pixels is None:
            return
        rx, ry, rw, rh = _int_rect(self.rect)
        self.doc.clear_region(self.window.page_index, slot, (rx, ry, rw, rh))
        self.float_source_cmd = self.doc.undo_stack.pop() if self.doc.undo_stack else None
        self.window.refresh_after_undo()
        self.float_pixels = pixels
        self.float_pos = (rx, ry)
        self.float_slot = slot
        self._float_grab = (x - rx, y - ry)
        self.mode = "floating"
        self.canvas.set_selection_rect(None)
        self.canvas.set_float(pixels, self.float_pos)

    def anchor_float(self):
        if self.mode != "floating" or self.float_pixels is None:
            return
        slot = getattr(self, "float_slot", None) or self.window.active_slot()
        fx, fy = self.float_pos
        self.doc.paste_pixels(self.window.page_index, slot, fx, fy, self.float_pixels)
        paste_cmd = self.doc.undo_stack.pop() if self.doc.undo_stack else None
        cmds = [c for c in (self.float_source_cmd, paste_cmd) if c is not None]
        if cmds:
            self.doc.undo_stack.append(CommandGroup(cmds) if len(cmds) > 1 else cmds[0])
            self.doc.redo_stack.clear()
        self.float_pixels = None
        self.float_source_cmd = None
        self.canvas.set_float(None, None)
        self.mode = "idle"
        self.window.after_edit()

    def cancel_float(self):
        if self.mode != "floating":
            return
        if self.float_source_cmd is not None:
            self.float_source_cmd.apply(self.doc, False)
            self.float_source_cmd = None
            self.window.refresh_after_undo()
        self.float_pixels = None
        self.canvas.set_float(None, None)
        self._set_idle()

    def select_all(self):
        rows, cols = self.doc.page_shape(self.window.page_index)
        self.rect = (0, 0, cols, rows)
        self.mode = "selected"
        self.canvas.set_selection_rect(self.rect)

    def copy(self):
        slot = self._slot()
        if self.mode == "floating":
            self.window.clipboard_pixels = self.float_pixels.copy()
        elif self.mode == "selected" and self.rect and slot:
            self.window.clipboard_pixels = self.doc.copy_region(
                self.window.page_index, slot, _int_rect(self.rect))
        else:
            return
        self.window.flash_status("Selection copied")

    def cut(self):
        slot = self._slot()
        if self.mode == "selected" and self.rect and slot:
            self.window.clipboard_pixels = self.doc.copy_region(
                self.window.page_index, slot, _int_rect(self.rect))
            self.doc.clear_region(self.window.page_index, slot, _int_rect(self.rect))
            self.window.after_edit()
            self.window.flash_status("Selection cut")

    def paste(self):
        pixels = self.window.clipboard_pixels
        if pixels is None:
            self.window.flash_status("Clipboard is empty")
            return
        self.anchor_float()
        view_center = self.canvas.visible_center()
        h, w = pixels.shape
        self.float_pixels = pixels.copy()
        self.float_pos = (round(view_center.x() - w / 2), round(view_center.y() - h / 2))
        self.float_slot = self.window.active_slot()
        self.float_source_cmd = None
        self.mode = "floating"
        self.canvas.set_float(self.float_pixels, self.float_pos)
        self.window.flash_status("Pasted — drag to position, Esc cancels, click outside anchors")

    def delete_selection(self):
        slot = self._slot()
        if self.mode == "floating":
            self.cancel_float() if self.float_source_cmd is None else self._delete_lifted()
        elif self.mode == "selected" and self.rect and slot:
            self.doc.clear_region(self.window.page_index, slot, _int_rect(self.rect))
            self.window.after_edit()
            self._set_idle()

    def _delete_lifted(self):
        # lifted pixels vanish; keep only the source clear as the undo step
        if self.float_source_cmd is not None:
            self.doc.undo_stack.append(self.float_source_cmd)
            self.doc.redo_stack.clear()
            self.float_source_cmd = None
        self.float_pixels = None
        self.canvas.set_float(None, None)
        self._set_idle()
        self.window.after_edit()

    def key_press(self, event) -> bool:
        key = event.key()
        if self.mode == "floating":
            step = 10 if event.modifiers() & Qt.KeyboardModifier.ShiftModifier else 1
            moves = {Qt.Key.Key_Left: (-step, 0), Qt.Key.Key_Right: (step, 0),
                     Qt.Key.Key_Up: (0, -step), Qt.Key.Key_Down: (0, step)}
            if key in moves:
                dx, dy = moves[key]
                self.float_pos = (self.float_pos[0] + dx, self.float_pos[1] + dy)
                self.canvas.set_float(self.float_pixels, self.float_pos)
                return True
            if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                self.anchor_float()
                return True
            if key == Qt.Key.Key_Escape:
                self.cancel_float()
                return True
        if key == Qt.Key.Key_Escape and self.mode != "idle":
            self._set_idle()
            return True
        if key == Qt.Key.Key_Delete and self.mode in ("selected", "floating"):
            self.delete_selection()
            return True
        return False


class CropTool(Tool):
    name = "crop"
    status_hint = ("Drag the region to keep, then press Enter "
                   "(page size is fixed by the device — outside ink is cleared).")

    def __init__(self, window):
        super().__init__(window)
        self.rect = None
        self._press = None

    def deactivate(self):
        self.rect = None
        self.canvas.set_crop_rect(None)

    def mouse_press(self, pos, event):
        self._press = (pos.x(), pos.y())
        self.canvas.set_crop_rect((pos.x(), pos.y(), 0, 0))

    def mouse_move(self, pos, event, pressed):
        if pressed and self._press:
            px, py = self._press
            self.rect = _norm_rect(px, py, pos.x(), pos.y())
            self.canvas.set_crop_rect(self.rect)

    def mouse_release(self, pos, event):
        self._press = None
        if self.rect and self.rect[2] >= 4 and self.rect[3] >= 4:
            self.window.flash_status("Press Enter to crop to the region (Esc cancels)")

    def key_press(self, event) -> bool:
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and self.rect:
            self.window.perform_crop(_int_rect(self.rect))
            self.rect = None
            self.canvas.set_crop_rect(None)
            return True
        if event.key() == Qt.Key.Key_Escape:
            self.rect = None
            self.canvas.set_crop_rect(None)
            return True
        return False


class TextTool(Tool):
    name = "text"
    status_hint = ("Click to place a text box; click a text box to edit it. "
                   "Drag to move. Del removes the selected box.")

    def cursor(self):
        return QCursor(Qt.CursorShape.IBeamCursor)

    def mouse_press(self, pos, event):
        item = self.canvas.text_item_at(pos)
        if item is not None:
            self.canvas.begin_text_edit(item)
            return
        slot = self.window.active_slot()
        if slot is None:
            self.window.flash_status("No editable ink layer selected for text")
            return
        obj = TextObject(
            page_index=self.window.page_index, slot=slot,
            x=pos.x(), y=pos.y(), text="",
            family=self.window.text_family(), point_size=self.window.text_size(),
            code=self.window.pen_code(),
        )
        obj = self.doc.add_text(obj)
        self.canvas.sync_text_items()
        self.canvas.begin_text_edit_by_uid(obj.uid)
        self.window.after_edit()


class PanTool(Tool):
    name = "pan"
    status_hint = "Drag to pan. Middle mouse or Space also pans in any tool."

    def cursor(self):
        return QCursor(Qt.CursorShape.OpenHandCursor)


def _norm_rect(x0, y0, x1, y1):
    x, y = min(x0, x1), min(y0, y1)
    return (x, y, abs(x1 - x0), abs(y1 - y0))


def _int_rect(rect):
    x, y, w, h = rect
    return (int(round(x)), int(round(y)), int(round(w)), int(round(h)))


def _contains(rect, x, y):
    rx, ry, rw, rh = rect
    return rx <= x < rx + rw and ry <= y < ry + rh
