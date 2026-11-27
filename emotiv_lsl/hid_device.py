"""Cross-platform Emotiv 2.4 GHz USB HID discovery and Feature/Output Report 0x00 helpers.

Emotiv 2.4 GHz USB HID receivers expose (0x1234, 0xED02) or (0x21A1, 0x0001).
Composite dongles expose two HID interfaces:
  - MI_00: UsagePage=0xF0FF, Usage=0x0010 (4-byte dongle status reports)
  - MI_01: UsagePage=0xFFFF, Usage=0x0002 (32-byte encrypted EEG/Motion + 18-byte Feature Report 0x00)
macOS IOHIDManager often reports interface_number=-1 and may collapse usage pages,
so we enforce (usage_page & 0xFF00) == 0xFF00 when usage_page is present and reject MI_00.
"""

from __future__ import annotations

import struct
import sys
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple


# Brain Actuated Technologies / Emotiv Inc. 2.4 GHz USB HID receivers (EPOC / EPOC+ / EPOC X).
EMOTIV_VENDOR_IDS: Tuple[int, ...] = (0x1234, 0x21A1)
EMOTIV_RECEIVER_VID_PIDS: Tuple[Tuple[int, int], ...] = (
    (0x1234, 0xED02),
    (0x21A1, 0x0001),
)
EMOTIV_EXTENDER_PID = 0xED03


@dataclass
class UsbHeadsetConfig:
    """Decoded 18..32-byte USB HID Feature Report 0x00 from MI_01."""

    headset_type: int          # 7 = EPOC X ((headset_fw >> 8) == 0x07)
    dongle_fw: int             # BE uint16 at buf[3:5]
    headset_fw: int            # BE uint16 at buf[5:7] (0x0000 when unpaired)
    headset_serial: str        # 8 uppercase hex chars from buf[7:11] (e.g. "E50202E9")
    eeg_rate: int              # LE uint16 at buf[13:15] (128 or 256 Hz)
    eeg_bits: int              # uint8 at buf[15] (14 or 16)
    mems_rate: int             # uint8 at buf[16] (0, 32, 64, or 128 Hz)
    mems_bits: int             # uint8 at buf[17] (0, 14, or 16)
    cfg_echo: int = 0          # uint8 at buf[2]
    mode_active: bool = True   # buf[12] != 0


def _mfg(device: Dict[str, Any]) -> str:
    return (device.get("manufacturer_string") or "").strip()


def _is_emotiv_identity(device: Dict[str, Any]) -> bool:
    vid = device.get("vendor_id", 0) or 0
    pid = device.get("product_id", 0) or 0
    if vid == 0x1234 and pid == EMOTIV_EXTENDER_PID:
        return False
    if (vid, pid) in EMOTIV_RECEIVER_VID_PIDS or vid in EMOTIV_VENDOR_IDS:
        return True
    mfg = _mfg(device).lower()
    return mfg == "emotiv" or mfg.startswith("emotiv")


def _is_status_only_interface(device: Dict[str, Any]) -> bool:
    """Identify composite MI_00 (UsagePage=0xF0FF, Usage=0x0010, 4-byte status pipe)."""
    usage_page = device.get("usage_page", 0) or 0
    usage = device.get("usage", 0) or 0
    if usage_page and (usage_page & 0xFF00) != 0xFF00:
        return True
    if usage == 0x0010:
        return True
    prod = (device.get("product_string") or "").strip().lower()
    if "receiver/dongle" in prod and usage != 2:
        return True
    return False


def _is_preferred_eeg_interface(device: Dict[str, Any]) -> bool:
    if _is_status_only_interface(device):
        return False
    usage_page = device.get("usage_page", 0) or 0
    usage = device.get("usage", 0) or 0
    iface = device.get("interface_number", -1)
    if usage_page and (usage_page & 0xFF00) == 0xFF00:
        if usage in (2, 0) and (usage == 2 or iface in (1, -1)):
            return True
    if usage == 2:
        return True
    if usage == 0 and iface == 1:
        return True
    return False


def list_emotiv_hid_devices() -> List[Dict[str, Any]]:
    """Return all HID entries that look like Emotiv USB receivers."""
    import hid

    return [d for d in hid.enumerate() if _is_emotiv_identity(d)]


def find_emotiv_hid_device(*, require_preferred_interface: bool = False) -> Dict[str, Any]:
    """Pick the HID device dict used for EEG/Motion streaming (MI_01).

    Preference order:
      1. Emotiv identity + preferred EEG usage/interface (UsagePage & 0xFF00 == 0xFF00, Usage==2 / MI_01)
      2. On macOS (or when require_preferred_interface is False): non-MI_00 entry with a serial number
      3. Any non-MI_00 Emotiv identity entry (or last-resort candidate on macOS)
    """
    candidates = list_emotiv_hid_devices()
    if not candidates:
        raise Exception(
            "Emotiv USB receiver not found "
            "(no HID device with vendor 0x1234/0x21A1 or manufacturer Emotiv)"
        )

    preferred = [d for d in candidates if _is_preferred_eeg_interface(d)]
    if preferred:
        with_serial_pref = [d for d in preferred if d.get("serial_number")]
        return with_serial_pref[0] if with_serial_pref else preferred[0]

    if require_preferred_interface and sys.platform != "darwin":
        raise Exception(
            "Emotiv USB receiver found but EEG interface "
            "((usage_page & 0xFF00) == 0xFF00 and usage==2 / interface_number==1) was not present"
        )

    non_status = [d for d in candidates if not _is_status_only_interface(d)]
    pool = non_status if non_status else candidates
    with_serial = [d for d in pool if d.get("serial_number")]
    if with_serial:
        return with_serial[0]
    return pool[0]


def open_emotiv_hid_device(device: Optional[Dict[str, Any]] = None):
    """Open an Emotiv HID device, supporting both ctypes and Cython hid bindings."""
    import hid

    if device is None:
        device = find_emotiv_hid_device()

    path = device["path"]
    # ctypes pyhidapi: hid.Device(path=...) opens in the constructor.
    # Cython hidapi: hid.device() then open_path(path).
    if hasattr(hid, "Device"):
        return hid.Device(path=path)

    hid_device = hid.device()
    hid_device.open_path(path)
    return hid_device


def parse_usb_feature_report_00(buf: bytes) -> UsbHeadsetConfig:
    """Decode 18..32-byte HID Feature Report 0x00 (CortexService parseFeatureReport)."""
    raw = bytes(buf)
    if len(raw) < 18 or raw[0] != 0x00:
        raise ValueError(f"Expected >=18B Feature Report 0x00, got len={len(raw)}")
    cfg_echo = raw[2]
    dongle_fw = (raw[3] << 8) | raw[4]
    headset_fw = (raw[5] << 8) | raw[6]
    headset_serial = raw[7:11].hex().upper()
    mode_active = raw[12] != 0
    if mode_active:
        eeg_rate = struct.unpack_from("<H", raw, 13)[0]
        eeg_bits = raw[15]
        mems_rate = raw[16]
        mems_bits = raw[17]
    else:
        eeg_rate, eeg_bits, mems_rate, mems_bits = 128, 16, 32, 16
    return UsbHeadsetConfig(
        headset_type=7 if (headset_fw >> 8) == 0x07 else 0,
        dongle_fw=dongle_fw,
        headset_fw=headset_fw,
        headset_serial=headset_serial,
        eeg_rate=eeg_rate,
        eeg_bits=eeg_bits,
        mems_rate=mems_rate,
        mems_bits=mems_bits,
        cfg_echo=cfg_echo,
        mode_active=mode_active,
    )


def read_usb_feature_report_00(hid_device: Any) -> Optional[UsbHeadsetConfig]:
    """Read and parse Feature Report 0x00 from an open HID handle, or None if unavailable."""
    if hid_device is None:
        return None
    getter = getattr(hid_device, "get_feature_report", None)
    if not callable(getter):
        return None
    for req_len in (32, 18):
        try:
            res = getter(0x00, req_len)
        except Exception:
            continue
        if not res:
            continue
        raw = bytes(res) if not isinstance(res, (bytes, bytearray)) else bytes(res)
        if len(raw) == 17 and raw[0] != 0x00:
            raw = b"\x00" + raw
        if len(raw) >= 18 and raw[0] == 0x00:
            return parse_usb_feature_report_00(raw)
    ## END for req_len in (32, 18)...

    return None


def build_epocx_usb_mode_report(eeg_rate: int = 256, eeg_bits: int = 16, mems_rate: int = 64, mems_bits: int = 16) -> bytes:
    """Encode 33-byte HID Output Report 0x00 (`00 66 BB 20 12 <CFG> ...`) for EPOC X."""
    if eeg_rate not in (128, 256):
        raise ValueError(f"Invalid eeg_rate={eeg_rate}; expected 128 or 256")
    if eeg_bits not in (14, 16):
        raise ValueError(f"Invalid eeg_bits={eeg_bits}; expected 14 or 16")
    mems_map = {0: 0x00, 32: 0x04, 64: 0x08, 128: 0x0C}
    if mems_rate not in mems_map:
        raise ValueError(f"Invalid mems_rate={mems_rate}; expected 0, 32, 64, or 128")
    if mems_rate > 0 and mems_bits not in (14, 16):
        raise ValueError(f"Invalid mems_bits={mems_bits}; expected 14 or 16")
    cfg = 0x80
    if eeg_rate == 256:
        cfg |= 0x40
    if eeg_bits == 16:
        cfg |= 0x20
    cfg |= mems_map[mems_rate]
    if mems_rate > 0 and mems_bits == 16:
        cfg |= 0x02
    out = bytearray(33)
    out[0] = 0x00
    out[1] = 0x66  # EPOC X magic byte 0 (EPOC+ uses 0x55)
    out[2] = 0xBB  # EPOC X magic byte 1 (EPOC+ uses 0xAA)
    out[3] = 0x20
    out[4] = 0x12
    out[5] = cfg
    return bytes(out)


def is_usb_mode_ack(hid_in: bytes) -> bool:
    """Check 32-byte (or 33-byte Report-ID-prefixed) HID input report for `0x88, 0x12` ACK."""
    if hid_in is None or len(hid_in) < 4:
        return False
    raw = bytes(hid_in)
    return (raw[2] == 0x88 and raw[3] == 0x12) or (len(raw) >= 5 and raw[3] == 0x88 and raw[4] == 0x12)


def _hid_write_report(hid_device: Any, report: bytes) -> None:
    writer = getattr(hid_device, "write", None)
    if not callable(writer):
        raise RuntimeError("HID device handle does not support write()")
    try:
        writer(bytes(report))
    except TypeError:
        writer(list(report))


def _hid_read_timeout(hid_device: Any, max_len: int, timeout_ms: int) -> bytes:
    try:
        data = hid_device.read(max_len, timeout_ms=timeout_ms)
    except TypeError:
        data = hid_device.read(max_len, timeout_ms)
    if not data:
        return b""
    return bytes(data) if not isinstance(data, (bytes, bytearray)) else bytes(data)


def configure_epocx_usb_mode(hid_device: Any, *, eeg_rate: int = 256, eeg_bits: int = 16, mems_rate: int = 64, mems_bits: int = 16, poll_attempts: int = 10, poll_timeout_ms: int = 500, max_retries: int = 5) -> Optional[UsbHeadsetConfig]:
    """Send 33B Output Report 0x00 (`00 66 BB 20 12 <CFG>`), poll `0x88 0x12` ACK, and re-read Feature Report 0x00."""
    report = build_epocx_usb_mode_report(
        eeg_rate=eeg_rate,
        eeg_bits=eeg_bits,
        mems_rate=mems_rate,
        mems_bits=mems_bits,
    )
    for _attempt in range(max(1, int(max_retries))):
        _hid_write_report(hid_device, report)
        for _poll in range(max(1, int(poll_attempts))):
            pkt = _hid_read_timeout(hid_device, 32, poll_timeout_ms)
            if pkt and is_usb_mode_ack(pkt):
                return read_usb_feature_report_00(hid_device)
            if not pkt and poll_timeout_ms <= 0:
                time.sleep(0.05)
        ## END for _poll in range(max(1, int(poll_attempts)))...
    ## END for _attempt in range(max(1, int(max_retries)))...

    return None


def format_hid_device(device: Dict[str, Any]) -> str:
    usage_page = device.get("usage_page")
    up_str = f"0x{usage_page:04X}" if isinstance(usage_page, int) else repr(usage_page)
    return (
        f"VID={device.get('vendor_id', 0):04X} "
        f"PID={device.get('product_id', 0):04X} "
        f"usage_page={up_str} "
        f"usage={device.get('usage')} "
        f"iface={device.get('interface_number')} "
        f"mfr={device.get('manufacturer_string')!r} "
        f"prod={device.get('product_string')!r} "
        f"serial={device.get('serial_number')!r}"
    )
