#!/usr/bin/env python3
"""Convert an Epoc X LabRecorder XDF into a timed `.epocxwire` packet dump."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from emotiv_lsl.epocx_wire import DEFAULT_SERIAL, write_wire_file, xdf_to_wire


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("xdf", type=Path, help="Input LabRecorder .xdf with Epoc X stream(s)")
    p.add_argument("-o", "--output", type=Path, required=True, help="Output .epocxwire path")
    p.add_argument("--serial", default=DEFAULT_SERIAL, help="HID/BLE serial for EpocXMode key")
    p.add_argument(
        "--serial-source",
        choices=("hid", "ble_hex"),
        default="hid",
        help="Serial material style (default: hid last-4 ASCII)",
    )
    p.add_argument("--adc-div", type=float, default=7.8, help="ADC divisor (16-bit default 7.8)")
    args = p.parse_args()

    if not args.xdf.exists():
        print(f"Error: XDF not found: {args.xdf}", file=sys.stderr)
        return 1

    wire = xdf_to_wire(
        args.xdf,
        serial=args.serial,
        serial_source=args.serial_source,
        adc_div=args.adc_div,
    )
    write_wire_file(args.output, wire)
    print(
        f"Wrote {len(wire.packets)} packets → {args.output} "
        f"(serial={wire.header.serial}, source={wire.header.serial_source})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
