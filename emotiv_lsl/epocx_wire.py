"""Epoc X `.epocxwire` format: timed HID session capture / Mode-7 wire packets.

Format (text header + binary records, little-endian):

  EPOCXWIRE/2
  serial=<string>
  serial_source=hid|ble_hex
  adc_div=7.8
  eeg_hz=128
  vid=...  (optional device metadata)
  ...
  <blank line>
  events:
    REPORT: uint64 rel_ns, uint8 type=1, uint16 nbytes, nbytes payload
    EMPTY:  uint64 rel_ns, uint8 type=2
    ERROR:  uint64 rel_ns, uint8 type=3, uint16 msg_len, utf-8 message
    OPEN:   uint64 rel_ns, uint8 type=4
    CLOSE:  uint64 rel_ns, uint8 type=5

Legacy EPOCXWIRE/1 uses records: uint64 rel_ns, uint16 nbytes, nbytes payload
(all treated as REPORT). Writers always emit v2. XDF→wire is synthetic (re-encrypted);
live ``--record`` is the exact-capture path.
"""
from __future__ import annotations

import platform
import struct
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import (
    Any,
    BinaryIO,
    Dict,
    List,
    Optional,
    Protocol,
    Sequence,
    Tuple,
    Union,
    runtime_checkable,
)

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

MAGIC_V1 = "EPOCXWIRE/1"
MAGIC_V2 = "EPOCXWIRE/2"
MAGIC = MAGIC_V2  # default for new writers

EVENT_REPORT = 1
EVENT_EMPTY = 2
EVENT_ERROR = 3
EVENT_OPEN = 4
EVENT_CLOSE = 5

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
    # Optional device / capture metadata (EPOCXWIRE/2)
    vid: Optional[int] = None
    pid: Optional[int] = None
    manufacturer: Optional[str] = None
    product: Optional[str] = None
    usage: Optional[int] = None
    usage_page: Optional[int] = None
    interface_number: Optional[int] = None
    path: Optional[str] = None
    release_number: Optional[int] = None
    hidapi: Optional[str] = None
    host: Optional[str] = None
    captured_at_utc: Optional[str] = None
    read_size: Optional[int] = None
    read_timeout_ms: Optional[int] = None
    report_descriptor: Optional[str] = None  # hex, for future WinUHid feeder
    format_version: int = 2


@dataclass
class WirePacket:
    """Legacy REPORT-only packet (v1 / synthetic XDF path)."""

    rel_ns: int
    payload: bytes


@dataclass
class WireEvent:
    rel_ns: int
    kind: int
    payload: bytes = b""
    message: str = ""


@dataclass
class WireFile:
    header: WireHeader
    packets: List[WirePacket] = field(default_factory=list)
    events: List[WireEvent] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.events and not self.packets:
            self.packets = [
                WirePacket(rel_ns=e.rel_ns, payload=e.payload)
                for e in self.events
                if e.kind == EVENT_REPORT
            ]
        elif self.packets and not self.events:
            self.events = [
                WireEvent(rel_ns=p.rel_ns, kind=EVENT_REPORT, payload=p.payload)
                for p in self.packets
            ]


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
    ## END for i, uv in enumerate(channels_uv)....

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
    ## END for i, v in enumerate(raws)....

    return bytes(buf)


def header_from_hid_device(
    device: Dict[str, Any],
    *,
    serial_source: str = "hid",
    adc_div: float = ADC_DIV_16BIT,
    eeg_hz: float = 128.0,
    read_size: Optional[int] = None,
    read_timeout_ms: Optional[int] = None,
) -> WireHeader:
    """Build a WireHeader from a hidapi enumerate() dict."""
    serial = device.get("serial_number") or DEFAULT_SERIAL
    if isinstance(serial, bytes):
        serial = serial.decode("utf-8", errors="replace")
    path = device.get("path")
    if isinstance(path, bytes):
        path = path.decode("utf-8", errors="replace")
    return WireHeader(
        serial=str(serial),
        serial_source=serial_source,
        adc_div=adc_div,
        eeg_hz=eeg_hz,
        vid=device.get("vendor_id"),
        pid=device.get("product_id"),
        manufacturer=device.get("manufacturer_string") or None,
        product=device.get("product_string") or None,
        usage=device.get("usage"),
        usage_page=device.get("usage_page"),
        interface_number=device.get("interface_number"),
        path=str(path) if path is not None else None,
        release_number=device.get("release_number"),
        hidapi="hidapi",
        host=platform.node(),
        captured_at_utc=datetime.now(timezone.utc).isoformat(),
        read_size=read_size,
        read_timeout_ms=read_timeout_ms,
        format_version=2,
    )


def _write_header(f: BinaryIO, header: WireHeader, *, magic: str = MAGIC_V2) -> None:
    f.write(f"{magic}\n".encode("utf-8"))
    f.write(f"serial={header.serial}\n".encode("utf-8"))
    f.write(f"serial_source={header.serial_source}\n".encode("utf-8"))
    f.write(f"adc_div={header.adc_div}\n".encode("utf-8"))
    f.write(f"eeg_hz={header.eeg_hz}\n".encode("utf-8"))
    optional: List[Tuple[str, Optional[Union[str, int, float]]]] = [
        ("vid", header.vid),
        ("pid", header.pid),
        ("manufacturer", header.manufacturer),
        ("product", header.product),
        ("usage", header.usage),
        ("usage_page", header.usage_page),
        ("interface_number", header.interface_number),
        ("path", header.path),
        ("release_number", header.release_number),
        ("hidapi", header.hidapi),
        ("host", header.host),
        ("captured_at_utc", header.captured_at_utc),
        ("read_size", header.read_size),
        ("read_timeout_ms", header.read_timeout_ms),
        ("report_descriptor", header.report_descriptor),
    ]
    for key, val in optional:
        if val is None:
            continue
        f.write(f"{key}={val}\n".encode("utf-8"))
    ## END for key, val in optional....

    f.write(b"\n")


def _write_event(f: BinaryIO, event: WireEvent) -> None:
    f.write(struct.pack("<QB", event.rel_ns, event.kind & 0xFF))
    if event.kind == EVENT_REPORT:
        payload = event.payload or b""
        f.write(struct.pack("<H", len(payload)))
        f.write(payload)
    elif event.kind == EVENT_ERROR:
        msg = (event.message or "").encode("utf-8")
        f.write(struct.pack("<H", len(msg)))
        f.write(msg)
    # EMPTY / OPEN / CLOSE: no extra payload


def write_wire_file(path: Union[str, Path], wire: WireFile) -> None:
    """Write a wire file as EPOCXWIRE/2 (events). Legacy packets become REPORT events."""
    path = Path(path)
    events = list(wire.events)
    if not events and wire.packets:
        events = [
            WireEvent(rel_ns=p.rel_ns, kind=EVENT_REPORT, payload=p.payload)
            for p in wire.packets
        ]

    header = wire.header
    header.format_version = 2
    with path.open("wb") as f:
        _write_header(f, header, magic=MAGIC_V2)
        for ev in events:
            _write_event(f, ev)
        ## END for ev in events....


def _parse_header_lines(lines: List[str]) -> WireHeader:
    if not lines:
        raise ValueError("Bad magic; expected EPOCXWIRE/1 or EPOCXWIRE/2, got None")
    magic = lines[0].strip()
    if magic == MAGIC_V1:
        version = 1
    elif magic == MAGIC_V2:
        version = 2
    else:
        raise ValueError(
            f"Bad magic; expected {MAGIC_V1!r} or {MAGIC_V2!r}, got {magic!r}"
        )
    header = WireHeader(format_version=version)
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
        elif k == "vid":
            header.vid = int(v, 0)
        elif k == "pid":
            header.pid = int(v, 0)
        elif k == "manufacturer":
            header.manufacturer = v
        elif k == "product":
            header.product = v
        elif k == "usage":
            header.usage = int(v, 0)
        elif k == "usage_page":
            header.usage_page = int(v, 0)
        elif k == "interface_number":
            header.interface_number = int(v, 0)
        elif k == "path":
            header.path = v
        elif k == "release_number":
            header.release_number = int(v, 0)
        elif k == "hidapi":
            header.hidapi = v
        elif k == "host":
            header.host = v
        elif k == "captured_at_utc":
            header.captured_at_utc = v
        elif k == "read_size":
            header.read_size = int(v, 0)
        elif k == "read_timeout_ms":
            header.read_timeout_ms = int(v, 0)
        elif k == "report_descriptor":
            header.report_descriptor = v
    ## END for line in lines[1:]....

    return header


def _read_events_v1(f: BinaryIO) -> List[WireEvent]:
    events: List[WireEvent] = []
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
        events.append(WireEvent(rel_ns=rel_ns, kind=EVENT_REPORT, payload=payload))
    ## END while True....

    return events


def _read_events_v2(f: BinaryIO) -> List[WireEvent]:
    events: List[WireEvent] = []
    while True:
        meta = f.read(9)  # u64 + u8
        if not meta:
            break
        if len(meta) < 9:
            raise ValueError("Truncated event header in wire file")
        rel_ns, kind = struct.unpack("<QB", meta)
        if kind == EVENT_REPORT:
            nb = f.read(2)
            if len(nb) < 2:
                raise ValueError("Truncated REPORT length")
            (nbytes,) = struct.unpack("<H", nb)
            payload = f.read(nbytes)
            if len(payload) != nbytes:
                raise ValueError("Truncated REPORT payload")
            events.append(WireEvent(rel_ns=rel_ns, kind=kind, payload=payload))
        elif kind == EVENT_ERROR:
            nb = f.read(2)
            if len(nb) < 2:
                raise ValueError("Truncated ERROR length")
            (nbytes,) = struct.unpack("<H", nb)
            msg = f.read(nbytes)
            if len(msg) != nbytes:
                raise ValueError("Truncated ERROR message")
            events.append(
                WireEvent(
                    rel_ns=rel_ns,
                    kind=kind,
                    message=msg.decode("utf-8", errors="replace"),
                )
            )
        elif kind in (EVENT_EMPTY, EVENT_OPEN, EVENT_CLOSE):
            events.append(WireEvent(rel_ns=rel_ns, kind=kind))
        else:
            raise ValueError(f"Unknown wire event type {kind}")
    ## END while True....

    return events


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
        ## END while True....

        header = _parse_header_lines(header_lines)
        if header.format_version == 1:
            events = _read_events_v1(f)
        else:
            events = _read_events_v2(f)
    return WireFile(header=header, events=events)


def _stream_by_name(streams: list, name: str) -> Optional[dict]:
    for s in streams:
        info = s.get("info") or {}
        names = info.get("name") or []
        if names and names[0] == name:
            return s
    ## END for s in streams....

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
    """Load an Epoc X LabRecorder XDF and build timed wire packets.

    Synthetic (re-encrypts decoded µV). Prefer live ``--record`` for exact HID bytes.
    """
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
        format_version=2,
    )
    key = _aes_key_from_header(header)

    timed: List[Tuple[float, str, int]] = []
    for i, t in enumerate(eeg_ts):
        timed.append((float(t), "eeg", i))
    ## END for i, t in enumerate(eeg_ts)....

    if m_ts is not None:
        for i, t in enumerate(m_ts):
            timed.append((float(t), "motion", i))
        ## END for i, t in enumerate(m_ts)....

    timed.sort(key=lambda e: e[0])

    packets: List[WirePacket] = []
    eeg_counter = 0
    motion_counter = 0
    for t, kind, idx in timed:
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
    ## END for t, kind, idx in timed....

    return WireFile(header=header, packets=packets)


class WireSessionRecorder:
    """Streaming writer for live HID capture (EPOCXWIRE/2)."""

    def __init__(self, path: Union[str, Path], header: WireHeader):
        self.path = Path(path)
        self.header = header
        self.header.format_version = 2
        self._f: Optional[BinaryIO] = None
        self._t0: Optional[float] = None
        self._event_count = 0
        self._flush_every = 32

    def open(self) -> None:
        if self._f is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._f = self.path.open("wb")
        _write_header(self._f, self.header, magic=MAGIC_V2)
        self._t0 = time.perf_counter()
        self.record_open()

    def _rel_ns(self) -> int:
        if self._t0 is None:
            self._t0 = time.perf_counter()
        return int(round((time.perf_counter() - self._t0) * 1e9))

    def _emit(self, event: WireEvent) -> None:
        if self._f is None:
            raise RuntimeError("WireSessionRecorder not open")
        _write_event(self._f, event)
        self._event_count += 1
        if self._event_count % self._flush_every == 0:
            self._f.flush()

    def record_open(self) -> None:
        self._emit(WireEvent(rel_ns=self._rel_ns(), kind=EVENT_OPEN))

    def record_close(self) -> None:
        self._emit(WireEvent(rel_ns=self._rel_ns(), kind=EVENT_CLOSE))

    def record_report(self, payload: bytes) -> None:
        self._emit(
            WireEvent(rel_ns=self._rel_ns(), kind=EVENT_REPORT, payload=bytes(payload))
        )

    def record_empty(self) -> None:
        self._emit(WireEvent(rel_ns=self._rel_ns(), kind=EVENT_EMPTY))

    def record_error(self, message: str) -> None:
        self._emit(WireEvent(rel_ns=self._rel_ns(), kind=EVENT_ERROR, message=str(message)))

    def close(self) -> None:
        if self._f is None:
            return
        try:
            self.record_close()
            self._f.flush()
        finally:
            self._f.close()
            self._f = None

    def __enter__(self) -> "WireSessionRecorder":
        self.open()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc is not None and self._f is not None:
            try:
                self.record_error(f"{exc_type.__name__ if exc_type else 'Error'}: {exc}")
            except Exception:
                pass
        self.close()


class WireReplaySource:
    """Timed event source replacing hid_device.read for `--replay`.

    EMPTY events return ``b""`` without setting EOF so disconnect-timeout logic runs.
    True EOF (past last event) returns ``b""`` with ``eof`` True.
    """

    def __init__(
        self,
        wire: WireFile,
        *,
        speed: float = 1.0,
        raise_on_error: bool = False,
    ):
        self.wire = wire
        self.speed = float(speed)
        self.raise_on_error = bool(raise_on_error)
        self._idx = 0
        self._t0_wall: Optional[float] = None
        self._eof = False
        # Ensure events list is populated from legacy packets.
        if not self.wire.events and self.wire.packets:
            self.wire.events = [
                WireEvent(rel_ns=p.rel_ns, kind=EVENT_REPORT, payload=p.payload)
                for p in self.wire.packets
            ]

    @classmethod
    def from_path(
        cls,
        path: Union[str, Path],
        *,
        speed: float = 1.0,
        raise_on_error: bool = False,
    ) -> "WireReplaySource":
        path = Path(path)
        if path.suffix.lower() == ".xdf":
            wire = xdf_to_wire(path)
        else:
            wire = read_wire_file(path)
        return cls(wire, speed=speed, raise_on_error=raise_on_error)

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
        headset.cipher = AES.new(headset.get_crypto_key(), AES.MODE_ECB)

    def _pace(self, rel_ns: int) -> None:
        now = time.perf_counter()
        if self._t0_wall is None:
            self._t0_wall = now
        if self.speed > 0:
            target = self._t0_wall + (rel_ns * 1e-9) / self.speed
            delay = target - time.perf_counter()
            if delay > 0:
                time.sleep(delay)

    def read(self, size: int = 32) -> bytes:
        """Return next REPORT payload, or empty bytes for EMPTY / EOF.

        Check ``eof`` to distinguish EMPTY (gap) from end-of-session.
        """
        del size  # HID max length; payloads are stored at recorded size
        events = self.wire.events
        while self._idx < len(events):
            ev = events[self._idx]
            self._idx += 1
            self._pace(ev.rel_ns)
            if ev.kind in (EVENT_OPEN, EVENT_CLOSE):
                continue
            if ev.kind == EVENT_EMPTY:
                return b""
            if ev.kind == EVENT_ERROR:
                if self.raise_on_error:
                    raise RuntimeError(ev.message or "wire ERROR event")
                continue
            if ev.kind == EVENT_REPORT:
                return ev.payload or b""
        ## END while self._idx < len(events)....

        self._eof = True
        return b""

    @property
    def eof(self) -> bool:
        return self._eof or self._idx >= len(self.wire.events)


@runtime_checkable
class WireFeeder(Protocol):
    """Common interface for live HID, in-process replay, and future WinUHid feeders."""

    @property
    def header(self) -> WireHeader: ...

    def open(self) -> None: ...

    def read(self, size: int = 32) -> bytes: ...

    def close(self) -> None: ...

    @property
    def eof(self) -> bool: ...


class ReplayFeeder:
    """WireFeeder adapter around WireReplaySource (in-process decrypt/decode path)."""

    def __init__(self, source: WireReplaySource):
        self._source = source
        self._opened = False

    @classmethod
    def from_path(
        cls, path: Union[str, Path], *, speed: float = 1.0
    ) -> "ReplayFeeder":
        return cls(WireReplaySource.from_path(path, speed=speed))

    @property
    def header(self) -> WireHeader:
        return self._source.header

    def open(self) -> None:
        self._opened = True

    def read(self, size: int = 32) -> bytes:
        if not self._opened:
            self.open()
        return self._source.read(size)

    def close(self) -> None:
        self._opened = False

    @property
    def eof(self) -> bool:
        return self._source.eof

    def apply_serial_to(self, headset) -> None:
        self._source.apply_serial_to(headset)


class LiveHidFeeder:
    """Thin WireFeeder around a hidapi device path (for symmetry / future hooks)."""

    def __init__(
        self,
        device_info: Dict[str, Any],
        *,
        read_timeout_ms: int = 500,
        header: Optional[WireHeader] = None,
    ):
        self.device_info = device_info
        self.read_timeout_ms = read_timeout_ms
        self._header = header or header_from_hid_device(
            device_info, read_timeout_ms=read_timeout_ms
        )
        self._hid = None
        self._eof = False
        self._use_timeout_kw = True

    @property
    def header(self) -> WireHeader:
        return self._header

    def open(self) -> None:
        import hid

        path = self.device_info["path"]
        if hasattr(hid, "Device"):
            self._hid = hid.Device(path=path)
        else:
            self._hid = hid.device()
            self._hid.open_path(path)

    def read(self, size: int = 32) -> bytes:
        if self._hid is None:
            raise RuntimeError("LiveHidFeeder not open")
        try:
            if self._use_timeout_kw:
                data = self._hid.read(size, timeout_ms=self.read_timeout_ms)
            else:
                data = self._hid.read(size, self.read_timeout_ms)
        except TypeError:
            self._use_timeout_kw = False
            data = self._hid.read(size, self.read_timeout_ms)
        if isinstance(data, list):
            data = bytes(data)
        return data or b""

    def close(self) -> None:
        if self._hid is not None:
            try:
                self._hid.close()
            except Exception:
                pass
            self._hid = None
        self._eof = True

    @property
    def eof(self) -> bool:
        return self._eof


class WinUHidFeeder:
    """Future OS-level virtual HID feeder (WinUHid / VHF).

    Not implemented in this pass. A later feeder will:
    1. Create a virtual device with captured ``vid``/``pid`` and ``report_descriptor``.
    2. Pace ``SubmitInputReport`` from REPORT events in the ``.epocxwire`` file.
    3. Note: WinUHid may not expose the USB serial string; AES may still need
       header ``serial=`` injection, or a fuller USB emulator (UDECX/Vdevice).

    See README "WinUHid feeder (later)" and ``scripts/feed_winuhid.py``.
    """

    def __init__(self, path: Union[str, Path], *, speed: float = 1.0):
        self.path = Path(path)
        self.speed = speed
        self._wire: Optional[WireFile] = None

    @property
    def header(self) -> WireHeader:
        if self._wire is None:
            self._wire = read_wire_file(self.path)
        return self._wire.header

    def open(self) -> None:
        raise NotImplementedError(
            "WinUHidFeeder requires the WinUHid driver/SDK. "
            "Use ReplayFeeder / main.py --replay for in-process playback, "
            "or scripts/feed_winuhid.py --dry-run to pace events without a driver."
        )

    def read(self, size: int = 32) -> bytes:
        raise NotImplementedError("WinUHidFeeder is a submit-side feeder, not a reader")

    def close(self) -> None:
        pass

    @property
    def eof(self) -> bool:
        return True
