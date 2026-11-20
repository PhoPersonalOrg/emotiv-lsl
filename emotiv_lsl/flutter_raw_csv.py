"""Decode Flutter logger session folders (`raw.csv` + `session.json`).

Supports:
  - Current header: wall_ms,mono_us,kind,char_uuid,bytes
  - Legacy header:  wall_ms,mono_us,kind,bytes

Classification prefers payload length over the advisory CSV ``kind`` column.
"""
from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, List, Optional, Sequence, Tuple, Union

from emotiv_lsl.emotiv_epoc_x import (
    ADC_DIV_14BIT,
    ADC_DIV_16BIT,
    EmotivEpocX,
)
from emotiv_lsl.epocx_wire import WireFile, WireHeader, WirePacket, write_wire_file

EEG_CHANNELS = [
    "AF3",
    "F7",
    "F3",
    "FC5",
    "T7",
    "P7",
    "O1",
    "O2",
    "P8",
    "T8",
    "FC6",
    "F4",
    "F8",
    "AF4",
]
MOTION_CHANNELS = [
    "Q0",
    "Q1",
    "Q2",
    "Q3",
    "AccX",
    "AccY",
    "AccZ",
    "MagX",
    "MagY",
    "MagZ",
]


@dataclass
class RawRow:
    wall_ms: int
    mono_us: int
    kind: str
    char_uuid: str
    payload: bytes


@dataclass
class SessionMeta:
    bt_key_hex: str
    serial_source: str = "ble_hex"
    eeg_resolution_bits: int = 16
    device_name: str = ""
    raw: dict = field(default_factory=dict)

    @property
    def adc_div(self) -> float:
        return ADC_DIV_16BIT if self.eeg_resolution_bits >= 16 else ADC_DIV_14BIT

    @property
    def eeg_resolution_bits_hex(self) -> int:
        """EmotivEpocX field: 0x10 (16-bit) or 0x0E (14-bit)."""
        return 0x10 if self.eeg_resolution_bits >= 16 else 0x0E


@dataclass
class DecodedSample:
    wall_ms: int
    mono_us: int
    values: List[float]


@dataclass
class DecodeResult:
    eeg: List[DecodedSample] = field(default_factory=list)
    motion: List[DecodedSample] = field(default_factory=list)
    skipped: int = 0
    failed: int = 0


def load_session_meta(session_dir: Union[str, Path]) -> SessionMeta:
    path = Path(session_dir) / "session.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    bits = int(data.get("eegResolutionBits", 16))
    return SessionMeta(
        bt_key_hex=str(data.get("btKeyHex", "")).strip().upper(),
        serial_source=str(data.get("serialSource", "ble_hex")),
        eeg_resolution_bits=bits,
        device_name=str(data.get("deviceName", "")),
        raw=data,
    )


def _parse_bytes_field(text: str) -> bytes:
    text = text.strip()
    if not text:
        return b""
    return bytes(int(tok) for tok in text.split())


def iter_raw_rows(raw_csv: Union[str, Path]) -> Iterator[RawRow]:
    path = Path(raw_csv)
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        try:
            header = next(reader)
        except StopIteration:
            return
        header_l = [h.strip().lower() for h in header]
        has_uuid = "char_uuid" in header_l
        for row in reader:
            if not row or all(not c.strip() for c in row):
                continue
            # Bytes may contain spaces; csv.reader already handled quoting.
            # Our writer does not quote; bytes is the last field joined by spaces
            # after a fixed number of commas — re-join if split too aggressively.
            if has_uuid:
                if len(row) < 5:
                    # Unquoted space-separated bytes → csv.reader splits on commas
                    # only; spaces stay in last cell. Require ≥5 cells.
                    continue
                wall_s, mono_s, kind, uuid, *byte_parts = row
                bytes_text = ",".join(byte_parts) if len(byte_parts) > 1 else byte_parts[0]
            else:
                if len(row) < 4:
                    continue
                wall_s, mono_s, kind, *byte_parts = row
                uuid = ""
                bytes_text = ",".join(byte_parts) if len(byte_parts) > 1 else byte_parts[0]
            yield RawRow(
                wall_ms=int(wall_s),
                mono_us=int(mono_s),
                kind=kind.strip().lower(),
                char_uuid=uuid.strip(),
                payload=_parse_bytes_field(bytes_text),
            )


def classify_payload(payload: bytes, kind_hint: str = "") -> str:
    """Return ``eeg``, ``motion``, or ``unknown`` from length (kind is advisory)."""
    n = len(payload)
    if n == 20:
        return "motion"
    if n >= 32 and n % 32 == 0:
        return "eeg"
    if n >= 20 and n % 20 == 0:
        return "motion"
    if n >= 32:
        return "eeg"
    if n >= 20:
        return "motion"
    hint = kind_hint.strip().lower()
    if hint in ("eeg", "motion"):
        return hint
    return "unknown"


def split_packages(payload: bytes, modality: str) -> List[bytes]:
    if modality == "eeg":
        stride = 32
    elif modality == "motion":
        stride = 20
    else:
        return [payload] if payload else []
    if len(payload) < stride:
        return []
    count = len(payload) // stride
    return [payload[i * stride : (i + 1) * stride] for i in range(count)]


def make_headset(meta: SessionMeta) -> EmotivEpocX:
    if not meta.bt_key_hex:
        raise ValueError("session.json missing btKeyHex")
    return EmotivEpocX(
        serial_number=meta.bt_key_hex,
        serial_source=meta.serial_source or "ble_hex",
        eeg_resolution_bits=meta.eeg_resolution_bits_hex,
        enable_motion_data=True,
        enable_electrode_quality_stream=False,
    )


def decode_rows(
    rows: Sequence[RawRow],
    headset: EmotivEpocX,
) -> DecodeResult:
    out = DecodeResult()
    for row in rows:
        modality = classify_payload(row.payload, row.kind)
        packages = split_packages(row.payload, modality)
        if not packages:
            out.skipped += 1
            continue
        for pkg in packages:
            try:
                decoded, _cq = headset.decode_data(pkg)
            except Exception:
                out.failed += 1
                continue
            if decoded is None:
                out.failed += 1
                continue
            sample = DecodedSample(
                wall_ms=row.wall_ms,
                mono_us=row.mono_us,
                values=[float(v) for v in decoded],
            )
            if len(decoded) == 14:
                out.eeg.append(sample)
            elif len(decoded) == 10:
                out.motion.append(sample)
            else:
                out.failed += 1
    return out


def write_offline_csvs(
    result: DecodeResult,
    out_dir: Union[str, Path],
) -> Tuple[Path, Path]:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    eeg_path = out_dir / "eeg_offline.csv"
    motion_path = out_dir / "motion_offline.csv"

    with eeg_path.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["wall_ms", "mono_us", *EEG_CHANNELS])
        for s in result.eeg:
            w.writerow([s.wall_ms, s.mono_us, *[f"{v:.9g}" for v in s.values]])

    with motion_path.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["wall_ms", "mono_us", *MOTION_CHANNELS])
        for s in result.motion:
            w.writerow([s.wall_ms, s.mono_us, *[f"{v:.9g}" for v in s.values]])

    return eeg_path, motion_path


def rows_to_wire(
    rows: Sequence[RawRow],
    meta: SessionMeta,
) -> WireFile:
    header = WireHeader(
        serial=meta.bt_key_hex,
        serial_source=meta.serial_source or "ble_hex",
        adc_div=meta.adc_div,
        eeg_hz=128.0,
    )
    if not rows:
        return WireFile(header=header, packets=[])

    t0 = rows[0].mono_us
    packets: List[WirePacket] = []
    for row in rows:
        modality = classify_payload(row.payload, row.kind)
        packages = split_packages(row.payload, modality)
        if not packages:
            # Preserve odd-sized payloads for forensic replay.
            if row.payload:
                rel_ns = max(0, (row.mono_us - t0) * 1000)
                packets.append(WirePacket(rel_ns=rel_ns, payload=row.payload))
            continue
        rel_ns = max(0, (row.mono_us - t0) * 1000)
        for pkg in packages:
            packets.append(WirePacket(rel_ns=rel_ns, payload=pkg))
    return WireFile(header=header, packets=packets)


def decode_session_folder(
    session_dir: Union[str, Path],
    *,
    write_csvs: bool = True,
    wire_path: Optional[Union[str, Path]] = None,
) -> DecodeResult:
    session_dir = Path(session_dir)
    meta = load_session_meta(session_dir)
    rows = list(iter_raw_rows(session_dir / "raw.csv"))
    headset = make_headset(meta)
    result = decode_rows(rows, headset)
    if write_csvs:
        write_offline_csvs(result, session_dir)
    if wire_path is not None:
        wire = rows_to_wire(rows, meta)
        write_wire_file(wire_path, wire)
    return result
