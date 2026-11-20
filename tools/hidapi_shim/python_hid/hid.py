"""Pure-Python ``hid`` module shim for Emotiv EPOCXWIRE replay.

Drop this file into a PyInstaller ``_internal`` / ``_MEI*`` folder (after
renaming/removing the real ``hid*.pyd``) so a frozen ``emotiv_lsl*.exe``
enumerates mfg=Emotiv / usage=2 / wire serial and reads paced REPORT bytes.

Env: EPOCX_WIRE / EPOCX_WIRE_PATH, EPOCX_SPEED (default 1.0; 0 = ASAP).
"""
from __future__ import annotations

import os
import struct
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

SHIM_PATH = r"\\shim\emotiv_epocx"
SHIM_VID = 0x1234
SHIM_PID = 0xED02
EVENT_REPORT = 1
EVENT_EMPTY = 2
EVENT_ERROR = 3
EVENT_OPEN = 4
EVENT_CLOSE = 5

version_str = "0.15.0-emotiv-shim"


def _wire_path() -> Optional[Path]:
    for key in ("EPOCX_WIRE", "EPOCX_WIRE_PATH"):
        v = os.environ.get(key)
        if v:
            return Path(v)
    return None


def _speed() -> float:
    try:
        return float(os.environ.get("EPOCX_SPEED", "1") or "1")
    except ValueError:
        return 1.0


def _parse_wire(path: Path):
    data = path.read_bytes()
    nl = data.find(b"\n")
    if nl < 0:
        raise ValueError("bad wire file")
    magic = data[:nl].decode("utf-8", errors="replace").strip()
    i = nl + 1
    serial = "UD20221202006756"
    while i < len(data):
        eol = data.find(b"\n", i)
        if eol < 0:
            break
        line = data[i:eol].decode("utf-8", errors="replace").strip("\r")
        i = eol + 1
        if line == "":
            break
        if line.startswith("serial="):
            serial = line.split("=", 1)[1]
    events: List[dict] = []
    if magic == "EPOCXWIRE/1":
        while i + 10 <= len(data):
            rel_ns, nbytes = struct.unpack_from("<QH", data, i)
            i += 10
            payload = data[i : i + nbytes]
            i += nbytes
            events.append({"rel_ns": rel_ns, "kind": EVENT_REPORT, "payload": payload})
    elif magic == "EPOCXWIRE/2":
        while i + 9 <= len(data):
            rel_ns, kind = struct.unpack_from("<QB", data, i)
            i += 9
            payload = b""
            if kind == EVENT_REPORT:
                (nbytes,) = struct.unpack_from("<H", data, i)
                i += 2
                payload = data[i : i + nbytes]
                i += nbytes
            elif kind == EVENT_ERROR:
                (msg_len,) = struct.unpack_from("<H", data, i)
                i += 2 + msg_len
            events.append({"rel_ns": rel_ns, "kind": kind, "payload": payload})
    else:
        raise ValueError(f"bad magic {magic!r}")
    return serial, events


_SERIAL = "UD20221202006756"
_EVENTS: List[dict] = []
_LOADED = False


def _ensure_loaded() -> None:
    global _SERIAL, _EVENTS, _LOADED
    if _LOADED:
        return
    path = _wire_path()
    if path and path.is_file():
        _SERIAL, _EVENTS = _parse_wire(path)
    _LOADED = True


def enumerate(vid: int = 0, pid: int = 0) -> List[Dict[str, Any]]:  # noqa: A001
    _ensure_loaded()
    if vid and vid != SHIM_VID:
        return []
    if pid and pid != SHIM_PID:
        return []
    return [
        {
            "path": SHIM_PATH.encode("utf-8"),
            "vendor_id": SHIM_VID,
            "product_id": SHIM_PID,
            "serial_number": _SERIAL,
            "release_number": 0x0100,
            "manufacturer_string": "Emotiv",
            "product_string": "EPOC X",
            "usage_page": 0xFFA0,
            "usage": 2,
            "interface_number": 1,
            "bus_type": 1,
        }
    ]


class device:
    """cython-hidapi compatible device object."""

    def __init__(self, vid=None, pid=None, serial=None, path=None):
        self._idx = 0
        self._eof = False
        self._t0: Optional[float] = None
        self._nonblocking = False
        _ensure_loaded()
        if path is not None or vid is not None or pid is not None or serial is not None:
            self.open(vid, pid, serial, path)

    def open(self, vid=None, pid=None, serial=None, path=None):
        del vid, pid, serial, path
        self._idx = 0
        self._eof = False
        self._t0 = None
        return None

    def open_path(self, path):
        return self.open(path=path)

    def close(self):
        self._eof = True

    def set_nonblocking(self, nonblock: int):
        self._nonblocking = bool(nonblock)

    def _pace(self, rel_ns: int) -> None:
        speed = _speed()
        if speed <= 0:
            return
        now = time.perf_counter()
        if self._t0 is None:
            self._t0 = now
        target = self._t0 + (rel_ns * 1e-9) / speed
        delay = target - time.perf_counter()
        if delay > 0:
            time.sleep(delay)

    def read(self, size: int, timeout_ms: int = -1):
        del timeout_ms
        _ensure_loaded()
        while self._idx < len(_EVENTS):
            ev = _EVENTS[self._idx]
            self._idx += 1
            self._pace(int(ev["rel_ns"]))
            kind = int(ev["kind"])
            if kind in (EVENT_OPEN, EVENT_CLOSE, EVENT_ERROR):
                continue
            if kind == EVENT_EMPTY:
                return b""
            if kind == EVENT_REPORT:
                payload = ev.get("payload") or b""
                return payload[:size]
        self._eof = True
        if self._nonblocking:
            return b""
        time.sleep(0.001)
        return b""

    def write(self, data: bytes) -> int:
        return len(data)

    def get_manufacturer_string(self) -> str:
        return "Emotiv"

    def get_product_string(self) -> str:
        return "EPOC X"

    def get_serial_number_string(self) -> str:
        _ensure_loaded()
        return _SERIAL


Device = device
