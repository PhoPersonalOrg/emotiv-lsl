#!/usr/bin/env python3
"""WinUHid feeder for EPOCXWIRE/2 captures.

Without the WinUHid driver this script paces REPORT events and prints them
(``--dry-run``, default). With the driver installed, ``--submit`` creates a
virtual HID device (VID/PID + report descriptor from the wire header) and
calls WinUHidSubmitInputReport for each REPORT payload.

Usage:
  python scripts/feed_winuhid.py session.epocxwire --dry-run
  python scripts/feed_winuhid.py session.epocxwire --dry-run --speed 0
  python scripts/feed_winuhid.py session.epocxwire --submit
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from emotiv_lsl.epocx_wire import (
    EVENT_CLOSE,
    EVENT_EMPTY,
    EVENT_ERROR,
    EVENT_OPEN,
    EVENT_REPORT,
    WinUHidFeeder,
    read_wire_file,
)


def _kind_name(kind: int) -> str:
    return {
        EVENT_REPORT: "REPORT",
        EVENT_EMPTY: "EMPTY",
        EVENT_ERROR: "ERROR",
        EVENT_OPEN: "OPEN",
        EVENT_CLOSE: "CLOSE",
    }.get(kind, f"TYPE{kind}")


def dry_run(path: Path, *, speed: float = 1.0) -> int:
    wire = read_wire_file(path)
    hdr = wire.header
    print(
        f"dry-run feed: serial={hdr.serial} vid={hdr.vid} pid={hdr.pid} "
        f"events={len(wire.events)} speed={speed}"
    )
    if hdr.report_descriptor:
        print(f"  report_descriptor: {len(hdr.report_descriptor)//2} bytes (hex present)")
    else:
        print("  report_descriptor: (none — using built-in Emotiv EEG default on --submit)")

    t0 = time.perf_counter()
    for ev in wire.events:
        if speed > 0:
            target = t0 + (ev.rel_ns * 1e-9) / speed
            delay = target - time.perf_counter()
            if delay > 0:
                time.sleep(delay)
        name = _kind_name(ev.kind)
        if ev.kind == EVENT_REPORT:
            print(f"  {ev.rel_ns:>12} ns  {name}  {len(ev.payload)} B")
        elif ev.kind == EVENT_ERROR:
            print(f"  {ev.rel_ns:>12} ns  {name}  {ev.message!r}")
        else:
            print(f"  {ev.rel_ns:>12} ns  {name}")
    ## END for ev in wire.events....

    print("done (no virtual HID submitted)")
    return 0


def submit_run(path: Path, *, speed: float = 1.0) -> int:
    wire = read_wire_file(path)
    hdr = wire.header
    print(
        f"submit feed: serial={hdr.serial} vid={hdr.vid} pid={hdr.pid} "
        f"events={len(wire.events)} speed={speed}"
    )
    feeder = WinUHidFeeder(path, speed=speed)
    try:
        feeder.open()
        n = feeder.feed()
    finally:
        feeder.close()
    print(f"done ({n} input reports submitted)")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="WinUHid feeder for .epocxwire")
    p.add_argument("wire", type=Path, help="EPOCXWIRE/1 or /2 capture file")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Pace and print events without WinUHid (default)",
    )
    mode.add_argument(
        "--submit",
        action="store_true",
        help="Create a virtual HID device and submit REPORT payloads via WinUHid",
    )
    p.add_argument("--speed", type=float, default=1.0, help="Pace factor (0=asap)")
    args = p.parse_args(argv)

    if args.submit:
        return submit_run(args.wire, speed=args.speed)
    return dry_run(args.wire, speed=args.speed)


if __name__ == "__main__":
    raise SystemExit(main())
