"""ctypes bindings for WinUHid.dll (virtual HID via VHF).

Requires the WinUHid driver/SDK from https://github.com/lurebat/WinUHid.
Set ``WINUHID_DLL`` to an absolute path if the DLL is not on ``PATH``.

Importing this module is safe on non-Windows; DLL load happens on first use.
"""
from __future__ import annotations

import ctypes
import os
import sys
from ctypes import (
    POINTER,
    WINFUNCTYPE,
    c_ubyte,
    c_uint,
    c_ulong,
    c_ushort,
    c_void_p,
    c_wchar_p,
)
from typing import Any, Optional, Union

# Emotiv Epoc X EEG interface (usage_page=0xFFFF, usage=2):
# 32-byte input report + 6-byte feature report, no report IDs.
DEFAULT_EPOCX_REPORT_DESCRIPTOR = bytes.fromhex(
    "06ffff0902a1000903750895201581257f8102090475089506b102c0"
)

DEFAULT_VID = 0x1234
DEFAULT_PID = 0xED02

WINUHID_EVENT_NONE = 0x0
WINUHID_EVENT_GET_FEATURE = 0x1
WINUHID_EVENT_SET_FEATURE = 0x2
WINUHID_EVENT_WRITE_REPORT = 0x4
WINUHID_EVENT_READ_REPORT = 0x8

_lib: Optional[ctypes.CDLL] = None


class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", c_ulong),
        ("Data2", c_ushort),
        ("Data3", c_ushort),
        ("Data4", c_ubyte * 8),
    ]


class WINUHID_DEVICE_CONFIG(ctypes.Structure):
    """Packed layout matching WinUHid.h ``WINUHID_DEVICE_CONFIG`` (pshpack1)."""

    _pack_ = 1
    _fields_ = [
        ("SupportedEvents", c_uint),  # WINUHID_EVENT_TYPE
        ("VendorID", c_ushort),
        ("ProductID", c_ushort),
        ("VersionNumber", c_ushort),
        ("ReportDescriptorLength", c_ushort),
        ("ReportDescriptor", c_void_p),
        ("ContainerId", GUID),
        ("InstanceID", c_wchar_p),
        ("HardwareIDs", c_wchar_p),
        ("ReadReportPeriodUs", c_uint),
    ]


PWINUHID_DEVICE = c_void_p
WINUHID_EVENT_CALLBACK = WINFUNCTYPE(None, c_void_p, PWINUHID_DEVICE, c_void_p)


def _last_error() -> int:
    if sys.platform != "win32":
        return 0
    return int(ctypes.GetLastError())


def _err_msg(prefix: str) -> str:
    err = _last_error()
    return f"{prefix} (GetLastError={err})" if err else prefix


def load_library(dll_path: Optional[Union[str, os.PathLike]] = None) -> ctypes.CDLL:
    """Load WinUHid.dll once and bind function prototypes."""
    global _lib
    if _lib is not None and dll_path is None:
        return _lib

    if sys.platform != "win32":
        raise OSError("WinUHid is only available on Windows")

    path: Optional[str] = None
    if dll_path is not None:
        path = str(dll_path)
    else:
        path = os.environ.get("WINUHID_DLL") or "WinUHid.dll"

    try:
        lib = ctypes.WinDLL(path)
    except OSError as exc:
        raise OSError(
            f"Failed to load WinUHid.dll from {path!r}. "
            "Install the WinUHid driver/SDK or set WINUHID_DLL to the DLL path."
        ) from exc

    lib.WinUHidGetDriverInterfaceVersion.argtypes = []
    lib.WinUHidGetDriverInterfaceVersion.restype = c_ulong

    lib.WinUHidCreateDevice.argtypes = [POINTER(WINUHID_DEVICE_CONFIG)]
    lib.WinUHidCreateDevice.restype = PWINUHID_DEVICE

    lib.WinUHidStartDevice.argtypes = [PWINUHID_DEVICE, c_void_p, c_void_p]
    lib.WinUHidStartDevice.restype = ctypes.c_bool

    lib.WinUHidSubmitInputReport.argtypes = [PWINUHID_DEVICE, c_void_p, c_ulong]
    lib.WinUHidSubmitInputReport.restype = ctypes.c_bool

    lib.WinUHidStopDevice.argtypes = [PWINUHID_DEVICE]
    lib.WinUHidStopDevice.restype = None

    lib.WinUHidDestroyDevice.argtypes = [PWINUHID_DEVICE]
    lib.WinUHidDestroyDevice.restype = None

    if dll_path is None:
        _lib = lib
    return lib


def get_driver_interface_version(
    dll_path: Optional[Union[str, os.PathLike]] = None,
) -> int:
    lib = load_library(dll_path)
    ver = int(lib.WinUHidGetDriverInterfaceVersion())
    if ver == 0:
        raise OSError(_err_msg("WinUHidGetDriverInterfaceVersion failed"))
    return ver


def create_device(
    *,
    vendor_id: int,
    product_id: int,
    report_descriptor: bytes,
    version_number: int = 0,
    supported_events: int = WINUHID_EVENT_NONE,
    instance_id: Optional[str] = None,
    read_report_period_us: int = 0,
    dll_path: Optional[Union[str, os.PathLike]] = None,
) -> Any:
    """Create a virtual HID device. Caller must eventually ``destroy_device``."""
    if not report_descriptor:
        raise ValueError("report_descriptor must be non-empty")
    if len(report_descriptor) > 0xFFFF:
        raise ValueError("report_descriptor too large for USHORT length")

    lib = load_library(dll_path)
    # Keep the buffer alive for the duration of CreateDevice (and beyond —
    # the driver copies it, but holding a reference is cheap insurance).
    desc_buf = (c_ubyte * len(report_descriptor)).from_buffer_copy(report_descriptor)

    config = WINUHID_DEVICE_CONFIG()
    config.SupportedEvents = supported_events
    config.VendorID = vendor_id & 0xFFFF
    config.ProductID = product_id & 0xFFFF
    config.VersionNumber = version_number & 0xFFFF
    config.ReportDescriptorLength = len(report_descriptor)
    config.ReportDescriptor = ctypes.cast(desc_buf, c_void_p)
    config.ContainerId = GUID()
    config.InstanceID = instance_id
    config.HardwareIDs = None
    config.ReadReportPeriodUs = read_report_period_us

    # Driver copies the descriptor during CreateDevice; keep desc_buf alive
    # until after the call returns.
    device = lib.WinUHidCreateDevice(ctypes.byref(config))
    if not device:
        raise OSError(_err_msg("WinUHidCreateDevice failed"))
    del desc_buf  # explicit: no longer needed after successful create
    return device


def start_device(
    device: Any,
    *,
    dll_path: Optional[Union[str, os.PathLike]] = None,
) -> None:
    lib = load_library(dll_path)
    ok = lib.WinUHidStartDevice(device, None, None)
    if not ok:
        raise OSError(_err_msg("WinUHidStartDevice failed"))


def submit_input_report(
    device: Any,
    report: bytes,
    *,
    dll_path: Optional[Union[str, os.PathLike]] = None,
) -> None:
    if not report:
        raise ValueError("report must be non-empty")
    lib = load_library(dll_path)
    buf = (c_ubyte * len(report)).from_buffer_copy(report)
    ok = lib.WinUHidSubmitInputReport(device, ctypes.cast(buf, c_void_p), len(report))
    if not ok:
        raise OSError(_err_msg("WinUHidSubmitInputReport failed"))


def stop_device(
    device: Any,
    *,
    dll_path: Optional[Union[str, os.PathLike]] = None,
) -> None:
    lib = load_library(dll_path)
    lib.WinUHidStopDevice(device)


def destroy_device(
    device: Any,
    *,
    dll_path: Optional[Union[str, os.PathLike]] = None,
) -> None:
    if device is None:
        return
    lib = load_library(dll_path)
    lib.WinUHidDestroyDevice(device)


class WinUHidBackend:
    """Thin callable façade used by ``WinUHidFeeder`` (and mocks in tests)."""

    def __init__(self, dll_path: Optional[Union[str, os.PathLike]] = None):
        self.dll_path = dll_path

    def create(
        self,
        *,
        vendor_id: int,
        product_id: int,
        report_descriptor: bytes,
        version_number: int = 0,
    ) -> Any:
        return create_device(
            vendor_id=vendor_id,
            product_id=product_id,
            report_descriptor=report_descriptor,
            version_number=version_number,
            dll_path=self.dll_path,
        )

    def start(self, device: Any) -> None:
        start_device(device, dll_path=self.dll_path)

    def submit(self, device: Any, report: bytes) -> None:
        submit_input_report(device, report, dll_path=self.dll_path)

    def destroy(self, device: Any) -> None:
        destroy_device(device, dll_path=self.dll_path)
