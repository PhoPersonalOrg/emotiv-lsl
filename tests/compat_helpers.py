"""Shared helpers for Python ↔ C++ emotiv-lsl compatibility tests and CLI."""
from __future__ import annotations

import os
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
MODE7_TWO_PACKET = FIXTURES_DIR / "mode7_two_packet.epocxwire"

# Cross-repo gold expectations (matches emotiv-lsl-cpp test_wire_fixture_replay EEG).
FIXTURE_SERIAL = "UD20221202006756"
FIXTURE_CQ = 0x2B  # raw word7 in mode7_two_packet.epocxwire -> norm < 0.2 -> grade 0
SYNTH_CQ_WORD7_CYCLE: Tuple[int, int, int, int] = (100, 400, 900, 1800)  # grades (0, 1, 2, 4) across all 14 divisors
FIXTURE_AF3_UV = round(100.0 * 7.8) / 7.8
SAMPLE_ATOL = 1e-2

# Flutter logger BLE serial (matches test_flutter_raw_csv / Dart crypto tests).
FLUTTER_BLE_SERIAL = "E50202E9"
FLUTTER_EEG_CHAR_UUID = "81072F41-9F3D-11E3-A9DC-0002A5D5C51B"
FLUTTER_MOTION_CHAR_UUID = "81072F42-9F3D-11E3-A9DC-0002A5D5C51B"
# Documented Flutter LSL inventory constants.
FLUTTER_LSL_EEG_NAME = "Epoc X"
FLUTTER_LSL_MOTION_NAME = "Epoc X Motion"
FLUTTER_LSL_QUALITY_NAME = "Epoc X eQuality"
FLUTTER_LSL_MOTION_TYPE = "Accelerometer"  # Python/C++ use SIGNAL
FLUTTER_HAS_EQUALITY_LSL = True

CPP_CANDIDATES = [
    Path(os.environ["EMOTIV_LSL_CPP"]) if os.environ.get("EMOTIV_LSL_CPP") else None,
    ROOT.parent
    / "emotiv-lsl-cpp"
    / "build-static"
    / "src"
    / "emotiv"
    / "Release"
    / "emotiv_lsl.exe",
    ROOT.parent / "emotiv-lsl-cpp" / "dist" / "emotiv_lsl-1.8.0-windows-x86_64.exe",
    ROOT.parent / "emotiv-lsl-cpp" / "build" / "install" / "emotiv_lsl.exe",
    ROOT.parent / "emotiv-lsl-cpp" / "build" / "install" / "emotiv_lsl",
    ROOT.parent
    / "emotiv-lsl-cpp"
    / "build"
    / "src"
    / "emotiv"
    / "Release"
    / "emotiv_lsl.exe",
    ROOT.parent / "emotiv-lsl-cpp" / "build" / "src" / "emotiv" / "emotiv_lsl",
]


def find_cpp_exe(explicit: Optional[Path] = None) -> Optional[Path]:
    """Return path to emotiv-lsl-cpp binary, or None if not found."""
    if explicit is not None:
        p = explicit.expanduser().resolve()
        return p if p.is_file() else None
    for cand in CPP_CANDIDATES:
        if cand is not None and cand.is_file():
            return cand.resolve()
    return None


def require_cpp_exe(explicit: Optional[Path] = None) -> Path:
    """Like find_cpp_exe but raises FileNotFoundError with candidate list."""
    found = find_cpp_exe(explicit)
    if found is not None:
        return found
    if explicit is not None:
        raise FileNotFoundError(f"--cpp-exe not found: {explicit}")
    raise FileNotFoundError(
        "emotiv-lsl-cpp executable not found. Pass --cpp-exe or set EMOTIV_LSL_CPP. "
        "Tried:\n  " + "\n  ".join(str(c) for c in CPP_CANDIDATES if c is not None)
    )


def skip_unless_cpp_exe():
    """unittest skip decorator when no C++ binary is available."""
    return unittest.skipUnless(
        find_cpp_exe() is not None,
        "emotiv-lsl-cpp binary not found (set EMOTIV_LSL_CPP or build sibling)",
    )


def python_decode_streams(wire_path: Path, *, quiet: bool = False, enable_motion: bool = False) -> Dict[str, np.ndarray]:
    """Decrypt/decode every REPORT via EmotivEpocX (identical to LSL push payload)."""
    from emotiv_lsl.emotiv_epoc_x import EmotivEpocX
    from emotiv_lsl.epocx_wire import (
        EEG_STREAM_NAME,
        MOTION_STREAM_NAME,
        QUALITY_STREAM_NAME,
        WireReplaySource,
    )

    src = WireReplaySource.from_path(wire_path, speed=0.0)
    headset = EmotivEpocX(
        enable_motion_data=enable_motion,
        enable_electrode_quality_stream=True,
        replay_source=src,
        replay_speed=0.0,
        serial_number=src.header.serial,
        serial_source=src.header.serial_source,
    )
    src.apply_serial_to(headset)

    eeg: List[List[float]] = []
    quality: List[List[float]] = []
    motion: List[List[float]] = []
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
        elif len(decoded) == 10 and enable_motion:
            motion.append([float(x) for x in decoded])
    ## END while True...

    if not quiet:
        print(f"  python decode: raw_reports={n_raw} eeg={len(eeg)} quality={len(quality)} motion={len(motion)}")
    out = {EEG_STREAM_NAME: np.asarray(eeg, dtype=np.float64)}
    if quality:
        out[QUALITY_STREAM_NAME] = np.asarray(quality, dtype=np.float64)
    if motion:
        out[MOTION_STREAM_NAME] = np.asarray(motion, dtype=np.float64)
    return out


# Back-compat alias used by compare_py_cpp_replay CLI
decode_python_streams = python_decode_streams


def space_reports_if_collapsed(wire, *, hz: float = 128.0):
    """If all REPORT timestamps are identical, space them at 1/hz for LSL pacing.

    Zero-span captures (e.g. mode7_two_packet) push every sample in one burst and
    LSL inlets often drop packets; synthetic spacing keeps decode identical.
    """
    from emotiv_lsl.epocx_wire import (
        EVENT_REPORT,
        WireEvent,
        WireFile,
        WirePacket,
    )

    reports = [e for e in wire.events if e.kind == EVENT_REPORT]
    if not reports and wire.packets:
        reports = [
            WireEvent(rel_ns=p.rel_ns, kind=EVENT_REPORT, payload=p.payload)
            for p in wire.packets
        ]
    if len(reports) < 2:
        return wire
    times = [e.rel_ns for e in reports]
    if max(times) != min(times):
        return wire

    step_ns = int(round(1e9 / float(hz)))
    new_packets = [
        WirePacket(rel_ns=i * step_ns, payload=r.payload or b"")
        for i, r in enumerate(reports)
    ]
    new_events = [
        WireEvent(rel_ns=p.rel_ns, kind=EVENT_REPORT, payload=p.payload)
        for p in new_packets
    ]
    # Preserve non-REPORT events with original timestamps (ordering not critical for LSL compare).
    for e in wire.events:
        if e.kind != EVENT_REPORT:
            new_events.append(e)
        ## END if e.kind != EVENT_REPORT....
    ## END for e in wire.events....

    return WireFile(header=wire.header, packets=new_packets, events=new_events)


def write_v1_for_cpp(
    wire,
    dest: Path,
    *,
    rebase_time: bool = True,
    space_collapsed: bool = False,
) -> int:
    """Export REPORT-only EPOCXWIRE/1 for C++ replay. Returns packet count."""
    from emotiv_lsl.epocx_wire import write_wire_file_v1

    if space_collapsed:
        wire = space_reports_if_collapsed(wire)
    return write_wire_file_v1(dest, wire, rebase_time=rebase_time)


def compare_sample_arrays(
    a: np.ndarray,
    b: np.ndarray,
    *,
    atol: float = SAMPLE_ATOL,
    rtol: float = 0.0,
    label: str = "",
) -> dict:
    """Compare sample matrices; ok requires count match and allclose."""
    n = min(a.shape[0], b.shape[0]) if a.ndim == 2 and b.ndim == 2 else 0
    report = {
        "label": label,
        "n_a": int(a.shape[0]) if a.ndim >= 1 else 0,
        "n_b": int(b.shape[0]) if b.ndim >= 1 else 0,
        "n_compared": n,
        "count_match": a.shape[0] == b.shape[0] if a.ndim >= 1 and b.ndim >= 1 else False,
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


# Back-compat alias
compare_arrays = compare_sample_arrays


def compare_sample_arrays_lsl(
    a: np.ndarray,
    b: np.ndarray,
    *,
    atol: float = SAMPLE_ATOL,
    rtol: float = 0.0,
    label: str = "",
    max_lag: int = 8,
    max_count_delta: int = 2,
) -> dict:
    """Like compare_sample_arrays, but accept small LSL drop/lag for subprocess capture.

    Offline decode compares must use ``compare_sample_arrays`` (strict counts).
    """
    strict = compare_sample_arrays(a, b, atol=atol, rtol=rtol, label=label)
    if strict["ok"]:
        return strict
    if a.ndim != 2 or b.ndim != 2 or a.size == 0 or b.size == 0:
        return strict
    if abs(int(a.shape[0]) - int(b.shape[0])) > max_count_delta:
        return strict
    rows = lag_scan(a, b, atol=atol, rtol=rtol, max_lag=max_lag)
    if not rows:
        return strict
    best = rows[0]
    ok = (
        best["max_abs"] <= atol
        and best["frac_close"] >= 0.999
        and best["n"] >= max(1, min(a.shape[0], b.shape[0]) - max_count_delta)
    )
    report = dict(strict)
    report["ok"] = bool(ok)
    report["lag"] = best["lag"]
    report["max_abs"] = best["max_abs"]
    report["rmse"] = best["rmse"]
    report["n_compared"] = best["n"]
    report["frac_close"] = best["frac_close"]
    if ok:
        report["first_mismatch"] = None
        report["count_match"] = True  # treated as acceptable after lag alignment
    return report


def stream_info_snapshot(info) -> dict:
    """Extract comparable StreamInfo fields for inventory / hard schema checks."""
    desc = info.desc()
    labels: List[str] = []
    channels = desc.child("channels")
    if channels is not None:
        ch = channels.child("channel")
        while ch is not None and ch.name():
            labels.append(ch.child_value("label") or "")
            ch = ch.next_sibling()
        ## END while ch is not None....

    pho = desc.child("phopylslhelper")
    pho_keys: List[str] = []
    pho_version = None
    if pho is not None and pho.name():
        pho_version = pho.child_value("version")
        child = pho.first_child()
        while child is not None and child.name():
            pho_keys.append(child.name())
            child = child.next_sibling()
        ## END while child is not None....

    return {
        "name": info.name(),
        "type": info.type(),
        "channel_count": info.channel_count(),
        "nominal_srate": info.nominal_srate(),
        "source_id": info.source_id(),
        "channel_labels": labels,
        "manufacturer": desc.child_value("manufacturer"),
        "version": desc.child_value("version"),
        "phopylslhelper_version": pho_version,
        "phopylslhelper_keys": pho_keys,
    }


def make_headset_for_info(*, enable_motion: bool = False, enable_quality: bool = True):
    """EmotivEpocX with known serial for StreamInfo construction (no HID).

    Uses ``serial_number=`` so ``get_lsl_source_id`` / crypto never touch live HID.
    (``init_with_serial(..., cryptokey=)`` omits serial on the instance.)
    """
    from emotiv_lsl.emotiv_epoc_x import EmotivEpocX

    return EmotivEpocX(
        serial_number=FIXTURE_SERIAL,
        serial_source="hid",
        enable_electrode_quality_stream=enable_quality,
        enable_motion_data=enable_motion,
    )


def make_flutter_headset_for_info(
    *,
    enable_motion: bool = True,
    enable_quality: bool = False,
    serial: str = FLUTTER_BLE_SERIAL,
):
    """EmotivEpocX configured like Flutter offline decode (ble_hex, no CQ)."""
    from emotiv_lsl.emotiv_epoc_x import EmotivEpocX

    return EmotivEpocX(
        serial_number=serial,
        serial_source="ble_hex",
        enable_electrode_quality_stream=enable_quality,
        enable_motion_data=enable_motion,
    )


def build_synthetic_wire(n_packets: int = 16, *, serial: str = FIXTURE_SERIAL, cq: Optional[int] = None, spacing_ns: int = 7_812_500):
    """Build a WireFile with deterministic Mode-7 EEG + per-slot 16-bit CQ packets."""
    from emotiv_lsl.emotiv_epoc_x import (
        ADC_DIV_16BIT,
        aes_key_epocx_mode,
        serial_bytes_hid,
    )
    from emotiv_lsl.epocx_wire import (
        WireEvent,
        WireFile,
        WireHeader,
        WirePacket,
        encode_eeg_wire,
        EVENT_REPORT,
    )

    key = aes_key_epocx_mode(serial_bytes_hid(serial))
    packets: List[WirePacket] = []
    events: List[WireEvent] = []
    for i in range(n_packets):
        uv = [100.0 + i * 1.0 + ch * 0.5 for ch in range(14)]
        word7 = SYNTH_CQ_WORD7_CYCLE[i % len(SYNTH_CQ_WORD7_CYCLE)] if cq is None else int(cq)
        payload = encode_eeg_wire(i + 1, uv, key, cq=word7, adc_div=ADC_DIV_16BIT)
        rel_ns = i * spacing_ns
        packets.append(WirePacket(rel_ns=rel_ns, payload=payload))
        events.append(WireEvent(rel_ns=rel_ns, kind=EVENT_REPORT, payload=payload))
    ## END for i in range(n_packets)....

    header = WireHeader(
        serial=serial,
        serial_source="hid",
        adc_div=ADC_DIV_16BIT,
        eeg_hz=128.0,
        format_version=2,
    )
    return WireFile(header=header, packets=packets, events=events)


def expected_synth_cq_matrix(n_packets: int = 16, *, cq: Optional[int] = None, eeg_rate: int = 128) -> np.ndarray:
    """Compute expected sticky 14-ch 10-20 CQ grades {0, 1, 2, 4} for build_synthetic_wire."""
    from emotiv_lsl.emotiv_epoc_x import CQ_SLOT_SCALE_DIVISORS, CQ_SLOT_TO_1020_IDX, cq_norm_to_grade

    state = [0.0] * 14
    rows: List[List[float]] = []
    half_rate = max(1, int(eeg_rate) // 2)
    for i in range(n_packets):
        counter = (i + 1) & 0xFF
        word7 = SYNTH_CQ_WORD7_CYCLE[i % len(SYNTH_CQ_WORD7_CYCLE)] if cq is None else int(cq)
        slot = counter % half_rate
        if 0 <= slot < 14:
            norm = (float(word7 & 0xFFFF) * 0.25) / CQ_SLOT_SCALE_DIVISORS[slot]
            state[CQ_SLOT_TO_1020_IDX[slot]] = float(cq_norm_to_grade(norm))
        rows.append(list(state))
    ## END for i in range(n_packets)...

    return np.asarray(rows, dtype=np.float64)


def build_synthetic_flutter_session(
    session_dir: Path,
    *,
    n_eeg: int = 8,
    n_motion: int = 0,
    serial: str = FLUTTER_BLE_SERIAL,
    cq: int = FIXTURE_CQ,
    spacing_us: int = 7812,
) -> Path:
    """Write a Flutter-shaped session folder (session.json + raw.csv).

    EEG ciphertexts use EpocXMode + ``serial_bytes_ble_hex`` (not HID last-4).
    Returns ``session_dir``.
    """
    import json

    from emotiv_lsl.emotiv_epoc_x import (
        ADC_DIV_16BIT,
        aes_key_epocx_mode,
        serial_bytes_ble_hex,
    )
    from emotiv_lsl.epocx_wire import encode_eeg_wire, encode_motion_wire

    session_dir = Path(session_dir)
    session_dir.mkdir(parents=True, exist_ok=True)

    key = aes_key_epocx_mode(serial_bytes_ble_hex(serial))
    meta = {
        "deviceName": f"EPOCX ({serial})",
        "btKeyHex": serial,
        "serialSource": "ble_hex",
        "eegResolutionBits": 16,
        "keyFingerprint": "compat-synth",
        "eegRateHz": 128,
        "motionRateHz": 16,
    }
    (session_dir / "session.json").write_text(
        json.dumps(meta, indent=2) + "\n", encoding="utf-8"
    )

    lines = ["wall_ms,mono_us,kind,char_uuid,bytes"]
    row_i = 0
    for i in range(n_eeg):
        uv = [100.0 + i * 1.0 + ch * 0.5 for ch in range(14)]
        payload = encode_eeg_wire(i + 1, uv, key, cq=cq, adc_div=ADC_DIV_16BIT)
        mono_us = row_i * spacing_us
        wall_ms = 1000 + row_i
        bytes_text = " ".join(str(b) for b in payload)
        lines.append(
            f"{wall_ms},{mono_us},eeg,{FLUTTER_EEG_CHAR_UUID},{bytes_text}"
        )
        row_i += 1
    ## END for i in range(n_eeg)....

    for i in range(n_motion):
        # AccX = 1 g when raw field 4 is 8191 (matches Flutter decode_pipeline_test).
        motion = [0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0]
        payload = encode_motion_wire(i + 1, motion)
        mono_us = row_i * spacing_us
        wall_ms = 1000 + row_i
        bytes_text = " ".join(str(b) for b in payload)
        lines.append(
            f"{wall_ms},{mono_us},motion,{FLUTTER_MOTION_CHAR_UUID},{bytes_text}"
        )
        row_i += 1
    ## END for i in range(n_motion)....

    (session_dir / "raw.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return session_dir


def flutter_session_to_wire(session_dir: Path, wire_path: Path):
    """Decode a Flutter session folder and write ``.epocxwire``; return DecodeResult."""
    from emotiv_lsl.flutter_raw_csv import decode_session_folder

    return decode_session_folder(
        session_dir,
        write_csvs=False,
        wire_path=wire_path,
    )


def expected_synth_eeg_uv(sample_index: int, channel: int) -> float:
    """ADC-quantized µV matching build_synthetic_* EEG channel formulas."""
    from emotiv_lsl.emotiv_epoc_x import ADC_DIV_16BIT

    raw_uv = 100.0 + sample_index * 1.0 + channel * 0.5
    return round(raw_uv * ADC_DIV_16BIT) / ADC_DIV_16BIT


def effective_replay_speed(
    wire,
    *,
    speed: float = 0.0,
    min_duration: float = 4.0,
    force_asap: bool = False,
) -> Tuple[float, float]:
    """Return (effective_speed, span_s) for C++ --replay-speed pacing."""
    from emotiv_lsl.epocx_wire import EVENT_REPORT

    report_ns = [e.rel_ns for e in wire.events if e.kind == EVENT_REPORT] or [
        p.rel_ns for p in wire.packets
    ]
    if report_ns:
        span_s = (max(report_ns) - min(report_ns)) * 1e-9
    else:
        span_s = 0.0
    if force_asap:
        return 0.0, span_s
    if speed > 0:
        return float(speed), span_s
    if span_s > 0:
        return max(span_s / max(float(min_duration), 0.5), 1e-6), span_s
    return 1.0, span_s


def _open_inlet(info):
    from pylsl import StreamInlet

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
    env: Optional[dict] = None,
    resolve_timeout_s: float = 30.0,
    drain_s: float = 2.0,
    kill_stale_windows: bool = False,
    quiet: bool = False,
) -> Dict[str, np.ndarray]:
    """Launch C++ replay and pull LSL samples while it waits for consumers."""
    from pylsl import ContinuousResolver

    from emotiv_lsl.emotiv_epoc_x import EmotivEpocX
    from emotiv_lsl.epocx_wire import EEG_STREAM_NAME, read_wire_file

    wire_hdr = read_wire_file(wire_v1).header
    expected_source_id = EmotivEpocX(
        serial_number=wire_hdr.serial,
        serial_source=wire_hdr.serial_source,
    ).get_lsl_source_id()

    if kill_stale_windows and os.name == "nt":
        # Opt-in: avoids surprising local sessions; CLI may set True.
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
    if not quiet:
        print("  cmd:", " ".join(cmd))
    child_env = dict(env if env is not None else os.environ)
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
            if not quiet and any(
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

    inlets = {}
    buffers: Dict[str, List[List[float]]] = {n: [] for n in stream_names}
    resolve_deadline = time.monotonic() + resolve_timeout_s

    try:
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
                results = [
                    r for r in resolver.results() if r.source_id() == expected_source_id
                ]
                if not results:
                    continue
                inlets[name] = _open_inlet(results[0])
                if not quiet:
                    print(f"  linked {name!r}")
            ## END for name, resolver in resolvers.items()....

            if len(inlets) == len(stream_names):
                break
            time.sleep(0.05)
        ## END while time.monotonic() < resolve_deadline....

        if not inlets:
            raise RuntimeError(
                "C++ LSL streams never appeared.\n" + "\n".join(log_lines[-40:])
            )

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
        if not quiet:
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
        if proc.stdout is not None:
            try:
                proc.stdout.close()
            except OSError:
                pass
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
    if not quiet:
        print(f"  cpp exit={rc} elapsed={elapsed:.2f}s")

    out: Dict[str, np.ndarray] = {}
    for name in stream_names:
        rows = buffers.get(name) or []
        if rows:
            arr = np.asarray(rows, dtype=np.float64)
        else:
            arr = np.zeros((0, 14), dtype=np.float64)
        out[name] = arr
        if not quiet:
            print(f"  {name}: n={arr.shape[0]} shape={arr.shape}")
    ## END for name in stream_names....

    if rc not in (0, None) and out[EEG_STREAM_NAME].shape[0] == 0:
        raise RuntimeError(
            f"C++ failed (exit={rc}) with no EEG samples.\n" + "\n".join(log_lines[-40:])
        )
    return out


def _aligned_pair(a: np.ndarray, b: np.ndarray, lag: int) -> Tuple[np.ndarray, np.ndarray]:
    if lag >= 0:
        n = min(a.shape[0] - lag, b.shape[0])
        return a[lag : lag + n], b[:n]
    n = min(a.shape[0], b.shape[0] + lag)
    return a[:n], b[-lag : -lag + n]


def lag_scan(
    a: np.ndarray,
    b: np.ndarray,
    *,
    atol: float,
    rtol: float,
    max_lag: int = 64,
) -> List[dict]:
    rows: List[dict] = []
    for lag in range(-max_lag, max_lag + 1):
        if lag >= 0 and lag >= a.shape[0]:
            continue
        if lag < 0 and -lag >= b.shape[0]:
            continue
        aa, bb = _aligned_pair(a, b, lag)
        if aa.shape[0] == 0:
            continue
        diff = np.abs(aa - bb)
        close = np.isclose(aa, bb, atol=atol, rtol=rtol)
        rows.append(
            {
                "lag": lag,
                "n": int(aa.shape[0]),
                "max_abs": float(diff.max()),
                "rmse": float(np.sqrt(np.mean((aa - bb) ** 2))),
                "frac_close": float(np.mean(close)),
            }
        )
    ## END for lag in range(-max_lag, max_lag + 1)....

    rows.sort(key=lambda r: (r["max_abs"], -r["frac_close"], -r["n"]))
    return rows


def diagnose_streams(
    py_streams: Dict[str, np.ndarray],
    cpp_streams: Dict[str, np.ndarray],
    stream_names: Sequence[str],
    *,
    atol: float,
    rtol: float,
    effective_speed: float,
    span_s: float,
    min_duration: float,
) -> None:
    print("\n=== diagnose ===")
    if effective_speed > 2.0 and span_s > 10.0:
        print(
            f"  HINT: C++ paced at speed={effective_speed:.3g} "
            f"(~{min_duration:.1f}s wall for {span_s:.1f}s wire). "
            "LSL often drops samples above ~2–4× realtime; re-run with e.g.\n"
            f"    --min-duration {max(span_s, 30):.0f}   # ~1× realtime\n"
            "    --speed 1"
        )
    for name in stream_names:
        a = py_streams.get(name)
        b = cpp_streams.get(name)
        if a is None or b is None or a.size == 0 or b.size == 0:
            print(f"  {name}: skip diagnose (missing/empty)")
            continue
        dn = int(a.shape[0]) - int(b.shape[0])
        print(f"  {name}: n_py={a.shape[0]} n_cpp={b.shape[0]} Δn={dn:+d}")
        rows = lag_scan(
            a, b, atol=atol, rtol=rtol, max_lag=min(128, max(a.shape[0], b.shape[0]) // 4)
        )
        best = rows[0]
        print(
            f"    best lag={best['lag']:+d}  n={best['n']}  "
            f"max_abs={best['max_abs']:.6g}  frac_close={best['frac_close']:.4f}"
        )
        if best["max_abs"] <= atol and best["frac_close"] >= 0.999:
            print(
                "    → values match after lag alignment; difference is almost certainly "
                "LSL capture drops / misalignment, not a decode bug."
            )
        elif abs(dn) > 0 and best["max_abs"] > atol:
            print(
                "    → count mismatch + no clean lag; likely multiple LSL drops mid-stream. "
                "Slow the C++ replay and re-compare."
            )
        else:
            print(
                "    → residual error after best lag; possible real decode difference. "
                "Inspect first mismatch samples / packet types next."
            )
        near = [r for r in rows if abs(r["lag"]) <= 3]
        near.sort(key=lambda r: r["lag"])
        for r in near:
            print(
                f"      lag={r['lag']:+d}  max_abs={r['max_abs']:.6g}  "
                f"frac_close={r['frac_close']:.4f}"
            )
        ## END for r in near....
    ## END for name in stream_names....
