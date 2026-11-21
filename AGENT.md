# AGENT.md - Emotiv-LSL Project

## Build/Run/Test Commands
- **Run main LSL server**: `python main.py`
- **macOS (Apple Silicon) setup**:
  - `brew install hidapi libusb labstreaminglayer/tap/lsl`
  - Use a **native arm64** toolchain (`arch -arm64 zsh`, arm64 `uv`); avoid Rosetta-only envs when `/usr/local` has Intel `liblsl`
  - `uv python pin 3.12 && uv sync`
  - List receiver: `python scripts/list_emotiv_hid.py`
  - Build app: `python scripts/build_macOS_app.py` → `dist/emotiv_lsl/emotiv_lsl`
- **Offline tests**: `python -m tests.test_epoc_x_decode` and `python -m tests.test_epocx_wire`
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

## Epoc X decode (Android-aligned)
- EEG decode mirrors EmotivPRO Android Mode 7 (EpocPlus LE16 + `getRawEegId` remap + ADC ÷7.8).
- Crypto: Version2 EpocXMode key schedule; XOR `0x55` → AES-128-ECB. V4/`findKey` unsupported.
- Transport remains USB HID only (no BLE GATT stack here). BLE checklist lives in the reverse-engineer protocol memory (`epocx-ble-protocol.md`).
- Motion: Android 20 B BE Q/ACC/MAG decoder exists; LSL motion defaults off until a real 20 B HID report is confirmed.
- Offline checks: `python -m tests.test_epoc_x_decode`, `python -m tests.test_epocx_wire`
- Exact HID capture: `python main.py --record session.epocxwire` then `python main.py --replay session.epocxwire`. Format is EPOCXWIRE/2 (REPORT/EMPTY/ERROR/OPEN/CLOSE); v1 still readable. XDF→wire is synthetic; prefer live `--record` for gold fixtures.
- Replay (synthetic): `python scripts/xdf_to_epocx_wire.py in.xdf -o out.epocxwire` then `python main.py --replay out.epocxwire` (or pass `.xdf` directly). Same `.epocxwire` works with `emotiv-lsl-cpp --replay` (v1; v2 C++ support later).
- WinUHid OS feeder (later): `WireFeeder` / `WinUHidFeeder` stub + `python scripts/feed_winuhid.py session.epocxwire --dry-run`.

## Code Style
- **Imports**: Standard library first, then third-party (hid, pylsl, Crypto), then local imports
- **Classes**: CamelCase (EmotivEpocX), inherit from EmotivBase
- **Methods**: snake_case (get_crypto_key, main_loop)  
- **Constants**: UPPER_CASE (SRATE in config.py)
- **Error handling**: Raise exceptions with descriptive messages
- **Type hints**: Use -> return type annotations where present
