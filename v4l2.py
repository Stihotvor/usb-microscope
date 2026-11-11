"""Minimal V4L2 control access through ioctls (linux/videodev2.h)."""
import fcntl
import os
import struct
from dataclasses import dataclass, field

VIDIOC_QUERYCTRL = 0xC0445624
VIDIOC_QUERYMENU = 0xC02C5625
VIDIOC_G_CTRL = 0xC008561B
VIDIOC_S_CTRL = 0xC008561C
QUERYCTRL_FMT = "II32siiiiI8x"
QUERYMENU_FMT = "II32sI"
NEXT_CTRL = 0x80000000
FLAG_DISABLED = 0x0001
FLAG_INACTIVE = 0x0010

TYPE_INT, TYPE_BOOL, TYPE_MENU = 1, 2, 3


@dataclass
class Control:
    id: int
    name: str
    type: int
    min: int
    max: int
    step: int
    default: int
    menu: dict = field(default_factory=dict)


class V4L2Device:
    def __init__(self, path):
        self.fd = os.open(path, os.O_RDWR)
        self.controls = list(self._enumerate())

    def close(self):
        os.close(self.fd)

    def _query(self, cid):
        buf = bytearray(struct.pack(QUERYCTRL_FMT, cid, 0, b"", 0, 0, 0, 0, 0))
        fcntl.ioctl(self.fd, VIDIOC_QUERYCTRL, buf)
        return struct.unpack(QUERYCTRL_FMT, buf)

    def _enumerate(self):
        cid = NEXT_CTRL
        while True:
            try:
                real_id, ctype, name, lo, hi, step, default, flags = self._query(cid)
            except OSError:
                return
            cid = real_id | NEXT_CTRL
            if ctype in (TYPE_INT, TYPE_BOOL, TYPE_MENU) and not flags & FLAG_DISABLED:
                ctrl = Control(real_id, name.rstrip(b"\0").decode(), ctype, lo, hi, max(step, 1), default)
                if ctype == TYPE_MENU:
                    ctrl.menu = self._menu(real_id, lo, hi)
                yield ctrl

    def _menu(self, cid, lo, hi):
        items = {}
        for index in range(lo, hi + 1):
            buf = bytearray(struct.pack(QUERYMENU_FMT, cid, index, b"", 0))
            try:
                fcntl.ioctl(self.fd, VIDIOC_QUERYMENU, buf)
                items[index] = struct.unpack(QUERYMENU_FMT, buf)[2].rstrip(b"\0").decode()
            except OSError:
                pass
        return items

    def is_active(self, cid):
        return not self._query(cid)[7] & FLAG_INACTIVE

    def get(self, cid):
        buf = bytearray(struct.pack("Ii", cid, 0))
        fcntl.ioctl(self.fd, VIDIOC_G_CTRL, buf)
        return struct.unpack("Ii", buf)[1]

    def set(self, cid, value):
        try:
            fcntl.ioctl(self.fd, VIDIOC_S_CTRL, struct.pack("Ii", cid, value))
            return True
        except OSError:
            return False
