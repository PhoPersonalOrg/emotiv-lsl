#!/usr/bin/env python3
"""List HID devices and highlight Emotiv USB receivers (macOS/Linux/Windows)."""

from __future__ import annotations

import argparse
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
    open_emotiv_hid_device,
    read_usb_feature_report_00,
)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--no-probe-feature",
        action="store_true",
        help="Skip opening selected MI_01 device to query Feature Report 0x00",
    )
    args = parser.parse_args(argv)

    all_devs = hid.enumerate()
    emotiv = list_emotiv_hid_devices()
    print(f"HID devices: {len(all_devs)}")
    print(f"Emotiv matches: {len(emotiv)}")
    for d in emotiv:
        print("  *", format_hid_device(d))
    ## END for d in emotiv...

    if not emotiv:
        print("No Emotiv USB receiver found. Plug in the dongle and retry.")
        print("All devices:")
        for d in all_devs:
            print("  -", format_hid_device(d))
        ## END for d in all_devs...

        return 1
    selected = find_emotiv_hid_device()
    print("Selected EEG device:")
    print("  ->", format_hid_device(selected))
    if not args.no_probe_feature:
        try:
            dev = open_emotiv_hid_device(selected)
            try:
                cfg = read_usb_feature_report_00(dev)
            finally:
                dev.close()
            if cfg is not None:
                print(
                    f"  Feature Report 0x00: headset=EPOCX-{cfg.headset_serial} "
                    f"headset_fw=0x{cfg.headset_fw:04X} dongle_fw=0x{cfg.dongle_fw:04X} "
                    f"eeg={cfg.eeg_rate}Hz/{cfg.eeg_bits}b mems={cfg.mems_rate}Hz/{cfg.mems_bits}b"
                )
            else:
                print("  Feature Report 0x00: unavailable")
        except Exception as exc:
            print(f"  Feature Report 0x00 probe skipped: {exc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
