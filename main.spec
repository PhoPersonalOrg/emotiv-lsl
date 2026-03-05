# -*- mode: python ; coding: utf-8 -*-
# Cross-platform PyInstaller spec for emotiv-lsl.
# Handles liblsl and hidapi bundling on Windows, macOS, and Linux.

import os
import sys
import subprocess

from PyInstaller.utils.hooks import collect_dynamic_libs

# ---------------------------------------------------------------------------
# liblsl – bundled with modern pylsl wheels (>=1.14).
# collect_dynamic_libs will pick up the .so/.dylib/.dll from inside the
# pylsl package directory.  On Linux we fall back to PYLSL_LIB or common
# system paths only when the wheel carries no bundled copy.
# ---------------------------------------------------------------------------
pylsl_bins = collect_dynamic_libs("pylsl")

extra_lsl_bins = []
if sys.platform not in ("win32", "darwin") and not pylsl_bins:
    _env_lib = os.environ.get("PYLSL_LIB")
    if _env_lib and os.path.exists(_env_lib):
        extra_lsl_bins.append((_env_lib, "pylsl/lib"))
    else:
        for _p in [
            "/usr/local/lib/liblsl.so",
            "/usr/lib/liblsl.so",
            "/usr/lib/x86_64-linux-gnu/liblsl.so",
            "/usr/lib/aarch64-linux-gnu/liblsl.so",
            "/usr/lib64/liblsl.so",
        ]:
            if os.path.exists(_p):
                extra_lsl_bins.append((_p, "pylsl/lib"))
                break

# ---------------------------------------------------------------------------
# hidapi – the `hid` ctypes package loads this native library at runtime.
# Bundle the right library for each platform so the frozen binary is
# self-contained.
# ---------------------------------------------------------------------------
hidapi_bins = []

if sys.platform == "win32":
    # DLL committed to the repo at hidapi-win/x64/hidapi.dll.
    # main.py registers the directory via os.add_dll_directory() at startup.
    _win_dll = os.path.join(SPECPATH, "hidapi-win", "x64", "hidapi.dll")
    if os.path.exists(_win_dll):
        hidapi_bins.append((_win_dll, "."))
    else:
        print("WARNING: hidapi-win/x64/hidapi.dll not found; "
              "the hid module will not work in the frozen binary.")

elif sys.platform == "darwin":
    # Homebrew installs hidapi to different prefixes on Intel vs Apple Silicon.
    _mac_search_dirs = [
        "/opt/homebrew/opt/hidapi/lib",   # Apple Silicon (Homebrew default)
        "/opt/homebrew/lib",
        "/usr/local/opt/hidapi/lib",       # Intel Mac (Homebrew default)
        "/usr/local/lib",
    ]
    _mac_lib_names = [
        "libhidapi.dylib",
        "libhidapi-iohidmanager.dylib",
        "libhidapi-libusb.dylib",
        "libhidapi.0.dylib",
    ]
    for _d in _mac_search_dirs:
        for _n in _mac_lib_names:
            _src = os.path.join(_d, _n)
            if os.path.exists(_src):
                hidapi_bins.append((_src, "Contents/Frameworks"))
    if not hidapi_bins:
        print("WARNING: hidapi dylibs not found. "
              "Install with: brew install hidapi")

else:
    # Linux: use ldconfig to locate the installed hidapi shared library,
    # then fall back to well-known paths if ldconfig is unavailable.
    _linux_found = False
    _ldconfig_available = False
    try:
        _ld_result = subprocess.run(
            ["ldconfig", "-p"], capture_output=True, text=True, check=False
        )
        _ldconfig_available = True
        for _line in _ld_result.stdout.splitlines():
            if "libhidapi" in _line and "=>" in _line:
                _lib_path = _line.split("=>")[1].strip()
                if os.path.exists(_lib_path):
                    hidapi_bins.append((_lib_path, "."))
                    _linux_found = True
                    break
    except FileNotFoundError:
        print("WARNING: ldconfig not found; falling back to common library paths.")
    if not _linux_found:
        for _cand in [
            "/usr/lib/x86_64-linux-gnu/libhidapi-hidraw.so.0",
            "/usr/lib/x86_64-linux-gnu/libhidapi-libusb.so.0",
            "/usr/lib/aarch64-linux-gnu/libhidapi-hidraw.so.0",
            "/usr/lib/libhidapi-hidraw.so.0",
            "/usr/lib/libhidapi-libusb.so.0",
            "/usr/local/lib/libhidapi.so",
        ]:
            if os.path.exists(_cand):
                hidapi_bins.append((_cand, "."))
                break
        else:
            _reason = "ldconfig did not list it" if _ldconfig_available else "ldconfig unavailable"
            print(f"WARNING: hidapi not found on Linux ({_reason}). "
                  "Install with: sudo apt-get install libhidapi-dev")

binaries = pylsl_bins + extra_lsl_bins + hidapi_bins

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=binaries,
    datas=[],
    hiddenimports=['pylsl', 'hid'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='emotiv_lsl',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['images/icons/emotiv_lsl_icon_design.ico'],
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='emotiv_lsl',
)
