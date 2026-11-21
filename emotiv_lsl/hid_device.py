"""Cross-platform Emotiv USB HID discovery helpers.

Emotiv USB receivers use vendor ID 0x1234. On Windows, CyKit historically
selected the EEG interface via usage==2 (or usage==0 + interface_number==1).
macOS IOHIDManager often reports interface_number=-1 and may collapse usage
pages, so we prefer VID/manufacturer matching and only use usage as a soft
preference.
"""

from __future__ import annotations

import sys
from typing import Any, Dict, List, Optional


# Brain Actuated Technologies / Emotiv USB dongles (EPOC / EPOC+ / EPOC X).
EMOTIV_VENDOR_IDS = (0x1234,)


def _mfg(device: Dict[str, Any]) -> str:
    return (device.get("manufacturer_string") or "").strip()


def _is_emotiv_identity(device: Dict[str, Any]) -> bool:
    if device.get("vendor_id") in EMOTIV_VENDOR_IDS:
        return True
    mfg = _mfg(device).lower()
    return mfg == "emotiv" or mfg.startswith("emotiv")


def _is_preferred_eeg_interface(device: Dict[str, Any]) -> bool:
    usage = device.get("usage", 0) or 0
    iface = device.get("interface_number", -1)
    if usage == 2:
        return True
    if usage == 0 and iface == 1:
        return True
    return False


def list_emotiv_hid_devices() -> List[Dict[str, Any]]:
    """Return all HID entries that look like Emotiv USB receivers."""
    import hid

    return [d for d in hid.enumerate() if _is_emotiv_identity(d)]


def find_emotiv_hid_device(
    *,
    require_preferred_interface: bool = False,
) -> Dict[str, Any]:
    """Pick the HID device dict used for EEG streaming.

    Preference order:
      1. Emotiv identity + preferred EEG usage/interface
      2. On macOS (or when require_preferred_interface is False): any Emotiv
         identity entry that has a serial number
      3. Any Emotiv identity entry
    """
    candidates = list_emotiv_hid_devices()
    if not candidates:
        raise Exception(
            "Emotiv USB receiver not found "
            "(no HID device with vendor 0x1234 / manufacturer Emotiv)"
        )

    preferred = [d for d in candidates if _is_preferred_eeg_interface(d)]
    if preferred:
        return preferred[0]

    if require_preferred_interface and sys.platform != "darwin":
        raise Exception(
            "Emotiv USB receiver found but EEG interface "
            "(usage==2 or interface_number==1) was not present"
        )

    with_serial = [d for d in candidates if d.get("serial_number")]
    if with_serial:
        return with_serial[0]
    return candidates[0]


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


def format_hid_device(device: Dict[str, Any]) -> str:
    return (
        f"VID={device.get('vendor_id', 0):04X} "
        f"PID={device.get('product_id', 0):04X} "
        f"usage={device.get('usage')} "
        f"iface={device.get('interface_number')} "
        f"mfr={device.get('manufacturer_string')!r} "
        f"prod={device.get('product_string')!r} "
        f"serial={device.get('serial_number')!r}"
    )
