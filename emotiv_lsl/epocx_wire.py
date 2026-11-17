"""Epoc X `.epocxwire` format: Mode-7 HID-like packets timed from XDF.

Format (text header + binary records, little-endian):
  EPOCXWIRE/1
  serial=<string>
  serial_source=hid|ble_hex
  adc_div=7.8
  eeg_hz=128
  <blank line>
  records: uint64 rel_ns, uint16 nbytes, nbytes payload (32 EEG cipher or 20 motion)
"""
from __future__ import annotations

import math
import struct
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO, Dict, Iterator, List, Optional, Sequence, Tuple, Union

from Crypto.Cipher import AES

from emotiv_lsl.emotiv_epoc_x import (
    ADC_DIV_16BIT,
    EEG_TYPE,
    EPOCX_RAW_EEG_ID_MODE7,
    MOTION_TYPE,
    aes_key_epocx_mode,
    serial_bytes_ble_hex,
    serial_bytes_hid,
)

MAGIC = "EPOCXWIRE/1"
EEG_STREAM_NAME = "Epoc X"
QUALITY_STREAM_NAME = "Epoc X eQuality"
MOTION_STREAM_NAME = "Epoc X Motion"
DEFAULT_SERIAL = "UD20221202006756"


@dataclass
class WireHeader:
    serial: str = DEFAULT_SERIAL
    serial_source: str = "hid"
    adc_div: float = ADC_DIV_16BIT
    eeg_hz: float = 128.0


@dataclass
class WirePacket:
    rel_ns: int
    payload: bytes


@dataclass
class WireFile:
    header: WireHeader
    packets: List[WirePacket] = field(default_factory=list)


def _aes_key_from_header(header: WireHeader) -> bytes:
    if header.serial_source == "ble_hex":
        s4 = serial_bytes_ble_hex(header.serial)
    else:
        s4 = serial_bytes_hid(header.serial)
    return aes_key_epocx_mode(s4)


def encrypt_eeg_plain(plain32: bytes, key16: bytes) -> bytes:
    """AES-128-ECB encrypt then XOR 0x55 (inverse of decrypt_eeg_packet)."""
    if len(plain32) != 32:
        raise ValueError(f"plain needs 32 bytes, got {len(plain32)}")
    cipher = AES.new(key16, AES.MODE_ECB)
    encrypted = cipher.encrypt(bytes(plain32))
    return bytes(b ^ 0x55 for b in encrypted)


def pack_mode7_plain(
    counter: int,
    channels_uv: Sequence[float],
    *,
    cq: int = 0,
    adc_div: float = ADC_DIV_16BIT,
) -> bytes:
    """Build Mode-7 plaintext from 14 µV channels (output order AF3…AF4)."""
    if len(channels_uv) != 14:
        raise ValueError(f"need 14 EEG channels, got {len(channels_uv)}")
    words = [0] * 15
    for i, uv in enumerate(channels_uv):
        raw = int(round(float(uv) * adc_div))
        words[EPOCX_RAW_EEG_ID_MODE7[i]] = max(0, min(0xFFFF, raw))
    words[7] = int(cq) & 0x7F
    return bytes([counter & 0xFF, EEG_TYPE & 0xFF]) + struct.pack("<15H", *words)


def encode_eeg_wire(
    counter: int,
    channels_uv: Sequence[float],
    key16: bytes,
    *,
    cq: int = 0,
    adc_div: float = ADC_DIV_16BIT,
) -> bytes:
    plain = pack_mode7_plain(counter, channels_uv, cq=cq, adc_div=adc_div)
    return encrypt_eeg_plain(plain, key16)


def encode_motion_wire(
    counter: int,
    motion10: Sequence[float],
) -> bytes:
    """Pack Android 20 B motion from [Q0,Q1,Q2,Q3,AccX,AccY,AccZ,MagX,MagY,MagZ].

    Q0 is derived on decode and ignored here. Inverse of correctMotionDirection.
    """
    if len(motion10) < 10:
        raise ValueError(f"need 10 motion channels, got {len(motion10)}")
    q1 = float(motion10[1]) * 16384.0
    q2 = float(motion10[2]) * 16384.0
    q3 = float(motion10[3]) * 16384.0
    accx = float(motion10[4]) * 8191.0
    accy = float(motion10[5]) * 8191.0
    accz = float(motion10[6]) * 8191.0
    magx = float(motion10[7]) * 6.0
    magy = float(motion10[8]) * 6.0
    magz = float(motion10[9]) * 6.0

    # Inverse correctMotionDirection:
    # q1=raw1, q2=-raw0, q3=raw2, accx=raw4, accy=-raw3, accz=raw5,
    # magx=-raw7, magy=raw6, magz=raw8
    raws = [
        int(round(-q2)),  # raw0
        int(round(q1)),  # raw1
        int(round(q3)),  # raw2
        int(round(-accy)),  # raw3
        int(round(accx)),  # raw4
        int(round(accz)),  # raw5
        int(round(magy)),  # raw6
        int(round(-magx)),  # raw7
        int(round(magz)),  # raw8
    ]
    buf = bytearray(20)
    buf[0] = counter & 0xFF
    buf[1] = MOTION_TYPE
    for i, v in enumerate(raws):
        v = max(-32768, min(32767, v))
        struct.pack_into(">h", buf, 2 + 2 * i, v)
    return bytes(buf)


def write_wire_file(path: Union[str, Path], wire: WireFile) -> None:
    path = Path(path)
    with path.open("wb") as f:
        f.write(f"{MAGIC}\n".encode("utf-8"))
        f.write(f"serial={wire.header.serial}\n".encode("utf-8"))
        f.write(f"serial_source={wire.header.serial_source}\n".encode("utf-8"))
        f.write(f"adc_div={wire.header.adc_div}\n".encode("utf-8"))
        f.write(f"eeg_hz={wire.header.eeg_hz}\n".encode("utf-8"))
        f.write(b"\n")
        for pkt in wire.packets:
            f.write(struct.pack("<QH", pkt.rel_ns, len(pkt.payload)))
            f.write(pkt.payload)


def _parse_header_lines(lines: List[str]) -> WireHeader:
    if not lines or lines[0].strip() != MAGIC:
        got = repr(lines[0]) if lines else "None"
        raise ValueError(f"Bad magic; expected {MAGIC!r}, got {got}")
    header = WireHeader()
    for line in lines[1:]:
        line = line.strip()
        if not line:
            break
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip()
        if k == "serial":
            header.serial = v
        elif k == "serial_source":
            header.serial_source = v
        elif k == "adc_div":
            header.adc_div = float(v)
        elif k == "eeg_hz":
            header.eeg_hz = float(v)
    return header


def read_wire_file(path: Union[str, Path]) -> WireFile:
    path = Path(path)
    with path.open("rb") as f:
        header_lines: List[str] = []
        while True:
            raw = f.readline()
            if not raw:
                break
            line = raw.decode("utf-8", errors="replace")
            if line in ("\n", "\r\n") and header_lines:
                break
            header_lines.append(line.rstrip("\r\n"))
        header = _parse_header_lines(header_lines)
        packets: List[WirePacket] = []
        while True:
            meta = f.read(10)
            if not meta:
                break
            if len(meta) < 10:
                raise ValueError("Truncated packet header in wire file")
            rel_ns, nbytes = struct.unpack("<QH", meta)
            payload = f.read(nbytes)
            if len(payload) != nbytes:
                raise ValueError("Truncated packet payload in wire file")
            packets.append(WirePacket(rel_ns=rel_ns, payload=payload))
    return WireFile(header=header, packets=packets)


def _stream_by_name(streams: list, name: str) -> Optional[dict]:
    for s in streams:
        info = s.get("info") or {}
        names = info.get("name") or []
        if names and names[0] == name:
            return s
    return None


def _nearest_cq(quality_ts: Sequence[float], quality_data, t: float) -> int:
    if quality_ts is None or len(quality_ts) == 0:
        return 0
    import numpy as np

    idx = int(np.argmin(np.abs(np.asarray(quality_ts) - t)))
    sample = quality_data[idx]
    # Broadcast CQ: first channel is enough
    try:
        return int(round(float(sample[0]))) & 0x7F
    except Exception:
        return 0


def xdf_to_wire(
    xdf_path: Union[str, Path],
    *,
    serial: str = DEFAULT_SERIAL,
    serial_source: str = "hid",
    adc_div: float = ADC_DIV_16BIT,
) -> WireFile:
    """Load an Epoc X LabRecorder XDF and build timed wire packets."""
    import numpy as np
    import pyxdf

    streams, _header = pyxdf.load_xdf(str(xdf_path))
    eeg = _stream_by_name(streams, EEG_STREAM_NAME)
    if eeg is None:
        raise ValueError(f"XDF missing stream named {EEG_STREAM_NAME!r}")

    eeg_ts = eeg["time_stamps"]
    eeg_data = eeg["time_series"]
    if eeg_data.ndim != 2 or eeg_data.shape[1] < 14:
        raise ValueError(f"EEG stream unexpected shape {getattr(eeg_data, 'shape', None)}")

    quality = _stream_by_name(streams, QUALITY_STREAM_NAME)
    q_ts = quality["time_stamps"] if quality is not None else None
    q_data = quality["time_series"] if quality is not None else None

    motion = _stream_by_name(streams, MOTION_STREAM_NAME)
    m_ts = motion["time_stamps"] if motion is not None else None
    m_data = motion["time_series"] if motion is not None else None

    t0 = float(eeg_ts[0])
    if m_ts is not None and len(m_ts) > 0:
        t0 = min(t0, float(m_ts[0]))

    header = WireHeader(
        serial=serial,
        serial_source=serial_source,
        adc_div=adc_div,
        eeg_hz=float((eeg.get("info") or {}).get("nominal_srate", [128])[0] or 128),
    )
    key = _aes_key_from_header(header)

    events: List[Tuple[float, str, int]] = []
    for i, t in enumerate(eeg_ts):
        events.append((float(t), "eeg", i))
    if m_ts is not None:
        for i, t in enumerate(m_ts):
            events.append((float(t), "motion", i))
    events.sort(key=lambda e: e[0])

    packets: List[WirePacket] = []
    eeg_counter = 0
    motion_counter = 0
    for t, kind, idx in events:
        rel_ns = int(round((t - t0) * 1e9))
        if kind == "eeg":
            sample = eeg_data[idx][:14]
            cq = _nearest_cq(q_ts, q_data, t) if q_ts is not None else 0
            payload = encode_eeg_wire(
                eeg_counter, sample, key, cq=cq, adc_div=adc_div
            )
            eeg_counter = (eeg_counter + 1) & 0xFF
            packets.append(WirePacket(rel_ns=rel_ns, payload=payload))
        else:
            sample = m_data[idx]
            if len(sample) < 10:
                continue
            payload = encode_motion_wire(motion_counter, sample)
            motion_counter = (motion_counter + 1) & 0xFF
            packets.append(WirePacket(rel_ns=rel_ns, payload=payload))

    return WireFile(header=header, packets=packets)


class WireReplaySource:
    """Timed packet source replacing hid_device.read for `--replay`."""

    def __init__(
        self,
        wire: WireFile,
        *,
        speed: float = 1.0,
    ):
        self.wire = wire
        self.speed = float(speed)
        self._idx = 0
        self._t0_wall: Optional[float] = None
        self._eof = False

    @classmethod
    def from_path(cls, path: Union[str, Path], *, speed: float = 1.0) -> "WireReplaySource":
        path = Path(path)
        if path.suffix.lower() == ".xdf":
            wire = xdf_to_wire(path)
        else:
            wire = read_wire_file(path)
        return cls(wire, speed=speed)

    @property
    def header(self) -> WireHeader:
        return self.wire.header

    def apply_serial_to(self, headset) -> None:
        """Inject serial into EmotivEpocX before crypto use."""
        headset.serial_number = self.header.serial
        if hasattr(headset, "serial_source"):
            headset.serial_source = self.header.serial_source
        if hasattr(headset, "eeg_resolution_bits"):
            headset.eeg_resolution_bits = (
                0x10 if abs(self.header.adc_div - ADC_DIV_16BIT) < 0.01 else 0x0E
            )
        # Rebuild cipher with injected serial.
        from Crypto.Cipher import AES

        headset.cipher = AES.new(headset.get_crypto_key(), AES.MODE_ECB)

    def read(self, size: int = 32) -> bytes:
        """Return next payload, sleeping to honor relative timestamps.

        Returns empty bytes on EOF (caller should stop).
        """
        if self._idx >= len(self.wire.packets):
            self._eof = True
            return b""
        pkt = self.wire.packets[self._idx]
        self._idx += 1

        now = time.perf_counter()
        if self._t0_wall is None:
            self._t0_wall = now

        if self.speed > 0:
            target = self._t0_wall + (pkt.rel_ns * 1e-9) / self.speed
            delay = target - time.perf_counter()
            if delay > 0:
                time.sleep(delay)

        return pkt.payload

    @property
    def eof(self) -> bool:
        return self._eof or self._idx >= len(self.wire.packets)
