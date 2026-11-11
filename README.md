# USB microscope viewer

Native Linux app for the Genesys 05e3:f12b USB microscope (`/dev/video2`).

## Setup
    python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

## Run
    .venv/bin/python microscope.py [--device /dev/video2] [--out ~/Pictures/microscope]

One window: live view on the left, capture, digital zoom and camera settings on the right.
Pick the camera in the Device list. Press Refresh after you plug in a camera.
At start the app selects the microscope (USB ID 05e3:f12b), else the first camera.
Keys: Space snapshot, R record, +/- zoom, 0 fit, F full screen, Ctrl+Q quit.
Mouse: wheel zooms at the cursor, drag pans, double-click fits.
Light and optical zoom are hardware-only. The snap button sends no event to Linux.
