"""Page canvas: QGraphicsView over layer images in raster coordinates."""
from __future__ import annotations

import numpy as np
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QFont, QImage, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QGraphicsItem,
    QGraphicsPixmapItem,
    QGraphicsRectItem,
    QGraphicsScene,
    QGraphicsTextItem,
    QGraphicsView,
)

from snformat.constants import DISPLAY_TRANSPARENT
from snformat.render import layer_display, make_lut


class LayerItem(QGraphicsItem):
    """One layer as a mutable ARGB32 image painted directly."""

    def __init__(self, cols: int, rows: int):
        super().__init__()
        self.cols, self.rows = cols, rows
        self.image = QImage(cols, rows, QImage.Format_ARGB32)
        self.image.fill(0)
        stride = self.image.bytesPerLine()
        buf = np.frombuffer(self.image.bits(), dtype=np.uint8)
        self.view = buf.reshape(rows, stride)[:, :cols * 4].reshape(rows, cols, 4)

    def boundingRect(self) -> QRectF:
        return QRectF(0, 0, self.cols, self.rows)

    def paint(self, painter: QPainter, option, widget=None):
        exposed = option.exposedRect.toAlignedRect().adjusted(-1, -1, 1, 1)
        painter.drawImage(exposed, self.image, exposed)

    def write_region(self, x: int, y: int, gray: np.ndarray, alpha: np.ndarray):
        h, w = gray.shape
        region = self.view[y:y + h, x:x + w]
        region[..., 0] = gray
        region[..., 1] = gray
        region[..., 2] = gray
        region[..., 3] = alpha
        self.update(QRectF(x, y, w, h))


class TextItem(QGraphicsTextItem):
    def __init__(self, canvas: "PageCanvas", uid: int):
        super().__init__()
        self.canvas = canvas
        self.uid = uid
        self._moved = False
        self.setDefaultTextColor(QColor(0, 0, 139))

    def focusOutEvent(self, event):
        super().focusOutEvent(event)
        self.canvas.commit_text_edit(self)

    def mouseMoveEvent(self, event):
        self._moved = True
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        super().mouseReleaseEvent(event)
        if self._moved:
            self._moved = False
            self.canvas.commit_text_move(self)


class PageCanvas(QGraphicsView):
    PAGE_MARGIN = 60

    def __init__(self, window):
        super().__init__()
        self.window = window
        self.setScene(QGraphicsScene(self))
        self.setBackgroundBrush(QBrush(QColor(56, 58, 62)))
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setMouseTracking(True)
        self.setTabletTracking(True)
        self.layer_items: dict[str, LayerItem] = {}
        self.text_items: dict[int, TextItem] = {}
        self.paper_item = None
        self.selection_item = None
        self.crop_item = None
        self.float_item = None
        self.visibility_override: dict[str, bool] = {}
        self._space_pan = False
        self._panning = False
        self._pan_last = None
        self._mouse_down = False
        self._editing_item: TextItem | None = None
        self._page_rows = 0
        self._page_cols = 0

    # ------------------------------------------------ document / page setup
    @property
    def doc(self):
        return self.window.doc

    def rebuild_page(self):
        """Recreate all items for the current page."""
        scene = self.scene()
        scene.clear()
        self.layer_items = {}
        self.text_items = {}
        self.selection_item = None
        self.crop_item = None
        self.float_item = None
        self._editing_item = None
        if self.doc is None:
            return
        page = self.doc.nb.pages[self.window.page_index]
        rows, cols = self.doc.page_shape(self.window.page_index)
        self._page_rows, self._page_cols = rows, cols

        paper = QGraphicsRectItem(0, 0, cols, rows)
        paper.setBrush(QBrush(QColor(255, 255, 255)))
        paper.setPen(QPen(Qt.PenStyle.NoPen))
        paper.setZValue(-100)
        scene.addItem(paper)
        shadow = QGraphicsRectItem(8, 8, cols, rows)
        shadow.setBrush(QBrush(QColor(0, 0, 0, 70)))
        shadow.setPen(QPen(Qt.PenStyle.NoPen))
        shadow.setZValue(-101)
        scene.addItem(shadow)

        order = page.layer_order()          # topmost first
        for depth, slot in enumerate(order):
            item = LayerItem(cols, rows)
            item.setZValue(len(order) - depth)
            scene.addItem(item)
            self.layer_items[slot] = item
            self._repaint_layer(slot)
        self.sync_text_items()
        scene.setSceneRect(QRectF(-self.PAGE_MARGIN, -self.PAGE_MARGIN,
                                  cols + 2 * self.PAGE_MARGIN, rows + 2 * self.PAGE_MARGIN))
        self.apply_visibility()

    def _repaint_layer(self, slot: str):
        item = self.layer_items.get(slot)
        if item is None:
            return
        page = self.doc.nb.pages[self.window.page_index]
        rendered = layer_display(self.doc.nb, page, slot)
        if rendered is None:
            item.image.fill(0)
            item.update()
            return
        gray, mask = rendered
        item.write_region(0, 0, gray, np.where(mask, 255, 0).astype(np.uint8))

    def refresh_layer_rect(self, slot: str, rect):
        x, y, w, h = rect
        if w <= 0 or h <= 0:
            return
        item = self.layer_items.get(slot)
        if item is None:
            return
        raster = self.doc.raster(self.window.page_index, slot)
        lut = make_lut(self.doc.nb.is_highres)
        sub = raster[y:y + h, x:x + w]
        gray = lut[sub]
        alpha = np.where(gray != DISPLAY_TRANSPARENT, 255, 0).astype(np.uint8)
        item.write_region(x, y, gray, alpha)

    def refresh_page_full(self):
        for slot in self.layer_items:
            self._repaint_layer(slot)
        self.sync_text_items()

    def apply_visibility(self):
        if self.doc is None:
            return
        page = self.doc.nb.pages[self.window.page_index]
        vis = page.visibility()
        for slot, item in self.layer_items.items():
            v = self.visibility_override.get(slot, vis.get(slot, True))
            item.setVisible(v)

    # ------------------------------------------------ zoom
    def zoom_by(self, factor: float):
        current = self.transform().m11()
        target = max(0.05, min(16.0, current * factor))
        self.setTransform(self.transform().fromScale(1, 1).scale(target, target))
        self._after_zoom()

    def zoom_fit(self):
        self.fitInView(QRectF(0, 0, self._page_cols, self._page_rows),
                       Qt.AspectRatioMode.KeepAspectRatio)
        self._after_zoom()

    def zoom_100(self):
        self.resetTransform()
        self._after_zoom()

    def _after_zoom(self):
        smooth = self.transform().m11() < 1.0
        self.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, smooth)
        self.window.update_zoom_label(self.transform().m11())

    def visible_center(self) -> QPointF:
        return self.mapToScene(self.viewport().rect().center())

    # ------------------------------------------------ overlays
    def set_selection_rect(self, rect):
        if self.selection_item is None and rect is not None:
            self.selection_item = QGraphicsRectItem()
            pen = QPen(QColor(0, 120, 215), 0, Qt.PenStyle.DashLine)
            self.selection_item.setPen(pen)
            self.selection_item.setZValue(500)
            self.scene().addItem(self.selection_item)
        if self.selection_item is not None:
            if rect is None:
                self.scene().removeItem(self.selection_item)
                self.selection_item = None
            else:
                self.selection_item.setRect(QRectF(*rect))

    def set_crop_rect(self, rect):
        if self.crop_item is None and rect is not None:
            self.crop_item = QGraphicsRectItem()
            pen = QPen(QColor(200, 60, 40), 0, Qt.PenStyle.DashDotLine)
            self.crop_item.setPen(pen)
            self.crop_item.setBrush(QBrush(QColor(200, 60, 40, 26)))
            self.crop_item.setZValue(500)
            self.scene().addItem(self.crop_item)
        if self.crop_item is not None:
            if rect is None:
                self.scene().removeItem(self.crop_item)
                self.crop_item = None
            else:
                self.crop_item.setRect(QRectF(*rect))

    def set_float(self, pixels: np.ndarray | None, pos):
        if pixels is None:
            if self.float_item is not None:
                self.scene().removeItem(self.float_item)
                self.float_item = None
            return
        if self.float_item is None:
            self.float_item = QGraphicsPixmapItem()
            self.float_item.setZValue(600)
            self.scene().addItem(self.float_item)
            border = QGraphicsRectItem(self.float_item)
            border.setPen(QPen(QColor(0, 120, 215), 0, Qt.PenStyle.DashLine))
            h, w = pixels.shape
            border.setRect(0, 0, w, h)
            lut = make_lut(self.doc.nb.is_highres)
            gray = lut[pixels]
            alpha = np.where(gray != DISPLAY_TRANSPARENT, 255, 0).astype(np.uint8)
            h, w = gray.shape
            rgba = np.zeros((h, w, 4), dtype=np.uint8)
            rgba[..., 0] = gray
            rgba[..., 1] = gray
            rgba[..., 2] = gray
            rgba[..., 3] = alpha
            img = QImage(rgba.tobytes(), w, h, w * 4, QImage.Format_ARGB32)
            self.float_item.setPixmap(QPixmap.fromImage(img))
        self.float_item.setPos(QPointF(pos[0], pos[1]))

    # ------------------------------------------------ text items
    def sync_text_items(self):
        if self.doc is None:
            return
        wanted = {t.uid: t for t in self.doc.texts_on_page(self.window.page_index)}
        for uid in list(self.text_items):
            if uid not in wanted:
                item = self.text_items.pop(uid)
                if item is self._editing_item:
                    self._editing_item = None
                self.scene().removeItem(item)
        from .textburn import make_font
        for uid, obj in wanted.items():
            item = self.text_items.get(uid)
            if item is None:
                item = TextItem(self, uid)
                item.setZValue(400)
                self.scene().addItem(item)
                self.text_items[uid] = item
            if item.toPlainText() != obj.text:
                item.setPlainText(obj.text)
            item.setFont(make_font(obj))
            item.setDefaultTextColor(self._text_color(obj))
            if item.pos() != QPointF(obj.x, obj.y):
                item.setPos(QPointF(obj.x, obj.y))
        self._update_text_interactivity()

    def _text_color(self, obj):
        gray = int(make_lut(self.doc.nb.is_highres)[obj.code])
        if gray >= 0xFE:
            return QColor(230, 230, 235)
        return QColor(gray, gray, gray)

    def _update_text_interactivity(self):
        text_tool = self.window.current_tool_name() == "text"
        for item in self.text_items.values():
            movable = text_tool and item is not self._editing_item
            item.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, movable)
            item.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsFocusable, text_tool)
            item.setAcceptedMouseButtons(
                Qt.MouseButton.LeftButton if text_tool else Qt.MouseButton.NoButton)

    def text_item_at(self, pos: QPointF) -> TextItem | None:
        for item in self.scene().items(pos):
            if isinstance(item, TextItem):
                return item
        return None

    def begin_text_edit_by_uid(self, uid: int):
        item = self.text_items.get(uid)
        if item is not None:
            self.begin_text_edit(item)

    def begin_text_edit(self, item: TextItem):
        if self._editing_item is item:
            return
        self.end_text_edit()
        self._editing_item = item
        item.setTextInteractionFlags(Qt.TextInteractionFlag.TextEditorInteraction)
        item.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, False)
        item.setFocus()

    def end_text_edit(self):
        if self._editing_item is not None:
            self._editing_item.clearFocus()

    def commit_text_edit(self, item: TextItem):
        if item is not self._editing_item:
            return
        self._editing_item = None
        item.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        obj = self.doc.text_objects.get(item.uid)
        if obj is None:
            return
        new_text = item.toPlainText()
        if not new_text.strip():
            self.doc.remove_text(item.uid)
        elif new_text != obj.text:
            self.doc.modify_text(item.uid, text=new_text)
        self.sync_text_items()
        self.window.after_edit()

    def commit_text_move(self, item: TextItem):
        obj = self.doc.text_objects.get(item.uid)
        if obj is None:
            return
        pos = item.pos()
        if (pos.x(), pos.y()) != (obj.x, obj.y):
            self.doc.modify_text(item.uid, x=pos.x(), y=pos.y())
            self.window.after_edit()

    def delete_selected_text(self) -> bool:
        if self.window.current_tool_name() != "text" or self._editing_item is not None:
            return False
        for item in self.scene().selectedItems():
            if isinstance(item, TextItem):
                self.doc.remove_text(item.uid)
                self.sync_text_items()
                self.window.after_edit()
                return True
        return False

    # ------------------------------------------------ input routing
    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Space and not event.isAutoRepeat() \
                and self._editing_item is None:
            self._space_pan = True
            self.viewport().setCursor(Qt.CursorShape.OpenHandCursor)
            event.accept()
            return
        if self._editing_item is not None:
            super().keyPressEvent(event)
            return
        tool = self.window.current_tool()
        if tool is not None and tool.key_press(event):
            return
        super().keyPressEvent(event)

    def keyReleaseEvent(self, event):
        if event.key() == Qt.Key.Key_Space and not event.isAutoRepeat():
            self._space_pan = False
            self._apply_tool_cursor()
        super().keyReleaseEvent(event)

    def _apply_tool_cursor(self):
        tool = self.window.current_tool()
        if tool is not None:
            self.viewport().setCursor(tool.cursor())

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.MiddleButton or \
                (self._space_pan and event.button() == Qt.MouseButton.LeftButton) or \
                self.window.current_tool_name() == "pan":
            self._panning = True
            self._pan_last = event.position()
            self.viewport().setCursor(Qt.CursorShape.ClosedHandCursor)
            event.accept()
            return
        if event.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return
        pos = self.mapToScene(event.position().toPoint())
        # active inline text editing gets default interaction inside its item
        if self._editing_item is not None:
            hit = self.text_item_at(pos)
            if hit is self._editing_item:
                super().mousePressEvent(event)
                return
            self.end_text_edit()
        if self.window.current_tool_name() == "text":
            hit = self.text_item_at(pos)
            if hit is not None and self.window.current_tool() is not None:
                super().mousePressEvent(event)   # let the item handle move/click
                self._mouse_down = True
                return
        self._mouse_down = True
        tool = self.window.current_tool()
        if tool is not None:
            tool.mouse_press(pos, event)
        event.accept()

    def mouseMoveEvent(self, event):
        if self._panning and self._pan_last is not None:
            delta = event.position() - self._pan_last
            self._pan_last = event.position()
            self.horizontalScrollBar().setValue(
                self.horizontalScrollBar().value() - int(delta.x()))
            self.verticalScrollBar().setValue(
                self.verticalScrollBar().value() - int(delta.y()))
            event.accept()
            return
        pos = self.mapToScene(event.position().toPoint())
        self.window.update_cursor_label(pos)
        if self._editing_item is not None or \
                (self.window.current_tool_name() == "text" and
                 self.scene().mouseGrabberItem() is not None):
            super().mouseMoveEvent(event)
            return
        tool = self.window.current_tool()
        if tool is not None:
            tool.mouse_move(pos, event, self._mouse_down)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._panning:
            self._panning = False
            self._apply_tool_cursor()
            event.accept()
            return
        if self.scene().mouseGrabberItem() is not None:
            super().mouseReleaseEvent(event)
            self._mouse_down = False
            return
        pos = self.mapToScene(event.position().toPoint())
        if self._mouse_down:
            self._mouse_down = False
            tool = self.window.current_tool()
            if tool is not None:
                tool.mouse_release(pos, event)
        super().mouseReleaseEvent(event)

    def wheelEvent(self, event):
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.zoom_by(1.25 if event.angleDelta().y() > 0 else 0.8)
            event.accept()
            return
        super().wheelEvent(event)

    def tabletEvent(self, event):
        from PySide6.QtGui import QTabletEvent
        pos = self.mapToScene(event.position().toPoint())
        tool = self.window.current_tool()
        if tool is None or self._space_pan or self.window.current_tool_name() in ("text", "pan"):
            event.ignore()   # fall back to synthesized mouse events
            return
        t = event.type()
        if t == QTabletEvent.Type.TabletPress:
            self._mouse_down = True
            tool.mouse_press(pos, event)
        elif t == QTabletEvent.Type.TabletMove:
            self.window.update_cursor_label(pos)
            tool.mouse_move(pos, event, self._mouse_down)
        elif t == QTabletEvent.Type.TabletRelease:
            if self._mouse_down:
                self._mouse_down = False
                tool.mouse_release(pos, event)
        event.accept()
