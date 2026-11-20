#!/usr/bin/env python3
"""Launch an emotiv_lsl*.exe with the EPOCXWIRE hidapi / Python hid shim.

  pixi run python scripts/launch_hid_shim.py session.epocxwire --exe path\\to\\emotiv_lsl.exe
  pixi run python scripts/launch_hid_shim.py session.epocxwire --exe path\\to\\emotiv_lsl.exe --speed 1 --build

Copies tools/hidapi_shim hidapi.dll beside DLL-linked exes, and for PyInstaller
``_internal`` (or an already-extracted ``_MEI*``) swaps ``hid*.pyd`` for the
pure-Python ``hid.py`` feeder.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import List, Optional

ROOT = Path(__file__).resolve().parents[1]
SHIM_DIR = ROOT / "tools" / "hidapi_shim"
PYTHON_HID = SHIM_DIR / "python_hid" / "hid.py"


def _find_built_dll() -> Optional[Path]:
    candidates = [
        SHIM_DIR / "build" / "Release" / "hidapi.dll",
        SHIM_DIR / "build" / "hidapi.dll",
        SHIM_DIR / "build" / "Debug" / "hidapi.dll",
    ]
    for c in candidates:
        if c.is_file():
            return c
    return None


def build_dll() -> Path:
    existing = _find_built_dll()
    build_dir = SHIM_DIR / "build"
    if not (build_dir / "CMakeCache.txt").is_file():
        subprocess.check_call(
            ["cmake", "-B", str(build_dir), "-A", "x64"],
            cwd=str(SHIM_DIR),
        )
    subprocess.check_call(
        ["cmake", "--build", str(build_dir), "--config", "Release"],
        cwd=str(SHIM_DIR),
    )
    dll = _find_built_dll()
    if dll is None:
        raise FileNotFoundError("hidapi.dll not produced under tools/hidapi_shim/build")
    if existing and existing.resolve() != dll.resolve():
        pass
    return dll


def _exe_imports_hidapi_dll(exe: Path) -> bool:
    """Best-effort: dumpbin if available, else ASCII scan."""
    dumpbin = None
    pf86 = Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))
    vswhere = pf86 / "Microsoft Visual Studio" / "Installer" / "vswhere.exe"
    if vswhere.is_file():
        try:
            out = subprocess.check_output(
                [
                    str(vswhere),
                    "-latest",
                    "-find",
                    r"**\dumpbin.exe",
                ],
                text=True,
                stderr=subprocess.DEVNULL,
            )
            for line in out.splitlines():
                p = Path(line.strip())
                if p.is_file() and "HostX64" in str(p) and "x64" in str(p):
                    dumpbin = p
                    break
                if dumpbin is None and p.is_file():
                    dumpbin = p
        except (subprocess.CalledProcessError, OSError):
            dumpbin = None
    if dumpbin is not None:
        try:
            text = subprocess.check_output(
                [str(dumpbin), "/DEPENDENTS", str(exe)],
                text=True,
                stderr=subprocess.DEVNULL,
                errors="replace",
            )
            return "hidapi.dll" in text.lower()
        except (subprocess.CalledProcessError, OSError):
            pass
    try:
        raw = exe.read_bytes()
        return b"hidapi.dll" in raw
    except OSError:
        return False


def _pyinstaller_internal_dirs(exe: Path) -> List[Path]:
    dirs: List[Path] = []
    sibling = exe.parent / "_internal"
    if sibling.is_dir():
        dirs.append(sibling)
    # Already-extracted one-file MEI folders that contain this build's hid.pyd
    temp = Path(os.environ.get("TEMP", os.environ.get("TMP", r"C:\Windows\Temp")))
    if temp.is_dir():
        for mei in sorted(temp.glob("_MEI*"), key=lambda p: p.stat().st_mtime, reverse=True)[:12]:
            if any(mei.glob("hid*.pyd")) or (mei / "hidapi.dll").is_file():
                dirs.append(mei)
    return dirs


def install_python_hid_shim(target_dir: Path) -> None:
    if not PYTHON_HID.is_file():
        raise FileNotFoundError(PYTHON_HID)
    for pyd in target_dir.glob("hid*.pyd"):
        bak = pyd.with_suffix(pyd.suffix + ".bak_emotiv_shim")
        if not bak.exists():
            pyd.rename(bak)
            print(f"  renamed {pyd.name} -> {bak.name}")
    dest = target_dir / "hid.py"
    shutil.copy2(PYTHON_HID, dest)
    print(f"  installed Python hid shim -> {dest}")


def install_dll_beside_exe(exe: Path, dll: Path) -> Path:
    dest = exe.parent / "hidapi.dll"
    shutil.copy2(dll, dest)
    print(f"  copied {dll.name} -> {dest}")
    return dest


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Launch emotiv_lsl with EPOCXWIRE hid shim")
    p.add_argument("wire", type=Path, help=".epocxwire capture")
    p.add_argument("--exe", type=Path, required=True, help="Path to emotiv_lsl*.exe")
    p.add_argument("--speed", type=float, default=1.0, help="EPOCX_SPEED (default 1)")
    p.add_argument("--build", action="store_true", help="cmake-build shim hidapi.dll first")
    p.add_argument(
        "--dll-only",
        action="store_true",
        help="Only install hidapi.dll beside exe (skip Python hid.py patch)",
    )
    p.add_argument(
        "--python-hid-dir",
        type=Path,
        default=None,
        help="Explicit _internal / _MEI dir to patch with python_hid/hid.py",
    )
    p.add_argument(
        "--no-run",
        action="store_true",
        help="Install shim artifacts only; do not start the exe",
    )
    p.add_argument("exe_args", nargs="*", help="Extra args forwarded to the exe")
    args = p.parse_args(argv)

    wire = args.wire.expanduser().resolve()
    exe = args.exe.expanduser().resolve()
    if not wire.is_file():
        print(f"Wire not found: {wire}", file=sys.stderr)
        return 2
    if not exe.is_file():
        print(f"Exe not found: {exe}", file=sys.stderr)
        return 2

    dll = _find_built_dll()
    if args.build or dll is None:
        print("Building hidapi.dll shim...")
        try:
            dll = build_dll()
        except (OSError, subprocess.CalledProcessError) as e:
            print(f"WARNING: could not build hidapi.dll ({e}); Python hid shim may still work")
            dll = _find_built_dll()

    uses_dll = _exe_imports_hidapi_dll(exe)
    print(f"Target: {exe}")
    print(f"  imports hidapi.dll: {uses_dll}")

    if dll is not None and (uses_dll or args.dll_only):
        install_dll_beside_exe(exe, dll)
    elif dll is not None and not uses_dll:
        # Still copy — harmless for static builds; helps if a side DLL is loaded later.
        install_dll_beside_exe(exe, dll)

    if not args.dll_only:
        dirs = []
        if args.python_hid_dir is not None:
            dirs = [args.python_hid_dir.expanduser().resolve()]
        else:
            dirs = _pyinstaller_internal_dirs(exe)
        if dirs:
            for d in dirs[:3]:
                print(f"Patching Python hid in {d}")
                install_python_hid_shim(d)
        else:
            print(
                "No _internal/_MEI hid*.pyd found yet. For one-file PyInstaller, "
                "run the exe once to extract, re-run this launcher with "
                "--python-hid-dir %TEMP%\\_MEIxxxx, or extract to one-dir."
            )

    env = os.environ.copy()
    env["EPOCX_WIRE"] = str(wire)
    env["EPOCX_WIRE_PATH"] = str(wire)
    env["EPOCX_SPEED"] = str(args.speed)
    # Prefer our DLL when the process probes the exe directory / PATH.
    env["PATH"] = str(exe.parent) + os.pathsep + env.get("PATH", "")

    if args.no_run:
        print("Shim installed; skipping exec (--no-run).")
        return 0

    cmd = [str(exe), *args.exe_args]
    print(f"Starting: {cmd}")
    print(f"  EPOCX_WIRE={wire}")
    print(f"  EPOCX_SPEED={args.speed}")
    proc = subprocess.Popen(cmd, cwd=str(exe.parent), env=env)
    # If one-file just extracted a new _MEI, try a quick late patch (best-effort race).
    if not args.dll_only and not _pyinstaller_internal_dirs(exe):
        deadline = time.time() + 8.0
        while time.time() < deadline and proc.poll() is None:
            dirs = _pyinstaller_internal_dirs(exe)
            if dirs:
                for d in dirs[:1]:
                    try:
                        print(f"Late-patch Python hid in {d}")
                        install_python_hid_shim(d)
                    except OSError as e:
                        print(f"  late patch failed: {e}")
                break
            time.sleep(0.2)
    rc = proc.wait()
    print(f"Exit: {rc}")
    return int(rc or 0)


if __name__ == "__main__":
    raise SystemExit(main())
