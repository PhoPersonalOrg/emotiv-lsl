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
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
from pylsl import ContinuousResolver, StreamInlet

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from emotiv_lsl.emotiv_epoc_x import EmotivEpocX  # noqa: E402
from emotiv_lsl.epocx_wire import (  # noqa: E402
    EEG_STREAM_NAME,
    EVENT_REPORT,
    QUALITY_STREAM_NAME,
    WireReplaySource,
    read_wire_file,
    write_wire_file_v1,
)

CPP_CANDIDATES = [
    Path(os.environ["EMOTIV_LSL_CPP"]) if os.environ.get("EMOTIV_LSL_CPP") else None,
    ROOT.parent / "emotiv-lsl-cpp" / "build-static" / "src" / "emotiv" / "Release" / "emotiv_lsl.exe",
    ROOT.parent / "emotiv-lsl-cpp" / "dist" / "emotiv_lsl-1.8.0-windows-x86_64.exe",
    ROOT.parent / "emotiv-lsl-cpp" / "build" / "install" / "emotiv_lsl.exe",
    ROOT.parent / "emotiv-lsl-cpp" / "build" / "src" / "emotiv" / "Release" / "emotiv_lsl.exe",
]


def _find_cpp_exe(explicit: Optional[Path]) -> Path:
    if explicit is not None:
        p = explicit.expanduser().resolve()
        if not p.is_file():
            raise FileNotFoundError(f"--cpp-exe not found: {p}")
        return p
    for cand in CPP_CANDIDATES:
        if cand is not None and cand.is_file():
            return cand.resolve()
    raise FileNotFoundError(
        "emotiv-lsl-cpp executable not found. Pass --cpp-exe or set EMOTIV_LSL_CPP. "
        "Tried:\n  " + "\n  ".join(str(c) for c in CPP_CANDIDATES if c is not None)
    )


def decode_python_streams(wire_path: Path) -> Dict[str, np.ndarray]:
    """Decrypt/decode every REPORT via EmotivEpocX (identical to LSL push payload)."""
    src = WireReplaySource.from_path(wire_path, speed=0.0)
    headset = EmotivEpocX(
        enable_motion_data=False,
        enable_electrode_quality_stream=True,
        replay_source=src,
        replay_speed=0.0,
        serial_number=src.header.serial,
        serial_source=src.header.serial_source,
    )
    src.apply_serial_to(headset)

    eeg: List[List[float]] = []
    quality: List[List[float]] = []
    n_raw = 0
    while True:
        data = src.read(headset.READ_SIZE)
        if not data and src.eof:
            break
        if not data:
            continue
        n_raw += 1
        if not headset.validate_data(data):
            continue
        decoded, cq = headset.decode_data(data)
        if decoded is None:
            continue
        if len(decoded) == 14:
            eeg.append([float(x) for x in decoded])
            if cq is not None and len(cq) == 14:
                quality.append([float(x) for x in cq])
        ## END if len(decoded) == 14....
    ## END while True....

    print(f"  python decode: raw_reports={n_raw} eeg={len(eeg)} quality={len(quality)}")
    out = {EEG_STREAM_NAME: np.asarray(eeg, dtype=np.float64)}
    if quality:
        out[QUALITY_STREAM_NAME] = np.asarray(quality, dtype=np.float64)
    return out


def _open_inlet(info) -> StreamInlet:
    inlet = StreamInlet(info, max_buflen=600, processing_flags=0)
    while True:
        chunk, _ = inlet.pull_chunk(timeout=0.0, max_samples=1024)
        if not chunk:
            break
    ## END while True....

    return inlet


def capture_cpp_lsl(
    *,
    cpp_exe: Path,
    wire_v1: Path,
    stream_names: Sequence[str],
    speed: float,
    env: dict,
    resolve_timeout_s: float = 30.0,
    drain_s: float = 2.0,
) -> Dict[str, np.ndarray]:
    """Launch C++ replay and pull LSL samples while it waits for consumers."""
    # Avoid a stale inlet making C++ have_consumers() true before we attach.
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/F", "/IM", "emotiv_lsl.exe"],
            capture_output=True,
            text=True,
            check=False,
        )
        time.sleep(0.5)

    handshake_dir = Path(tempfile.mkdtemp(prefix="epocx_hsl_"))
    ready_file = handshake_dir / "outlets_ready"
    go_file = handshake_dir / "go"

    resolvers = {
        name: ContinuousResolver(prop="name", value=name, forget_after=15.0)
        for name in stream_names
    }
    time.sleep(0.25)

    cmd = [
        str(cpp_exe),
        "--replay",
        str(wire_v1),
        "--replay-speed",
        str(speed),
    ]
    print("  cmd:", " ".join(cmd))
    child_env = dict(env)
    child_env["EMOTIV_REPLAY_OUTLETS_READY_FILE"] = str(ready_file)
    child_env["EMOTIV_REPLAY_GO_FILE"] = str(go_file)
    child_env.setdefault("EMOTIV_REPLAY_PREFETCH_SEC", "30")
    child_env.setdefault("EMOTIV_REPLAY_HOLD_SEC", "1")

    t0 = time.perf_counter()
    proc = subprocess.Popen(
        cmd,
        cwd=str(cpp_exe.parent),
        env=child_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    log_lines: List[str] = []

    def _pump() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            log_lines.append(line.rstrip("\n"))
            if any(
                k in line
                for k in (
                    "Replay",
                    "outlet",
                    "Streaming",
                    "Waiting",
                    "consumer",
                    "Outlets ready",
                    "GO file",
                    "finished",
                    "Holding",
                    "Error",
                )
            ):
                print(f"  [cpp] {line.rstrip()}")
        ## END for line in proc.stdout....

    pump = threading.Thread(target=_pump, name="cpp-stdout", daemon=True)
    pump.start()

    inlets: Dict[str, StreamInlet] = {}
    buffers: Dict[str, List[List[float]]] = {n: [] for n in stream_names}
    resolve_deadline = time.monotonic() + resolve_timeout_s

    try:
        # Wait until C++ has advertised outlets.
        while time.monotonic() < resolve_deadline and proc.poll() is None:
            if ready_file.is_file():
                break
            time.sleep(0.02)
        ## END while time.monotonic() < resolve_deadline....

        if not ready_file.is_file():
            raise RuntimeError(
                "C++ never created outlets-ready file.\n" + "\n".join(log_lines[-40:])
            )

        while time.monotonic() < resolve_deadline and proc.poll() is None:
            for name, resolver in resolvers.items():
                if name in inlets:
                    continue
                results = resolver.results()
                if not results:
                    continue
                inlets[name] = _open_inlet(results[0])
                print(f"  linked {name!r}")
            ## END for name, resolver in resolvers.items()....

            if len(inlets) == len(stream_names):
                break
            time.sleep(0.05)
        ## END while time.monotonic() < resolve_deadline....

        if not inlets:
            raise RuntimeError(
                "C++ LSL streams never appeared.\n"
                + "\n".join(log_lines[-40:])
            )

        # Tell C++ it is safe to push; busy-pull in a tight loop so speed=0 does not overrun.
        stop_pull = threading.Event()

        def _busy_pull() -> None:
            while not stop_pull.is_set():
                for name, inlet in list(inlets.items()):
                    chunk, _tss = inlet.pull_chunk(timeout=0.0, max_samples=2048)
                    for sample in chunk:
                        buffers[name].append(list(sample))
                    ## END for sample in chunk....
                ## END for name, inlet in list(inlets.items())....

                time.sleep(0.0)
            ## END while not stop_pull.is_set()....

        puller = threading.Thread(target=_busy_pull, name="lsl-pull", daemon=True)
        puller.start()
        go_file.write_text("go\n", encoding="utf-8")
        print("  wrote GO file; pulling samples...")

        while proc.poll() is None:
            time.sleep(0.05)
        ## END while proc.poll() is None....

        drain_until = time.monotonic() + drain_s
        while time.monotonic() < drain_until:
            time.sleep(0.05)
        ## END while time.monotonic() < drain_until....

        stop_pull.set()
        puller.join(timeout=2)

    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        pump.join(timeout=2)
        try:
            for p in (ready_file, go_file):
                if p.is_file():
                    p.unlink()
                ## END if p.is_file()....
            ## END for p in (ready_file, go_file)....

            handshake_dir.rmdir()
        except OSError:
            pass

    elapsed = time.perf_counter() - t0
    rc = proc.returncode if proc.returncode is not None else -1
    print(f"  cpp exit={rc} elapsed={elapsed:.2f}s")

    out: Dict[str, np.ndarray] = {}
    for name in stream_names:
        rows = buffers.get(name) or []
        if rows:
            arr = np.asarray(rows, dtype=np.float64)
        else:
            arr = np.zeros((0, 14), dtype=np.float64)
        out[name] = arr
        print(f"  {name}: n={arr.shape[0]} shape={arr.shape}")
    ## END for name in stream_names....

    if rc not in (0, None) and out[EEG_STREAM_NAME].shape[0] == 0:
        raise RuntimeError(
            f"C++ failed (exit={rc}) with no EEG samples.\n" + "\n".join(log_lines[-40:])
        )
    return out


def compare_arrays(
    a: np.ndarray,
    b: np.ndarray,
    *,
    atol: float,
    rtol: float,
    label: str,
) -> dict:
    n = min(a.shape[0], b.shape[0])
    report = {
        "label": label,
        "n_a": int(a.shape[0]),
        "n_b": int(b.shape[0]),
        "n_compared": n,
        "count_match": a.shape[0] == b.shape[0],
        "ok": False,
        "max_abs": None,
        "rmse": None,
        "first_mismatch": None,
    }
    if a.ndim != 2 or b.ndim != 2:
        report["first_mismatch"] = f"bad ndim a={a.ndim} b={b.ndim}"
        return report
    if n == 0:
        report["ok"] = a.shape[0] == 0 and b.shape[0] == 0
        return report
    if a.shape[1] != b.shape[1]:
        report["first_mismatch"] = f"channel_count {a.shape[1]} vs {b.shape[1]}"
        return report

    aa = a[:n]
    bb = b[:n]
    diff = np.abs(aa - bb)
    report["max_abs"] = float(diff.max())
    report["rmse"] = float(np.sqrt(np.mean((aa - bb) ** 2)))
    close = np.isclose(aa, bb, atol=atol, rtol=rtol)
    if close.all() and report["count_match"]:
        report["ok"] = True
        return report
    bad = np.argwhere(~close)
    if len(bad):
        si, ch = int(bad[0][0]), int(bad[0][1])
        report["first_mismatch"] = {
            "sample": si,
            "channel": ch,
            "a": float(aa[si, ch]),
            "b": float(bb[si, ch]),
            "abs": float(abs(aa[si, ch] - bb[si, ch])),
        }
    report["ok"] = bool(close.all() and report["count_match"])
    return report


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

    cpp_exe = _find_cpp_exe(args.cpp_exe)
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
        n_v1 = write_wire_file_v1(v1_path, wire, rebase_time=True)
        print(f"C++ v1 export: {v1_path} ({n_v1} REPORT packets, time rebased)")

        # Estimate rebased span for pacing (first REPORT at 0).
        report_ns = [e.rel_ns for e in wire.events if e.kind == EVENT_REPORT] or [
            p.rel_ns for p in wire.packets
        ]
        if report_ns:
            span_s = (max(report_ns) - min(report_ns)) * 1e-9
        else:
            span_s = 0.0
        if args.force_asap:
            effective_speed = 0.0
        elif args.speed > 0:
            effective_speed = float(args.speed)
        elif span_s > 0:
            effective_speed = max(span_s / max(float(args.min_duration), 0.5), 1e-6)
            print(
                f"Auto pace C++: span={span_s:.3f}s → ~{args.min_duration:.1f}s wall "
                f"(--replay-speed {effective_speed:.6g})"
            )
        else:
            effective_speed = 1.0

        child_env = os.environ.copy()
        child_env.pop("SSL_CERT_DIR", None)

        print("\n=== python (in-process decode) ===")
        py_streams = decode_python_streams(wire_path)

        print("\n=== cpp (replay + LSL capture) ===")
        cpp_streams = capture_cpp_lsl(
            cpp_exe=cpp_exe,
            wire_v1=v1_path,
            stream_names=stream_names,
            speed=effective_speed,
            env=child_env,
        )
    finally:
        tmp_ctx.cleanup()

    print("\n=== comparison ===")
    all_ok = True
    for name in stream_names:
        a = py_streams.get(name)
        b = cpp_streams.get(name)
        if a is None or b is None:
            print(f"  {name}: MISSING (python={a is not None}, cpp={b is not None})")
            all_ok = False
            continue
        rep = compare_arrays(a, b, atol=args.atol, rtol=args.rtol, label=name)
        status = "OK" if rep["ok"] else "DIFF"
        print(
            f"  {name}: {status}  n_py={rep['n_a']} n_cpp={rep['n_b']} "
            f"compared={rep['n_compared']} max_abs={rep['max_abs']} rmse={rep['rmse']}"
        )
        if rep["first_mismatch"] is not None:
            print(f"    first_mismatch: {rep['first_mismatch']}")
        if not rep["ok"]:
            all_ok = False
    ## END for name in stream_names....

    if all_ok:
        print("\nPASS: Python and C++ stream samples match within tolerance.")
        return 0

    print("\nFAIL: streams differ (see above).", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
