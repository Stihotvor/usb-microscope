"""USB microscope viewer: live view, camera controls, digital zoom, snapshots, video recording.

Keys: s/space = snapshot, r = start/stop recording, +/- = zoom, arrows or w/a/x/d = pan,
      0 = reset zoom, c = reset controls to defaults, q/Esc = quit.
"""
import argparse
import fcntl
import os
import struct
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_FONTDIR", "/usr/share/fonts/truetype/dejavu")

import cv2  # noqa: E402

# V4L2 ioctl numbers and struct layouts (linux/videodev2.h).
VIDIOC_QUERYCTRL = 0xC0445624
VIDIOC_G_CTRL = 0xC008561B
VIDIOC_S_CTRL = 0xC008561C
QUERYCTRL_FMT = "II32siiiiI8x"
NEXT_CTRL = 0x80000000
FLAG_DISABLED = 0x0001
TYPE_INT, TYPE_BOOL, TYPE_MENU = 1, 2, 3

WINDOW = "Microscope"
PANEL = "Controls"


class V4L2Controls:
    """Reads and writes the camera's hardware controls through V4L2 ioctls."""

    def __init__(self, device):
        self.fd = os.open(device, os.O_RDWR)
        self.controls = list(self._enumerate())

    def _enumerate(self):
        cid = NEXT_CTRL
        while True:
            buf = bytearray(struct.pack(QUERYCTRL_FMT, cid, 0, b"", 0, 0, 0, 0, 0))
            try:
                fcntl.ioctl(self.fd, VIDIOC_QUERYCTRL, buf)
            except OSError:
                return
            cid_, ctype, name, lo, hi, _step, default, flags = struct.unpack(QUERYCTRL_FMT, buf)
            cid = cid_ | NEXT_CTRL
            if ctype in (TYPE_INT, TYPE_BOOL, TYPE_MENU) and not flags & FLAG_DISABLED:
                yield {"id": cid_, "name": name.rstrip(b"\0").decode(), "min": lo, "max": hi, "default": default}

    def get(self, cid):
        buf = bytearray(struct.pack("Ii", cid, 0))
        fcntl.ioctl(self.fd, VIDIOC_G_CTRL, buf)
        return struct.unpack("Ii", buf)[1]

    def set(self, cid, value):
        try:
            fcntl.ioctl(self.fd, VIDIOC_S_CTRL, struct.pack("Ii", cid, value))
        except OSError:
            pass  # Some controls are read-only while an auto mode is on.


class DigitalZoom:
    """Crops and scales the frame around a movable centre."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.level, self.cx, self.cy = 1.0, 0.5, 0.5

    def change(self, factor):
        self.level = min(8.0, max(1.0, self.level * factor))

    def pan(self, dx, dy):
        step = 0.05 / self.level
        self.cx = min(1.0, max(0.0, self.cx + dx * step))
        self.cy = min(1.0, max(0.0, self.cy + dy * step))

    def apply(self, frame):
        if self.level == 1.0:
            return frame
        h, w = frame.shape[:2]
        cw, ch = int(w / self.level), int(h / self.level)
        x = int(min(max(self.cx * w - cw / 2, 0), w - cw))
        y = int(min(max(self.cy * h - ch / 2, 0), h - ch))
        return cv2.resize(frame[y:y + ch, x:x + cw], (w, h), interpolation=cv2.INTER_CUBIC)


def build_panel(ctrls):
    cv2.namedWindow(PANEL, cv2.WINDOW_NORMAL)
    for c in ctrls.controls:
        def on_change(pos, c=c):
            ctrls.set(c["id"], pos + c["min"])
        cv2.createTrackbar(c["name"], PANEL, ctrls.get(c["id"]) - c["min"], c["max"] - c["min"], on_change)


def reset_controls(ctrls):
    for c in ctrls.controls:
        ctrls.set(c["id"], c["default"])
        cv2.setTrackbarPos(c["name"], PANEL, c["default"] - c["min"])


def window_closed():
    try:
        return cv2.getWindowProperty(WINDOW, cv2.WND_PROP_VISIBLE) < 1
    except cv2.error:
        return False  # Qt backend not ready yet.


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--device", default="/dev/video2")
    ap.add_argument("--width", type=int, default=640)
    ap.add_argument("--height", type=int, default=480)
    ap.add_argument("--out", type=Path, default=Path.home() / "Pictures" / "microscope")
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    cap = cv2.VideoCapture(args.device, cv2.CAP_V4L2)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, args.width)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, args.height)
    if not cap.isOpened():
        raise SystemExit(f"Cannot open {args.device}")

    ctrls = V4L2Controls(args.device)
    zoom = DigitalZoom()
    cv2.namedWindow(WINDOW, cv2.WINDOW_NORMAL)
    build_panel(ctrls)
    writer, message, message_until = None, "", 0.0

    def notify(text):
        nonlocal message, message_until
        message, message_until = text, time.time() + 2
        print(text)

    while True:
        ok, frame = cap.read()
        if not ok:
            notify("Frame read failed")
            break
        view = zoom.apply(frame)
        if writer:
            writer.write(view)

        shown = view.copy()
        status = f"zoom x{zoom.level:.1f}" + ("  REC" if writer else "")
        cv2.putText(shown, status, (10, 25), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 2)
        if time.time() < message_until:
            cv2.putText(shown, message, (10, shown.shape[0] - 15), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 255), 1)
        cv2.imshow(WINDOW, shown)

        key = cv2.waitKeyEx(1)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        if key in (ord("q"), 27) or window_closed():
            break
        elif key in (ord("s"), ord(" ")):
            path = args.out / f"snap-{stamp}.png"
            cv2.imwrite(str(path), view)
            notify(f"Saved {path}")
        elif key == ord("r"):
            if writer:
                writer.release()
                writer = None
                notify("Recording stopped")
            else:
                path = args.out / f"video-{stamp}.avi"
                fps = cap.get(cv2.CAP_PROP_FPS) or 30
                writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"MJPG"), fps, view.shape[1::-1])
                notify(f"Recording {path}")
        elif key in (ord("+"), ord("=")):
            zoom.change(1.25)
        elif key == ord("-"):
            zoom.change(0.8)
        elif key == ord("0"):
            zoom.reset()
        elif key == ord("c"):
            reset_controls(ctrls)
        elif key in (ord("a"), 65361):
            zoom.pan(-1, 0)
        elif key in (ord("d"), 65363):
            zoom.pan(1, 0)
        elif key in (ord("w"), 65362):
            zoom.pan(0, -1)
        elif key in (ord("x"), 65364):
            zoom.pan(0, 1)

    if writer:
        writer.release()
    cap.release()
    cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
