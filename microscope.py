"""USB microscope viewer: one window with live view, camera settings, zoom, snapshots and recording.

Shortcuts: Space snapshot, R record, +/- zoom, 0 fit, F full screen, Ctrl+Q quit.
Mouse: wheel zooms at the cursor, drag pans, double-click resets zoom.
"""
import argparse
import glob
import sys
import time
from pathlib import Path

import cv2
from PySide6.QtCore import QPointF, Qt, QThread, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QImage, QKeySequence, QPainter, QShortcut
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFileDialog, QFormLayout, QGroupBox, QHBoxLayout, QLabel,
    QMainWindow, QPushButton, QScrollArea, QSlider, QSplitter, QVBoxLayout, QWidget,
)

from v4l2 import TYPE_BOOL, TYPE_MENU, V4L2Device

MAX_ZOOM = 8.0
MICROSCOPE_USB_ID = "05e3:f12b"


def list_cameras():
    """Video capture nodes as (path, label, usb_id). Skips metadata nodes (index > 0)."""
    cameras = []
    for node in sorted(glob.glob("/sys/class/video4linux/video*"), key=lambda p: int(p.rsplit("video", 1)[1])):
        node = Path(node)
        if (node / "index").read_text().strip() != "0":
            continue
        usb = node / "device" / ".."
        try:
            usb_id = f"{(usb / 'idVendor').read_text().strip()}:{(usb / 'idProduct').read_text().strip()}"
        except OSError:
            usb_id = ""
        path = f"/dev/{node.name}"
        cameras.append((path, f"{(node / 'name').read_text().split(':')[0].strip()}  ({path})", usb_id))
    return cameras


class CameraThread(QThread):
    """Reads frames from the camera and emits them as BGR arrays."""

    frame_ready = Signal(object)
    failed = Signal(str)

    def __init__(self, device):
        super().__init__()
        self.device = device
        self.running = True

    def run(self):
        cap = cv2.VideoCapture(self.device, cv2.CAP_V4L2)
        if not cap.isOpened():
            self.failed.emit(f"Cannot open {self.device}")
            return
        while self.running:
            ok, frame = cap.read()
            if not ok:
                self.failed.emit("Camera disconnected")
                break
            self.frame_ready.emit(frame)
        cap.release()

    def stop(self):
        self.running = False
        self.wait(2000)


class VideoView(QWidget):
    """Shows the frame with digital zoom. Wheel zooms at the cursor, drag pans."""

    zoom_changed = Signal(float)

    def __init__(self):
        super().__init__()
        self.setMinimumSize(320, 240)
        self.setAttribute(Qt.WA_OpaquePaintEvent)
        self.image = None
        self.zoom = 1.0
        self.center = QPointF(0.5, 0.5)  # Visible centre, in frame fractions.
        self.drag_from = None

    def set_frame(self, frame_bgr):
        h, w = frame_bgr.shape[:2]
        rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
        self.image = QImage(rgb.data, w, h, 3 * w, QImage.Format_RGB888).copy()
        self.update()

    def clear(self):
        self.image = None
        self.update()

    def set_zoom(self, zoom, anchor=None):
        zoom = min(MAX_ZOOM, max(1.0, zoom))
        if anchor is not None:  # Keep the frame point under the cursor fixed.
            self.center += (anchor - self.center) * (1 - self.zoom / zoom)
        self.zoom = zoom
        self._clamp()
        self.update()
        self.zoom_changed.emit(zoom)

    def crop(self):
        """Visible region as (x, y, w, h) in frame fractions."""
        size = 1 / self.zoom
        return self.center.x() - size / 2, self.center.y() - size / 2, size, size

    def crop_pixels(self, w, h):
        x, y, cw, ch = self.crop()
        return int(x * w), int(y * h), max(1, int(cw * w)), max(1, int(ch * h))

    def _clamp(self):
        half = 0.5 / self.zoom
        self.center = QPointF(min(1 - half, max(half, self.center.x())), min(1 - half, max(half, self.center.y())))

    def _target(self):
        """Letterboxed rectangle the image is drawn into."""
        iw, ih = self.image.width(), self.image.height()
        scale = min(self.width() / iw, self.height() / ih)
        w, h = iw * scale, ih * scale
        return (self.width() - w) / 2, (self.height() - h) / 2, w, h

    def _to_frame(self, pos):
        x, y, w, h = self._target()
        cx, cy, cw, ch = self.crop()
        return QPointF(cx + (pos.x() - x) / w * cw, cy + (pos.y() - y) / h * ch)

    def paintEvent(self, _event):
        p = QPainter(self)
        p.fillRect(self.rect(), Qt.black)
        if self.image is None:
            p.setPen(Qt.gray)
            p.drawText(self.rect(), Qt.AlignCenter, "Waiting for camera…")
            return
        x, y, w, h = self._target()
        sx, sy, sw, sh = self.crop_pixels(self.image.width(), self.image.height())
        part = self.image.copy(sx, sy, sw, sh).scaled(int(w), int(h), Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
        p.drawImage(QPointF(x, y), part)

    def wheelEvent(self, e):
        if self.image is not None:
            factor = 1.15 if e.angleDelta().y() > 0 else 1 / 1.15
            self.set_zoom(self.zoom * factor, self._to_frame(e.position()))

    def mousePressEvent(self, e):
        self.drag_from = e.position()
        self.setCursor(Qt.ClosedHandCursor)

    def mouseMoveEvent(self, e):
        if self.drag_from is None or self.image is None:
            return
        _, _, w, h = self._target()
        d = e.position() - self.drag_from
        self.drag_from = e.position()
        self.center -= QPointF(d.x() / w / self.zoom, d.y() / h / self.zoom)
        self._clamp()
        self.update()

    def mouseReleaseEvent(self, _e):
        self.drag_from = None
        self.unsetCursor()

    def mouseDoubleClickEvent(self, _e):
        self.set_zoom(1.0)


class CameraSettings(QGroupBox):
    """One widget per hardware control, built from what the device reports."""

    def __init__(self, device: V4L2Device):
        super().__init__("Camera")
        self.device = device
        self.widgets = {}
        form = QFormLayout(self)
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        for c in device.controls:
            value = device.get(c.id)
            if c.type == TYPE_BOOL:
                w = QCheckBox()
                w.setChecked(bool(value))
                w.toggled.connect(lambda v, c=c: self._set(c, int(v)))
                form.addRow(c.name, w)
            elif c.type == TYPE_MENU:
                w = QComboBox()
                for idx, name in c.menu.items():
                    w.addItem(name, idx)
                w.setCurrentIndex(max(0, w.findData(value)))
                w.currentIndexChanged.connect(lambda _i, c=c, w=w: self._set(c, w.currentData()))
                form.addRow(c.name, w)
            else:
                w = QSlider(Qt.Horizontal)
                w.setRange(c.min, c.max)
                w.setSingleStep(c.step)
                w.setValue(value)
                label = QLabel(str(value))
                label.setMinimumWidth(36)
                label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
                w.valueChanged.connect(lambda v, c=c, label=label: (label.setText(str(v)), self._set(c, v)))
                row = QHBoxLayout()
                row.addWidget(w)
                row.addWidget(label)
                form.addRow(c.name, row)
            self.widgets[c.id] = w
        reset = QPushButton("Reset to defaults")
        reset.clicked.connect(self.reset)
        form.addRow(reset)
        self._refresh_active()

    def _set(self, control, value):
        self.device.set(control.id, value)
        self._refresh_active()

    def _refresh_active(self):
        """Disable controls the camera ignores, e.g. WB temperature while auto WB is on."""
        for cid, w in self.widgets.items():
            w.setEnabled(self.device.is_active(cid))

    def reset(self):
        for c in self.device.controls:
            w = self.widgets[c.id]
            if c.type == TYPE_BOOL:
                w.setChecked(bool(c.default))
            elif c.type == TYPE_MENU:
                w.setCurrentIndex(max(0, w.findData(c.default)))
            else:
                w.setValue(c.default)
            self.device.set(c.id, c.default)
        self._refresh_active()


class MainWindow(QMainWindow):
    def __init__(self, device_path, out_dir: Path):
        super().__init__()
        self.setWindowTitle("Microscope")
        self.out_dir = out_dir
        self.frame = None
        self.writer = None
        self.frame_times = []

        self.view = VideoView()
        self.camera = None
        self.device = None
        self.settings = None

        panel = QWidget()
        self.side = QVBoxLayout(panel)
        self.side.addWidget(self._device_box())
        self.side.addWidget(self._capture_box())
        self.side.addWidget(self._zoom_box())
        self.side.addStretch()
        scroll = QScrollArea()
        scroll.setWidget(panel)
        scroll.setWidgetResizable(True)
        scroll.setMinimumWidth(360)

        splitter = QSplitter()
        splitter.addWidget(self.view)
        splitter.addWidget(scroll)
        splitter.setStretchFactor(0, 1)
        splitter.setSizes([900, 380])
        self.setCentralWidget(splitter)

        self.info = QLabel()
        self.statusBar().addPermanentWidget(self.info)
        self.statusBar().showMessage("Wheel: zoom   Drag: pan   Double-click: fit   F: full screen")
        self._shortcuts()
        self.refresh_devices(prefer=device_path)

    def _device_box(self):
        box = QGroupBox("Device")
        lay = QHBoxLayout(box)
        self.device_combo = QComboBox()
        self.device_combo.currentIndexChanged.connect(lambda _i: self.open_device(self.device_combo.currentData()))
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(lambda: self.refresh_devices(prefer=self.device_combo.currentData()))
        lay.addWidget(self.device_combo, 1)
        lay.addWidget(refresh)
        return box

    def refresh_devices(self, prefer=None):
        """Fill the device list. Keep `prefer` if present, else pick the microscope, else the first camera."""
        cameras = list_cameras()
        paths = [c[0] for c in cameras]
        if prefer not in paths:
            prefer = next((c[0] for c in cameras if c[2] == MICROSCOPE_USB_ID), paths[0] if paths else None)
        self.device_combo.blockSignals(True)
        self.device_combo.clear()
        for path, label, usb_id in cameras:
            self.device_combo.addItem(("🔬 " if usb_id == MICROSCOPE_USB_ID else "") + label, path)
        self.device_combo.setCurrentIndex(max(0, paths.index(prefer)) if prefer else -1)
        self.device_combo.blockSignals(False)
        if prefer != (self.camera.device if self.camera else None) or not (self.camera and self.camera.isRunning()):
            self.open_device(prefer)
        if not cameras:
            self.statusBar().showMessage("No camera found. Connect the microscope and press Refresh.")

    def open_device(self, path):
        self.close_device()
        if not path:
            return
        try:
            self.device = V4L2Device(path)
        except OSError as e:
            self.statusBar().showMessage(f"Cannot open {path}: {e.strerror}")
            return
        self.settings = CameraSettings(self.device)
        self.side.insertWidget(self.side.count() - 1, self.settings)
        self.camera = CameraThread(path)
        self.camera.frame_ready.connect(self._on_frame)
        self.camera.failed.connect(self.statusBar().showMessage)
        self.camera.start()
        self.statusBar().showMessage(f"Connected to {path}", 3000)

    def close_device(self):
        if self.writer:
            self.toggle_record(False)
        if self.camera:
            self.camera.frame_ready.disconnect()  # Drop frames still queued from the old device.
            self.camera.stop()
            self.camera = None
        if self.settings:
            self.settings.deleteLater()
            self.settings = None
        if self.device:
            self.device.close()
            self.device = None
        self.frame = None
        self.view.clear()
        self.info.setText("")

    def _capture_box(self):
        box = QGroupBox("Capture")
        lay = QVBoxLayout(box)
        buttons = QHBoxLayout()
        self.snap_btn = QPushButton("Snapshot  (Space)")
        self.snap_btn.clicked.connect(self.snapshot)
        self.rec_btn = QPushButton("Record  (R)")
        self.rec_btn.setCheckable(True)
        self.rec_btn.toggled.connect(self.toggle_record)
        for b in (self.snap_btn, self.rec_btn):
            b.setMinimumHeight(36)
            b.setFocusPolicy(Qt.NoFocus)  # Keep Space for the snapshot shortcut.
            buttons.addWidget(b)
        lay.addLayout(buttons)
        folder = QHBoxLayout()
        self.folder_label = QLabel(str(self.out_dir))
        self.folder_label.setWordWrap(True)
        change = QPushButton("Change…")
        change.clicked.connect(self._choose_folder)
        open_btn = QPushButton("Open")
        open_btn.clicked.connect(self._open_folder)
        folder.addWidget(self.folder_label, 1)
        folder.addWidget(change)
        folder.addWidget(open_btn)
        lay.addLayout(folder)
        return box

    def _zoom_box(self):
        box = QGroupBox("Digital zoom")
        lay = QHBoxLayout(box)
        self.zoom_slider = QSlider(Qt.Horizontal)
        self.zoom_slider.setRange(10, int(MAX_ZOOM * 10))
        self.zoom_slider.valueChanged.connect(lambda v: self.view.set_zoom(v / 10))
        self.zoom_label = QLabel("1.0×")
        self.zoom_label.setMinimumWidth(36)
        fit = QPushButton("Fit")
        fit.clicked.connect(lambda: self.view.set_zoom(1.0))
        self.view.zoom_changed.connect(self._on_zoom)
        lay.addWidget(self.zoom_slider, 1)
        lay.addWidget(self.zoom_label)
        lay.addWidget(fit)
        return box

    def _shortcuts(self):
        keys = {
            "Space": self.snapshot,
            "R": self.rec_btn.toggle,
            "+": lambda: self.view.set_zoom(self.view.zoom * 1.25),
            "=": lambda: self.view.set_zoom(self.view.zoom * 1.25),
            "-": lambda: self.view.set_zoom(self.view.zoom / 1.25),
            "0": lambda: self.view.set_zoom(1.0),
            "F": self._toggle_fullscreen,
            "Esc": self.showNormal,
            "Ctrl+Q": self.close,
        }
        for key, fn in keys.items():
            QShortcut(QKeySequence(key), self, activated=fn)

    def _on_zoom(self, zoom):
        self.zoom_label.setText(f"{zoom:.1f}×")
        self.zoom_slider.blockSignals(True)
        self.zoom_slider.setValue(round(zoom * 10))
        self.zoom_slider.blockSignals(False)

    def _toggle_fullscreen(self):
        if self.isFullScreen():
            self.showNormal()
        else:
            self.showFullScreen()

    def _choose_folder(self):
        path = QFileDialog.getExistingDirectory(self, "Save folder", str(self.out_dir))
        if path:
            self.out_dir = Path(path)
            self.folder_label.setText(path)

    def _open_folder(self):
        self.out_dir.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.out_dir)))

    def _on_frame(self, frame):
        self.frame = frame
        self.view.set_frame(frame)
        if self.writer:
            self.writer.write(self._zoomed(frame))
        now = time.monotonic()
        self.frame_times = [t for t in self.frame_times if now - t < 1] + [now]
        h, w = frame.shape[:2]
        rec = "   ● REC" if self.writer else ""
        self.info.setText(f"{w}×{h}   {len(self.frame_times)} fps{rec}")

    def _zoomed(self, frame):
        """Frame as shown on screen: cropped by digital zoom, scaled back to full size."""
        h, w = frame.shape[:2]
        x, y, cw, ch = self.view.crop_pixels(w, h)
        if (cw, ch) == (w, h):
            return frame
        return cv2.resize(frame[y:y + ch, x:x + cw], (w, h), interpolation=cv2.INTER_CUBIC)

    def _new_path(self, prefix, ext):
        self.out_dir.mkdir(parents=True, exist_ok=True)
        return self.out_dir / f"{prefix}-{time.strftime('%Y%m%d-%H%M%S')}.{ext}"

    def snapshot(self):
        if self.frame is None:
            return
        path = self._new_path("snap", "png")
        cv2.imwrite(str(path), self._zoomed(self.frame))
        self.statusBar().showMessage(f"Saved {path}", 4000)

    def toggle_record(self, on):
        if on and self.frame is not None:
            path = self._new_path("video", "avi")
            h, w = self.frame.shape[:2]
            fps = max(1, len(self.frame_times))
            self.writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"MJPG"), fps, (w, h))
            self.rec_btn.setText("Stop  (R)")
            self.rec_btn.setStyleSheet("background:#c62828;color:white")
            self.statusBar().showMessage(f"Recording to {path}")
            return
        if self.writer:
            self.writer.release()
            self.writer = None
            self.statusBar().showMessage("Recording saved", 4000)
        self.rec_btn.blockSignals(True)
        self.rec_btn.setChecked(False)
        self.rec_btn.blockSignals(False)
        self.rec_btn.setText("Record  (R)")
        self.rec_btn.setStyleSheet("")

    def closeEvent(self, event):
        self.close_device()
        event.accept()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--device", default=None, help="video device to select at start, default: the microscope")
    ap.add_argument("--out", type=Path, default=Path.home() / "Pictures" / "microscope")
    args = ap.parse_args()

    app = QApplication(sys.argv)
    win = MainWindow(args.device, args.out)
    win.resize(1280, 760)
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
