# USB microscope viewer

Native Linux app for the Genesys 05e3:f12b USB microscope (`/dev/video2`).

## Setup
    python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

## Run
    .venv/bin/python microscope.py [--device /dev/video2] [--out ~/Pictures/microscope]

The "Controls" window has sliders for all hardware V4L2 controls.
Keys: s/space snapshot, r record, +/- zoom, arrows pan, 0 reset zoom, c reset controls, q quit.
Light and optical zoom are hardware-only. The snap button sends no event to Linux.
