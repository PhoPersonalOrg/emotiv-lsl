#!/usr/bin/env python3
"""Replay one `.epocxwire` through Python emotiv-lsl and C++ emotiv-lsl-cpp, compare streams.

  * Python — in-process decrypt/decode (same samples that would be pushed to LSL)
  * C++    — subprocess ``--replay``; this script attaches LSL inlets while C++ waits
             for consumers (requires a build with replay-outlet prefetch + have_consumers)

C++ still only reads EPOCXWIRE/1, so v2 captures are exported to a temp REPORT-only
v1 file (timestamps rebased to the first REPORT).

Example:
  pixi run python scripts/compare_py_cpp_replay.py session.epocxwire
  pixi run python scripts/compare_py_cpp_replay.py session.epocxwire --cpp-exe path\\to\\emotiv_lsl.exe
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path
from typing import Optional, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from emotiv_lsl.epocx_wire import (  # noqa: E402
    EEG_STREAM_NAME,
    EVENT_REPORT,
    QUALITY_STREAM_NAME,
    read_wire_file,
)
from tests.compat_helpers import (  # noqa: E402
    capture_cpp_lsl,
    compare_sample_arrays,
    diagnose_streams,
    python_decode_streams,
    require_cpp_exe,
    write_v1_for_cpp,
    effective_replay_speed,
)


def main(argv: Optional[Sequence[str]] = None) -> int:
    p = argparse.ArgumentParser(
        description="Replay .epocxwire in Python + C++ emotiv-lsl and compare EEG/quality samples"
    )
    p.add_argument("wire", type=Path, help="EPOCXWIRE capture (.epocxwire)")
    p.add_argument(
        "--speed",
        type=float,
        default=0.0,
        help="C++ --replay-speed. 0 (default) auto-paces rebased wire to --min-duration "
        "so LSL does not drop; use --force-asap for true speed=0.",
    )
    p.add_argument(
        "--min-duration",
        type=float,
        default=4.0,
        help="When --speed 0, stretch C++ replay to about this many seconds (default 4)",
    )
    p.add_argument(
        "--force-asap",
        action="store_true",
        help="Pass --replay-speed 0 literally (may drop LSL samples under load)",
    )
    p.add_argument(
        "--cpp-exe",
        type=Path,
        default=None,
        help="Path to emotiv-lsl-cpp emotiv_lsl.exe (or set EMOTIV_LSL_CPP)",
    )
    p.add_argument(
        "--atol",
        type=float,
        default=1e-2,
        help="Absolute tolerance in µV (default 1e-2; LSL float32 vs Python float64)",
    )
    p.add_argument("--rtol", type=float, default=0.0, help="Relative tolerance")
    p.add_argument(
        "--no-quality",
        action="store_true",
        help="Compare EEG only (skip Epoc X eQuality)",
    )
    p.add_argument(
        "--keep-v1",
        type=Path,
        default=None,
        help="Keep the temp EPOCXWIRE/1 export at this path",
    )
    p.add_argument(
        "--diagnose",
        action="store_true",
        help="On DIFF: run lag-scan + pacing hints (also auto-runs when counts differ)",
    )
    p.add_argument(
        "--kill-stale",
        action="store_true",
        help="On Windows, taskkill emotiv_lsl.exe before capture (opt-in)",
    )
    args = p.parse_args(argv)

    wire_path = args.wire.expanduser().resolve()
    if not wire_path.is_file():
        print(f"Wire file not found: {wire_path}", file=sys.stderr)
        return 2

    wire = read_wire_file(wire_path)
    n_reports = sum(1 for e in wire.events if e.kind == EVENT_REPORT) or len(wire.packets)
    print(
        f"Wire: {wire_path.name} v{wire.header.format_version} "
        f"serial={wire.header.serial} reports={n_reports} events={len(wire.events)}"
    )

    cpp_exe = require_cpp_exe(args.cpp_exe)
    print(f"C++ exe: {cpp_exe}")

    stream_names = [EEG_STREAM_NAME]
    if not args.no_quality:
        stream_names.append(QUALITY_STREAM_NAME)

    tmp_ctx = tempfile.TemporaryDirectory(prefix="epocx_cmp_")
    tmp_dir = Path(tmp_ctx.name)
    try:
        if args.keep_v1 is not None:
            v1_path = args.keep_v1.expanduser().resolve()
            v1_path.parent.mkdir(parents=True, exist_ok=True)
        else:
            v1_path = tmp_dir / "session_v1.epocxwire"
        n_v1 = write_v1_for_cpp(wire, v1_path, rebase_time=True)
        print(f"C++ v1 export: {v1_path} ({n_v1} REPORT packets, time rebased)")

        effective_speed, span_s = effective_replay_speed(
            wire,
            speed=float(args.speed),
            min_duration=float(args.min_duration),
            force_asap=bool(args.force_asap),
        )
        if args.speed <= 0 and not args.force_asap and span_s > 0:
            print(
                f"Auto pace C++: span={span_s:.3f}s → ~{args.min_duration:.1f}s wall "
                f"(--replay-speed {effective_speed:.6g})"
            )

        child_env = os.environ.copy()
        child_env.pop("SSL_CERT_DIR", None)

        print("\n=== python (in-process decode) ===")
        py_streams = python_decode_streams(wire_path)

        print("\n=== cpp (replay + LSL capture) ===")
        cpp_streams = capture_cpp_lsl(
            cpp_exe=cpp_exe,
            wire_v1=v1_path,
            stream_names=stream_names,
            speed=effective_speed,
            env=child_env,
            kill_stale_windows=bool(args.kill_stale),
        )
    finally:
        tmp_ctx.cleanup()

    print("\n=== comparison ===")
    all_ok = True
    count_mismatch = False
    for name in stream_names:
        a = py_streams.get(name)
        b = cpp_streams.get(name)
        if a is None or b is None:
            print(f"  {name}: MISSING (python={a is not None}, cpp={b is not None})")
            all_ok = False
            continue
        rep = compare_sample_arrays(a, b, atol=args.atol, rtol=args.rtol, label=name)
        status = "OK" if rep["ok"] else "DIFF"
        print(
            f"  {name}: {status}  n_py={rep['n_a']} n_cpp={rep['n_b']} "
            f"compared={rep['n_compared']} max_abs={rep['max_abs']} rmse={rep['rmse']}"
        )
        if rep["first_mismatch"] is not None:
            print(f"    first_mismatch: {rep['first_mismatch']}")
        if not rep["count_match"]:
            count_mismatch = True
        if not rep["ok"]:
            all_ok = False
    ## END for name in stream_names....

    if not all_ok and (args.diagnose or count_mismatch or effective_speed > 2.0):
        diagnose_streams(
            py_streams,
            cpp_streams,
            stream_names,
            atol=args.atol,
            rtol=args.rtol,
            effective_speed=effective_speed,
            span_s=span_s,
            min_duration=float(args.min_duration),
        )

    if all_ok:
        print("\nPASS: Python and C++ stream samples match within tolerance.")
        return 0

    print("\nFAIL: streams differ (see above).", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
