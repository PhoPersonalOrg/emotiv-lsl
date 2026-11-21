#!/usr/bin/env python3
"""List HID devices and highlight Emotiv USB receivers (macOS/Linux/Windows)."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import hid

from emotiv_lsl.hid_device import (
    find_emotiv_hid_device,
    format_hid_device,
    list_emotiv_hid_devices,
)


def main() -> int:
    all_devs = hid.enumerate()
    emotiv = list_emotiv_hid_devices()
    print(f"HID devices: {len(all_devs)}")
    print(f"Emotiv matches: {len(emotiv)}")
    for d in emotiv:
        print("  *", format_hid_device(d))
    if not emotiv:
        print("No Emotiv USB receiver found. Plug in the dongle and retry.")
        print("All devices:")
        for d in all_devs:
            print("  -", format_hid_device(d))
        return 1
    selected = find_emotiv_hid_device()
    print("Selected EEG device:")
    print("  ->", format_hid_device(selected))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
