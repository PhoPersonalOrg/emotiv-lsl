# Emotiv hidapi shim (EPOCXWIRE → old / live binaries)

Process-local Emotiv HID emulation for binaries that open the dongle via **hidapi** (no kernel driver). Prefer this over WinUHid for old-exe → LabRecorder → XDF compare.

## What it fakes

| Field | Value |
| --- | --- |
| `manufacturer_string` | `Emotiv` |
| `usage` | `2` (interface `1`) |
| `vendor_id` | `0x1234` |
| `product_id` | `0xED02` |
| `serial_number` | wire header `serial=` (AES key material) |
| `hid_read` / `hid_read_timeout` | paced REPORT payloads from `.epocxwire` |

## Build (`hidapi.dll`)

```powershell
cd tools/hidapi_shim
cmake -B build -A x64
cmake --build build --config Release
# -> build/Release/hidapi.dll
```

Works when the consumer **dynamically** loads `hidapi.dll` (e.g. `emotiv-lsl-cpp` `build/install/emotiv_lsl.exe`). Static-linked or cython-`hid*.pyd` embeds need the Python shim below.

## Env

| Variable | Meaning |
| --- | --- |
| `EPOCX_WIRE` or `EPOCX_WIRE_PATH` | Path to `.epocxwire` |
| `EPOCX_SPEED` | Pace multiplier (`1` realtime, `0` ASAP) |

## Launch (recommended)

From `emotiv-lsl`:

```powershell
pixi run python scripts/launch_hid_shim.py session.epocxwire `
  --exe "C:\path\to\emotiv_lsl.exe" --speed 1
```

The launcher:

1. Builds/finds `hidapi.dll` when the target imports it, and copies it beside the exe (DLL search order).
2. For **PyInstaller** trees (`_internal` / extracted one-dir): renames `hid*.pyd` aside and drops `python_hid/hid.py` so `import hid` uses the pure-Python feeder (frozen one-file packs statically link hidapi into the `.pyd`; replacing only `hidapi.dll` is not enough).

Then start LabRecorder, wait for EOF / stop the exe, and compare:

```powershell
pixi run python scripts/compare_xdfs.py lab.xdf --wire session.epocxwire
```

## Not in scope

- Device Manager / USBView fidelity → deferred **UDECX / USBIP** (see project `AGENT.md`).
- WinUHid remains experimental (no serial string).
