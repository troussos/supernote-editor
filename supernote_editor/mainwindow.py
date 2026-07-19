"""Main window: menus, tool palette, page/layer panels, status bar."""
from __future__ import annotations

import os

import numpy as np
from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtGui import QAction, QActionGroup, QColor, QIcon, QImage, QKeySequence, QPainter, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDockWidget,
    QFileDialog,
    QFontComboBox,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QSpinBox,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

import snformat
from snformat.constants import INK_LAYER_KEYS
from snformat.render import composite_page
from snformat.safe_io import SaveError

from .canvas import PageCanvas
from .document import Document
from .textburn import burn_text_objects, try_restore_sidecar
from . import tools as tools_mod

APP_TITLE = "Supernote Editor"

SHADES = ["Black", "Dark gray", "Gray", "White"]


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.doc: Document | None = None
        self.page_index = 0
        self.clipboard_pixels: np.ndarray | None = None
        self._tools: dict[str, tools_mod.Tool] = {}
        self._tool_name = "pen"
        self._shade = "Black"
        self._tool_sizes = {"pen": 3, "highlighter": 18, "eraser": 14}
        self._pending_thumbs: set[int] = set()

        self.canvas = PageCanvas(self)
        self.setCentralWidget(self.canvas)
        self._build_tools()
        self._build_actions()
        self._build_toolbar()
        self._build_docks()
        self._build_statusbar()

        self._thumb_timer = QTimer(self)
        self._thumb_timer.setSingleShot(True)
        self._thumb_timer.setInterval(600)
        self._thumb_timer.timeout.connect(self._refresh_pending_thumbs)

        self.setWindowTitle(APP_TITLE)
        self.resize(1280, 860)
        self._update_ui_state()

    # ------------------------------------------------------------ builders
    def _build_tools(self):
        for cls in (tools_mod.MarqueeTool, tools_mod.PenTool, tools_mod.HighlighterTool,
                    tools_mod.EraserTool, tools_mod.CropTool, tools_mod.TextTool,
                    tools_mod.PanTool):
            tool = cls(self)
            self._tools[tool.name] = tool

    def _build_actions(self):
        def act(text, slot, shortcut=None, enabled=True):
            a = QAction(text, self)
            if shortcut:
                a.setShortcut(QKeySequence(shortcut))
            a.triggered.connect(slot)
            a.setEnabled(enabled)
            self.addAction(a)
            return a

        self.act_open = act("&Open…", self.open_file_dialog, "Ctrl+O")
        self.act_save = act("&Save", self.save, "Ctrl+S", enabled=False)
        self.act_save_as = act("Save &As…", self.save_as, "Ctrl+Shift+S", enabled=False)
        self.act_export = act("Export page as PN&G…", self.export_png, enabled=False)
        self.act_verify = act("&Verify file integrity", self.verify_file, enabled=False)
        self.act_quit = act("E&xit", self.close, "Alt+F4")

        self.act_undo = act("&Undo", self.undo, "Ctrl+Z", enabled=False)
        self.act_redo = act("&Redo", self.redo, "Ctrl+Y", enabled=False)
        self.act_cut = act("Cu&t", lambda: self._marquee_op("cut"), "Ctrl+X", enabled=False)
        self.act_copy = act("&Copy", lambda: self._marquee_op("copy"), "Ctrl+C", enabled=False)
        self.act_paste = act("&Paste", lambda: self._marquee_op("paste"), "Ctrl+V", enabled=False)
        self.act_select_all = act("Select &All", lambda: self._marquee_op("select_all"), "Ctrl+A", enabled=False)
        self.act_deselect = act("&Deselect", lambda: self._marquee_op("deselect"), "Ctrl+D", enabled=False)

        self.act_prev = act("&Previous page", lambda: self.goto_page(self.page_index - 1), "PgUp", enabled=False)
        self.act_next = act("&Next page", lambda: self.goto_page(self.page_index + 1), "PgDown", enabled=False)
        self.act_dup_page = act("&Duplicate page", self.duplicate_page, enabled=False)
        self.act_blank_page = act("Add &blank page (copy layout)", self.add_blank_page, enabled=False)
        self.act_del_page = act("De&lete page…", self.delete_page, enabled=False)

        self.act_zoom_in = act("Zoom &in", lambda: self.canvas.zoom_by(1.25), "Ctrl+=", enabled=False)
        self.act_zoom_out = act("Zoom &out", lambda: self.canvas.zoom_by(0.8), "Ctrl+-", enabled=False)
        self.act_zoom_fit = act("&Fit page", self.canvas.zoom_fit, "Ctrl+0", enabled=False)
        self.act_zoom_100 = act("Zoom &100%", self.canvas.zoom_100, "Ctrl+1", enabled=False)

        # menus
        m_file = self.menuBar().addMenu("&File")
        for a in (self.act_open, self.act_save, self.act_save_as, self.act_export,
                  self.act_verify, self.act_quit):
            m_file.addAction(a)
        m_edit = self.menuBar().addMenu("&Edit")
        for a in (self.act_undo, self.act_redo, self.act_cut, self.act_copy,
                  self.act_paste, self.act_select_all, self.act_deselect):
            m_edit.addAction(a)
        m_page = self.menuBar().addMenu("&Page")
        for a in (self.act_prev, self.act_next, self.act_dup_page,
                  self.act_blank_page, self.act_del_page):
            m_page.addAction(a)
        m_view = self.menuBar().addMenu("&View")
        for a in (self.act_zoom_in, self.act_zoom_out, self.act_zoom_fit, self.act_zoom_100):
            m_view.addAction(a)
        m_help = self.menuBar().addMenu("&Help")
        m_help.addAction(act("&About / file info", self.show_about))

    def _build_toolbar(self):
        tb = QToolBar("Tools")
        tb.setMovable(False)
        tb.setIconSize(QSize(20, 20))
        self.addToolBar(tb)
        self._tool_group = QActionGroup(self)
        self._tool_actions = {}
        for name, label, key in [
            ("marquee", "Marquee", "M"), ("pen", "Pen", "P"),
            ("highlighter", "Highlighter", "G"), ("eraser", "Eraser", "E"),
            ("crop", "Crop", "C"), ("text", "Text", "T"), ("pan", "Pan", "H"),
        ]:
            a = QAction(label, self)
            a.setCheckable(True)
            a.setShortcut(key)
            a.setToolTip(f"{label} ({key})")
            a.triggered.connect(lambda checked, n=name: self.set_tool(n))
            self._tool_group.addAction(a)
            tb.addAction(a)
            self.addAction(a)
            self._tool_actions[name] = a
        self._tool_actions["pen"].setChecked(True)
        tb.addSeparator()

        self._shade_group = QActionGroup(self)
        self._shade_actions = {}
        for shade, gray in [("Black", 0), ("Dark gray", 0x9D), ("Gray", 0xC9), ("White", 0xFE)]:
            a = QAction(shade, self)
            a.setCheckable(True)
            a.setIcon(_swatch_icon(gray))
            a.triggered.connect(lambda checked, s=shade: self.set_shade(s))
            self._shade_group.addAction(a)
            tb.addAction(a)
            self._shade_actions[shade] = a
        self._shade_actions["Black"].setChecked(True)
        tb.addSeparator()

        tb.addWidget(QLabel(" Size "))
        self.size_spin = QSpinBox()
        self.size_spin.setRange(1, 60)
        self.size_spin.setValue(self._tool_sizes["pen"])
        self.size_spin.valueChanged.connect(self._size_changed)
        tb.addWidget(self.size_spin)

        # [ / ] size shortcuts
        smaller = QAction("Smaller brush", self)
        smaller.setShortcut("[")
        smaller.triggered.connect(lambda: self.size_spin.setValue(self.size_spin.value() - 1))
        self.addAction(smaller)
        bigger = QAction("Bigger brush", self)
        bigger.setShortcut("]")
        bigger.triggered.connect(lambda: self.size_spin.setValue(self.size_spin.value() + 1))
        self.addAction(bigger)

        tb.addSeparator()
        tb.addWidget(QLabel(" Font "))
        self.font_combo = QFontComboBox()
        self.font_combo.setMaximumWidth(180)
        tb.addWidget(self.font_combo)
        self.font_size_spin = QSpinBox()
        self.font_size_spin.setRange(8, 300)
        self.font_size_spin.setValue(36)
        self.font_size_spin.setToolTip("Text size in page pixels")
        tb.addWidget(self.font_size_spin)

    def _build_docks(self):
        self.page_list = QListWidget()
        self.page_list.setViewMode(QListWidget.ViewMode.IconMode)
        self.page_list.setIconSize(QSize(120, 160))
        self.page_list.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.page_list.setMovement(QListWidget.Movement.Static)
        self.page_list.setSpacing(8)
        self.page_list.currentRowChanged.connect(self._page_list_changed)
        dock = QDockWidget("Pages", self)
        dock.setWidget(self.page_list)
        dock.setFeatures(QDockWidget.DockWidgetFeature.NoDockWidgetFeatures)
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, dock)

        panel = QWidget()
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(6, 6, 6, 6)
        lay.addWidget(QLabel("Active layer"))
        self.layer_combo = QComboBox()
        self.layer_combo.currentIndexChanged.connect(lambda _: self._update_ui_state())
        lay.addWidget(self.layer_combo)
        lay.addWidget(QLabel("Visibility"))
        self.layer_list = QListWidget()
        self.layer_list.itemChanged.connect(self._layer_visibility_changed)
        lay.addWidget(self.layer_list)
        note = QLabel("Visibility is view-only;\nit is not written to the file.")
        note.setStyleSheet("color: gray;")
        lay.addWidget(note)
        dock2 = QDockWidget("Layers", self)
        dock2.setWidget(panel)
        dock2.setFeatures(QDockWidget.DockWidgetFeature.NoDockWidgetFeatures)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, dock2)

    def _build_statusbar(self):
        self.status_file = QLabel("No file open")
        self.status_pos = QLabel("")
        self.status_zoom = QLabel("100%")
        self.status_hint = QLabel("")
        sb = self.statusBar()
        sb.addWidget(self.status_file)
        sb.addWidget(self.status_hint, 1)
        sb.addPermanentWidget(self.status_pos)
        sb.addPermanentWidget(self.status_zoom)

    # ------------------------------------------------------------ tool API
    def current_tool(self) -> tools_mod.Tool | None:
        return self._tools.get(self._tool_name) if self.doc else None

    def current_tool_name(self) -> str:
        return self._tool_name

    def set_tool(self, name: str):
        old = self._tools.get(self._tool_name)
        if old is not None and self.doc is not None:
            old.deactivate()
        self._tool_name = name
        self._tool_actions[name].setChecked(True)
        tool = self._tools[name]
        if self.doc is not None:
            tool.activate()
            self.canvas.viewport().setCursor(tool.cursor())
            self.canvas._update_text_interactivity()
        if name in self._tool_sizes:
            self.size_spin.blockSignals(True)
            self.size_spin.setValue(self._tool_sizes[name])
            self.size_spin.blockSignals(False)
        self.flash_status(tool.status_hint)

    def set_shade(self, shade: str):
        self._shade = shade

    def _size_changed(self, value: int):
        if self._tool_name in self._tool_sizes:
            self._tool_sizes[self._tool_name] = value

    def tool_size(self, tool) -> int:
        return self._tool_sizes.get(tool.name, 3)

    def pen_code(self) -> int:
        hi = self.doc.nb.is_highres if self.doc else True
        return {
            "Black": 0x61,
            "Dark gray": 0x9D if hi else 0x63,
            "Gray": 0xC9 if hi else 0x64,
            "White": 0x65,
        }[self._shade]

    def marker_code(self) -> int:
        hi = self.doc.nb.is_highres if self.doc else True
        return {
            "Black": 0x66,
            "Dark gray": 0x9E if hi else 0x67,
            "Gray": 0xCA if hi else 0x68,
            "White": 0x65,
        }[self._shade]

    def text_family(self) -> str:
        return self.font_combo.currentFont().family()

    def text_size(self) -> float:
        return float(self.font_size_spin.value())

    def active_slot(self) -> str | None:
        if self.doc is None:
            return None
        slot = self.layer_combo.currentData()
        if slot and slot in self.doc.editable_slots(self.page_index):
            return slot
        slots = self.doc.editable_slots(self.page_index)
        return slots[0] if slots else None

    # ------------------------------------------------------------ file ops
    def open_file_dialog(self):
        if not self._confirm_discard():
            return
        start = os.path.dirname(self.doc.path) if self.doc else ""
        path, _ = QFileDialog.getOpenFileName(self, "Open Supernote file", start,
                                              "Supernote notes (*.note);;All files (*)")
        if path:
            self.open_path(path)

    def open_path(self, path: str):
        try:
            doc = Document.open(path)
        except Exception as e:
            QMessageBox.critical(self, APP_TITLE,
                                 f"Could not open {os.path.basename(path)}:\n\n{type(e).__name__}: {e}")
            return
        self.doc = doc
        self.page_index = 0
        doc.raster_listeners.append(self._on_raster_changed)
        doc.text_listeners.append(lambda page: self._queue_thumb(page))
        doc.structure_listeners.append(self._on_structure_changed)
        restored = try_restore_sidecar(doc)
        self._rebuild_page_list()
        self._rebuild_layer_panel()
        self.canvas.rebuild_page()
        self.canvas.zoom_fit()
        self.set_tool(self._tool_name)
        msg = f"Opened {os.path.basename(path)} — {doc.page_count} page(s), file version {doc.nb.version}"
        if restored:
            msg += f" — restored {restored} editable text box(es) from sidecar"
        self.flash_status(msg)
        if doc.nb.warnings:
            QMessageBox.warning(self, APP_TITLE, "\n\n".join(doc.nb.warnings))
        self._update_ui_state()

    def save(self):
        self._save_to(None)

    def save_as(self):
        path, _ = QFileDialog.getSaveFileName(self, "Save As", self.doc.path,
                                              "Supernote notes (*.note)")
        if path:
            self._save_to(path)

    def _save_to(self, path: str | None):
        if self.doc is None:
            return
        self.canvas.end_text_edit()
        marquee = self._tools["marquee"]
        if marquee.mode == "floating":
            marquee.anchor_float()
        try:
            warnings = self.doc.save(path, burn_text=burn_text_objects)
        except SaveError as e:
            details = "\n".join(e.problems[:12])
            QMessageBox.critical(self, APP_TITLE,
                                 f"{e}\n\n{details}\n\nYour file on disk was not modified.")
            return
        except Exception as e:
            QMessageBox.critical(self, APP_TITLE,
                                 f"Save failed: {type(e).__name__}: {e}\n\n"
                                 "Your file on disk was not modified.")
            return
        self.canvas.refresh_page_full()
        self._queue_all_thumbs()
        msg = f"Saved and verified {os.path.basename(self.doc.path)} (backup kept)"
        if warnings:
            msg += f" — {len(warnings)} warning(s)"
            QMessageBox.warning(self, APP_TITLE, "\n\n".join(warnings))
        self.flash_status(msg)
        self._update_ui_state()

    def export_png(self):
        if self.doc is None:
            return
        base = os.path.splitext(self.doc.path)[0]
        path, _ = QFileDialog.getSaveFileName(self, "Export page as PNG",
                                              f"{base}_p{self.page_index + 1}.png", "PNG (*.png)")
        if not path:
            return
        arr = composite_page(self.doc.nb, self.doc.nb.pages[self.page_index])
        img = QImage(arr.tobytes(), arr.shape[1], arr.shape[0], arr.shape[1],
                     QImage.Format_Grayscale8)
        img.save(path)
        self.flash_status(f"Exported {os.path.basename(path)}")

    def verify_file(self):
        if self.doc is None:
            return
        problems = snformat.roundtrip_check(self.doc.path)
        if problems:
            QMessageBox.warning(self, APP_TITLE,
                                "Verification notes:\n\n" + "\n".join(problems[:15]))
        else:
            QMessageBox.information(
                self, APP_TITLE,
                "File parses cleanly, every layer decodes, and an unedited "
                "rewrite reproduces the file faithfully.")

    # ------------------------------------------------------------ edit ops
    def undo(self):
        if self.doc and self.doc.undo_stack:
            cmd = self.doc.undo()
            self._jump_to_command_page(cmd)
            self.refresh_after_undo()
            self.after_edit()

    def redo(self):
        if self.doc and self.doc.redo_stack:
            cmd = self.doc.redo()
            self._jump_to_command_page(cmd)
            self.refresh_after_undo()
            self.after_edit()

    def _jump_to_command_page(self, cmd):
        page = None
        stack = [cmd]
        while stack:
            c = stack.pop()
            if hasattr(c, "commands"):
                stack.extend(c.commands)
            elif hasattr(c, "page_index"):
                page = c.page_index
                break
            elif hasattr(c, "obj"):
                page = c.obj.page_index
                break
        if page is not None and page != self.page_index and page < self.doc.page_count:
            self.goto_page(page)

    def refresh_after_undo(self):
        self.canvas.refresh_page_full()

    def _marquee_op(self, op: str):
        if self.doc is None:
            return
        if op in ("copy", "cut", "paste") and self._tool_name == "text" \
                and self.canvas._editing_item is not None:
            return   # let the text editor own clipboard keys
        if op == "deselect":
            self._tools["marquee"].deactivate()
            return
        self.set_tool("marquee")
        marquee = self._tools["marquee"]
        getattr(marquee, op)()

    def perform_crop(self, rect):
        if self.doc is None:
            return
        slots = self.doc.editable_slots(self.page_index)
        if not slots:
            return
        if len(slots) > 1:
            box = QMessageBox(self)
            box.setWindowTitle(APP_TITLE)
            box.setText("Crop which layers? Ink outside the region is cleared "
                        "(page size is fixed by the device).")
            all_btn = box.addButton("All ink layers", QMessageBox.ButtonRole.AcceptRole)
            box.addButton("Active layer only", QMessageBox.ButtonRole.AcceptRole)
            box.addButton(QMessageBox.StandardButton.Cancel)
            box.exec()
            if box.clickedButton() == all_btn:
                chosen = slots
            elif box.result() == QMessageBox.StandardButton.Cancel:
                return
            else:
                chosen = [self.active_slot()]
        else:
            chosen = slots
        self.doc.crop_to_region(self.page_index, rect, chosen)
        self.canvas.refresh_page_full()
        self.after_edit()
        self.flash_status("Cropped — ink outside the region cleared")

    # ------------------------------------------------------------ pages
    def goto_page(self, index: int):
        if self.doc is None or not (0 <= index < self.doc.page_count):
            return
        for tool in self._tools.values():
            tool.deactivate()
        self.page_index = index
        self.canvas.rebuild_page()
        self._rebuild_layer_panel()
        self.page_list.blockSignals(True)
        self.page_list.setCurrentRow(index)
        self.page_list.blockSignals(False)
        self._update_ui_state()

    def _page_list_changed(self, row: int):
        if row >= 0 and self.doc is not None and row != self.page_index:
            self.goto_page(row)

    def duplicate_page(self):
        if self.doc is None:
            return
        self.doc.duplicate_page(self.page_index)
        self.goto_page(self.page_index + 1)
        self.flash_status("Page duplicated (undo history was cleared by this structural change)")

    def add_blank_page(self):
        if self.doc is None:
            return
        self.doc.duplicate_page(self.page_index)
        new_index = self.page_index + 1
        for slot in self.doc.editable_slots(new_index):
            raster = self.doc.raster(new_index, slot)
            if (raster != 0x62).any():
                raster[:] = 0x62
                self.doc.nb.mark_ink_edited(self.doc.nb.pages[new_index], slot)
        from snformat.edit_ops import sync_page_after_ink_edit
        sync_page_after_ink_edit(self.doc.nb.pages[new_index])
        self.doc._reset_undo("blank page added")
        self.goto_page(new_index)
        self.flash_status("Blank page added (same template/layout as current page)")

    def delete_page(self):
        if self.doc is None or self.doc.page_count <= 1:
            QMessageBox.information(self, APP_TITLE, "A notebook must keep at least one page.")
            return
        ret = QMessageBox.question(
            self, APP_TITLE,
            f"Delete page {self.page_index + 1} of {self.doc.page_count}?\n"
            "This clears the undo history.")
        if ret != QMessageBox.StandardButton.Yes:
            return
        warnings = self.doc.delete_page(self.page_index)
        self.goto_page(min(self.page_index, self.doc.page_count - 1))
        if warnings:
            QMessageBox.information(self, APP_TITLE, "\n".join(warnings[:10]))
        self.flash_status("Page deleted")

    def _on_structure_changed(self):
        self._rebuild_page_list()
        self._update_ui_state()

    # ------------------------------------------------------------ panels
    def _rebuild_page_list(self):
        self.page_list.blockSignals(True)
        self.page_list.clear()
        for i in range(self.doc.page_count):
            item = QListWidgetItem(self._thumb_icon(i), f"Page {i + 1}")
            self.page_list.addItem(item)
        self.page_list.setCurrentRow(self.page_index)
        self.page_list.blockSignals(False)

    def _thumb_icon(self, index: int) -> QIcon:
        arr = composite_page(self.doc.nb, self.doc.nb.pages[index])
        img = QImage(arr.tobytes(), arr.shape[1], arr.shape[0], arr.shape[1],
                     QImage.Format_Grayscale8)
        pm = QPixmap.fromImage(img).scaled(
            120, 160, Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation)
        return QIcon(pm)

    def _queue_thumb(self, page_index: int):
        self._pending_thumbs.add(page_index)
        self._thumb_timer.start()

    def _queue_all_thumbs(self):
        self._pending_thumbs.update(range(self.doc.page_count))
        self._thumb_timer.start()

    def _refresh_pending_thumbs(self):
        if self.doc is None:
            return
        for i in sorted(self._pending_thumbs):
            if i < self.doc.page_count and i < self.page_list.count():
                self.page_list.item(i).setIcon(self._thumb_icon(i))
        self._pending_thumbs.clear()

    def _rebuild_layer_panel(self):
        page = self.doc.nb.pages[self.page_index]
        vis = page.visibility()
        self.layer_combo.blockSignals(True)
        self.layer_combo.clear()
        for slot in INK_LAYER_KEYS:
            if slot in page.layers and page.layers[slot].is_editable():
                self.layer_combo.addItem(slot, slot)
        self.layer_combo.blockSignals(False)
        self.layer_list.blockSignals(True)
        self.layer_list.clear()
        for slot in page.layer_order():
            item = QListWidgetItem(slot)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            checked = self.canvas.visibility_override.get(slot, vis.get(slot, True))
            item.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
            item.setData(Qt.ItemDataRole.UserRole, slot)
            self.layer_list.addItem(item)
        self.layer_list.blockSignals(False)

    def _layer_visibility_changed(self, item: QListWidgetItem):
        slot = item.data(Qt.ItemDataRole.UserRole)
        self.canvas.visibility_override[slot] = item.checkState() == Qt.CheckState.Checked
        self.canvas.apply_visibility()

    # ------------------------------------------------------------ misc UI
    def _on_raster_changed(self, page_index: int, slot: str, rect):
        if page_index == self.page_index:
            if rect is None:
                self.canvas.refresh_page_full()
            else:
                self.canvas.refresh_layer_rect(slot, rect)
        self._queue_thumb(page_index)

    def after_edit(self):
        self._queue_thumb(self.page_index)
        self._update_ui_state()

    def flash_status(self, text: str):
        self.status_hint.setText(text)

    def update_zoom_label(self, scale: float):
        self.status_zoom.setText(f"{scale * 100:.0f}%")

    def update_cursor_label(self, pos):
        if self.doc is not None:
            self.status_pos.setText(f"{int(pos.x())}, {int(pos.y())}")

    def _update_ui_state(self):
        has_doc = self.doc is not None
        modified = has_doc and self.doc.is_modified()
        for a in (self.act_save, self.act_save_as, self.act_export, self.act_verify,
                  self.act_select_all, self.act_deselect, self.act_cut, self.act_copy,
                  self.act_paste, self.act_dup_page, self.act_blank_page, self.act_del_page,
                  self.act_zoom_in, self.act_zoom_out, self.act_zoom_fit, self.act_zoom_100):
            a.setEnabled(has_doc)
        self.act_undo.setEnabled(has_doc and bool(self.doc.undo_stack))
        self.act_redo.setEnabled(has_doc and bool(self.doc.redo_stack))
        self.act_prev.setEnabled(has_doc and self.page_index > 0)
        self.act_next.setEnabled(has_doc and self.page_index < self.doc.page_count - 1)
        if has_doc:
            name = os.path.basename(self.doc.path)
            star = "*" if modified else ""
            self.setWindowTitle(f"{star}{name} — {APP_TITLE}")
            self.status_file.setText(
                f"{star}{name}  |  page {self.page_index + 1}/{self.doc.page_count}"
                f"  |  v{self.doc.nb.version} {self.doc.nb.equipment}")
        else:
            self.setWindowTitle(APP_TITLE)
            self.status_file.setText("No file open — File > Open a .note")

    def show_about(self):
        info = ""
        if self.doc is not None:
            nb = self.doc.nb
            w, h = nb.page_size()
            info = (f"\n\nOpen file: {os.path.basename(self.doc.path)}"
                    f"\nSignature: {nb.signature}\nEquipment: {nb.equipment}"
                    f"\nPage size: {w} x {h}\nPages: {nb.get_total_pages() if hasattr(nb, 'get_total_pages') else len(nb.pages)}")
        QMessageBox.information(
            self, "About",
            "Supernote Editor — native .note reading and writing.\n"
            "Format support reverse-engineered; every save is re-parsed and "
            "verified before touching your file, and a timestamped .bak backup "
            "is kept next to it." + info)

    # ------------------------------------------------------------ closing
    def _confirm_discard(self) -> bool:
        if self.doc is None or not self.doc.is_modified():
            return True
        ret = QMessageBox.question(
            self, APP_TITLE, "Save changes to the current file?",
            QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard
            | QMessageBox.StandardButton.Cancel)
        if ret == QMessageBox.StandardButton.Save:
            self.save()
            return self.doc is None or not self.doc.is_modified()
        return ret == QMessageBox.StandardButton.Discard

    def closeEvent(self, event):
        if self._confirm_discard():
            event.accept()
        else:
            event.ignore()


def _swatch_icon(gray: int) -> QIcon:
    pm = QPixmap(18, 18)
    pm.fill(QColor(120, 120, 120))
    p = QPainter(pm)
    p.fillRect(1, 1, 16, 16, QColor(gray, gray, gray))
    p.end()
    return QIcon(pm)
