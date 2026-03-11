# Emotiv-LSL C++ Port Plan

## Goal Description
Create a pure C++ version of the `emotiv_lsl` project. This tool connects to Emotiv EPOC/EPOC+ and EPOC X headsets via USB HID, decrypts the proprietary data stream using an AES-128 ECB cipher (key derived from serial number), and pushes the EEG, motion, and sensor quality data to LSL (Lab Streaming Layer). The C++ version must build across Windows, macOS, and Linux using CMake.

## Proposed Architecture
The C++ project will be created in `c:\Users\pho\repos\ACTIVE_DEV\workspace\PhoPersonalOrg\emotiv-lsl-cpp`.

### Dependencies (managed via CMake `FetchContent` or system packages where appropriate):
1. **LSL (liblsl)**: For pushing streams to the Lab Streaming Layer.
2. **hidapi**: For cross-platform USB HID device communication.
3. **tiny-AES-c**: A highly portable, small C library for AES-128 ECB decryption (since handling OpenSSL across platforms natively without a package manager can be cumbersome, a tiny single-file AES library is perfect for this one requirement).

### Project Structure (`emotiv-lsl-cpp`)
*   `CMakeLists.txt`: Root CMake configuration, fetching/finding dependencies and building the executable.
*   `src/main.cpp`: Entry point. Connects to the device, sets up the LSL outlets, and runs the main read loop.
*   `src/emotiv_base.h` & `src/emotiv_base.cpp`: Base class containing data structures, conversion logic ([convertEPOC_PLUS](file:///c:/Users/pho/repos/ACTIVE_DEV/workspace/PhoPersonalOrg/emotiv-lsl/emotiv_lsl/emotiv_base.py#134-137)), LSL stream setup routines.
*   `src/emotiv_epoc_x.h` & `src/emotiv_epoc_x.cpp`: Subclass implementing the Epoc X specific specifics (key generation from the serial number, specific AES decryption logic, decoding EEG & motion).

### Key Components to Port
- **Device connection**: Replace `hid.Device` enumeration with `hid_enumerate()` and `hid_open_path()`.
- **Crypto Key extraction**: Implement [get_crypto_key()](file:///c:/Users/pho/repos/ACTIVE_DEV/workspace/PhoPersonalOrg/emotiv-lsl/emotiv_lsl/emotiv_epoc_x.py#45-60) translating the Python array indexing of the serial string into C++ `std::vector<uint8_t>`.
- **Buffer handling**: Replace Python byte array slicing with `std::vector<uint8_t>` or raw C arrays.
- **Conversion Math**: Re-implement [convertEPOC_PLUS](file:///c:/Users/pho/repos/ACTIVE_DEV/workspace/PhoPersonalOrg/emotiv-lsl/emotiv_lsl/emotiv_base.py#134-137) and [decode_motion_data](file:///c:/Users/pho/repos/ACTIVE_DEV/workspace/PhoPersonalOrg/emotiv-lsl/emotiv_lsl/emotiv_epoc_x.py#208-244) (accelerometer/gyro offset arithmetic).
- **LSL integration**: Use `<lsl_cpp.h>` to create `lsl::stream_info` and `lsl::stream_outlet` for EEG (14 ch), Motion (6 ch), and Electrode Quality.

## Verification Plan

### Automated Build Tests
- Generate CMake config (`cmake -B build`) to verify dependency resolution (liblsl, hidapi, tiny-AES-c).
- Compile the code (`cmake --build build`) to ensure there are no compilation errors.

### Execution & Integration Verification
- **Run the executable**: `emotiv-lsl-cpp.exe`. Ensure it successfully initiates hidapi and initializes the AES cipher and LSL outlets.
- **Simulate offline/dummy if no headset is connected**: At minimum, we'll verify it tries to connect and if no headset is found, exits gracefully (or waits). Since I do not have physical access to the headset during the build, I will ensure LSL outlets are structurally identical to the Python script by inspecting the generated code.

### Manual Verification
- Ask the user to run the generated executable `emotiv-lsl-cpp.exe` locally with their actual Emotiv Epoc headset connected.
- Ask the user to verify LSL streams are broadcasting using `bsl_stream_viewer` as they normally would.
