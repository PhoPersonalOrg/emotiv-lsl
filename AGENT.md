# AGENT.md - Emotiv-LSL Project

## Build/Run/Test Commands
- **Run main LSL server**: `python main.py`
- **macOS (Apple Silicon) setup**:
  - `brew install hidapi libusb labstreaminglayer/tap/lsl`
  - Use a **native arm64** toolchain (`arch -arm64 zsh`, arm64 `uv`); avoid Rosetta-only envs when `/usr/local` has Intel `liblsl`
  - `uv python pin 3.12 && uv sync`
  - List receiver: `python scripts/list_emotiv_hid.py`
  - Build app: `python scripts/build_macOS_app.py` → `dist/emotiv_lsl/emotiv_lsl`
- **Offline tests**: `python -m tests.test_epoc_x_decode`, `python -m tests.test_epocx_wire`, `python -m tests.test_flutter_raw_csv`
- **Client compat (headset emulator)**: `python -m tests.test_client_compat`, `python -m tests.test_client_compat_flutter`, and `python -m tests.test_client_compat_inventory` (offline always; C++ LSL compare skips unless `EMOTIV_LSL_CPP` or sibling `emotiv-lsl-cpp` build is found). Flutter in-app crypto gate: `flutter test test/crypto_codec_test.dart` in sibling `flutter_application_final_emotiv_logger`.
- **Install dependencies**: `uv sync` (preferred) or `pip install -r requirements.txt`
- **Setup conda env**: `conda create -n lsl_env python=3.8 && conda activate lsl_env && conda install -c conda-forge liblsl`
- **View LSL stream**: `bsl_stream_viewer` (after installing bsl)
- **Run examples**: `python examples/read_data.py` or `python examples/pho_read_and_export_mne.py`

## Architecture
- **Main entry**: `main.py` instantiates EmotivEpocX and runs main_loop()
- **Core module**: `emotiv_lsl/` contains device classes (EmotivEpocX, EmotivEpocPlus, EmotivBase)
- **Examples**: `examples/` has data reading, MNE export, and analysis notebooks
- **LSL streaming**: Uses pylsl to stream EEG data from Emotiv headsets
- **Encryption**: AES decryption using device serial-based crypto keys
- **Dependencies**: Python 3.8+, hidapi, pycryptodome, pylsl, mne, matplotlib

## Epoc X decode (2.4 GHz USB HID-first)
- Transport is **2.4 GHz USB HID** (`0x1234:0xED02` and `0x21A1:0x0001`, composite `MI_01`: `(usage_page & 0xFF00) == 0xFF00`, `usage == 2`).
- Queries **USB HID Feature Report `0x00`** on open to discover `dongle_fw`, `headset_fw`, paired `headset_serial` (`EPOCX-<HEX>`), `eeg_rate` (`128/256 Hz`), `eeg_bits` (`14/16`), and `mems_rate` (`0/32/64/128 Hz`, default `32 Hz`). Optional **33-byte Output Report `0x00`** (`00 66 BB 20 12 <CFG>` + `0x88 0x12` ACK) switches sample rates (`--eeg-rate`, `--eeg-bits`, `--mems-rate`).
- Crypto supports both **Version 2** (`fw <= 0x073E`: `"UD..."` dongle reverse, `XOR 0x55`, AES-128-ECB) and **Version 4** (`fw >= 0x073F`: paired `headset_serial[:8]` SHA-256 schedule, `XOR 0x14`, AES-256-ECB).
- Every 32-byte USB HID input report is decrypted (`is_mems=False`) and demuxed on `plain[1] & 0x7F`: `0x10` -> Mode-7 EEG (`14` ch uV + continuous per-electrode CQ score `cq_norm_to_score` (`0->0, 0.2->1, 0.7->2, 1.7->3, >1.7->3+(norm-1.7)`) + `counter == eeg_rate - 1` battery latch + `marker_hw`), `0x20` -> 20-byte Motion (`Q0..Q3`, `AccX..Z`, `MagX..Z`, `"top"` or `"back"` headband orientation). Motion LSL is enabled by default (`--no-motion` to disable).
- Historical XDF CQ comparability: `emotiv_lsl/legacy_xdf_cq.py` (`reconstruct_legacy_equality_array`) and `scripts/recover_legacy_xdf_cq.py` losslessly unpack `words[7]` from legacy nibble-scraped `Epoc X eQuality` XDF streams and reconstruct the exact same continuous 14-channel CQ scores and battery `%`.
- Isolated offline replay compatibility: `emotiv_lsl/flutter_raw_csv.py` (including 64B and 84B BLE `81072f41` super-frame splitting) and `serial_source="ble_hex"` remain available strictly for offline `.epocxwire` / CSV decoding.
- Offline checks: `python -m tests.test_epoc_x_decode`, `python -m tests.test_epocx_wire`, `python -m tests.test_legacy_xdf_cq`, `python -m tests.test_flutter_raw_csv`, `python -m tests.test_client_compat`, `python -m tests.test_client_compat_flutter`, `python -m tests.test_client_compat_inventory`
- Exact HID capture: `python main.py --record session.epocxwire` then `python main.py --replay session.epocxwire`. Format is EPOCXWIRE/2 (REPORT/EMPTY/ERROR/OPEN/CLOSE); v1 still readable. XDF→wire is synthetic; prefer live `--record` for gold fixtures.
- Replay (synthetic): `python scripts/xdf_to_epocx_wire.py in.xdf -o out.epocxwire` then `python main.py --replay out.epocxwire` (or pass `.xdf` directly). Same `.epocxwire` works with `emotiv-lsl-cpp --replay` (v1; v2 C++ support later — compare script auto-exports v1).
- Python vs C++ LSL parity (suite): `python -m tests.test_client_compat` — hard EEG/CQ sample asserts; optional C++ subprocess when binary present. Flutter session path: `python -m tests.test_client_compat_flutter`. Inventory layer: `python -m tests.test_client_compat_inventory`.
- Python vs C++ LSL parity (CLI): `python scripts/compare_py_cpp_replay.py session.epocxwire` (sequential replay + sample compare; `--cpp-exe` / `EMOTIV_LSL_CPP` if needed).
- Old exe without `--replay`: process-local hidapi shim (`tools/hidapi_shim/`) — build `hidapi.dll`, then `python scripts/launch_hid_shim.py session.epocxwire --exe path\to\emotiv_lsl.exe --speed 1`. Injects wire serial/mfg/usage; prefer over WinUHid.
- LabRecorder XDF vs Python wire gold: `python scripts/compare_xdfs.py lab.xdf --wire session.epocxwire` (lag-scan on DIFF).
- WinUHid OS feeder: **experimental / incomplete** (no USB serial → AES breaks). Dry-run only is fine: `python scripts/feed_winuhid.py session.epocxwire --dry-run`. Real submit needs driver + `WINUHID_DLL`.
- Deferred: UDECX / USBIP full virtual USB only if Device Manager / non-hidapi consumers need fidelity.

## Code Style
- **Imports**: Standard library first, then third-party (hid, pylsl, Crypto), then local imports
- **Classes**: CamelCase (EmotivEpocX), inherit from EmotivBase
- **Methods**: snake_case (get_crypto_key, main_loop)  
- **Constants**: UPPER_CASE (SRATE in config.py)
- **Error handling**: Raise exceptions with descriptive messages
- **Type hints**: Use -> return type annotations where present
