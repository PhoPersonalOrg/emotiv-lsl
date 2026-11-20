#!/usr/bin/env python3
"""Offline-decode a Flutter Emotiv logger session folder.

Reads ``session.json`` + ``raw.csv`` (undecrypted BLE payloads) and writes
``eeg_offline.csv`` / ``motion_offline.csv`` beside them. Optionally emits a
``.epocxwire`` file for ``main.py --replay``.

Example:
  python scripts/decode_flutter_raw_csv.py /path/to/session_2025-...
  python scripts/decode_flutter_raw_csv.py ./session_x -o ./session_x/replay.epocxwire
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from emotiv_lsl.flutter_raw_csv import decode_session_folder


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "session_dir",
        type=Path,
        help="Flutter session_* folder containing session.json and raw.csv",
    )
    p.add_argument(
        "-o",
        "--wire-output",
        type=Path,
        default=None,
        help="Optional .epocxwire path for main.py --replay",
    )
    p.add_argument(
        "--no-csv",
        action="store_true",
        help="Skip writing eeg_offline.csv / motion_offline.csv",
    )
    args = p.parse_args()

    session_dir = args.session_dir
    if not session_dir.is_dir():
        print(f"Error: not a directory: {session_dir}", file=sys.stderr)
        return 1
    if not (session_dir / "raw.csv").is_file():
        print(f"Error: missing raw.csv in {session_dir}", file=sys.stderr)
        return 1
    if not (session_dir / "session.json").is_file():
        print(f"Error: missing session.json in {session_dir}", file=sys.stderr)
        return 1

    result = decode_session_folder(
        session_dir,
        write_csvs=not args.no_csv,
        wire_path=args.wire_output,
    )
    print(
        f"Decoded {len(result.eeg)} EEG + {len(result.motion)} motion samples "
        f"(skipped={result.skipped}, failed={result.failed}) from {session_dir}"
    )
    if not args.no_csv:
        print(f"  → {session_dir / 'eeg_offline.csv'}")
        print(f"  → {session_dir / 'motion_offline.csv'}")
    if args.wire_output is not None:
        print(f"  → {args.wire_output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
