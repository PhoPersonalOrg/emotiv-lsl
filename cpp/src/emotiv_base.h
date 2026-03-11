#pragma once

#include <string>
#include <vector>
#include <map>
#include <memory>
#include <lsl_cpp.h>
#include <hidapi.h>

extern "C" {
#include "aes.h"
}

struct EmotivData {
    std::vector<double> eeg_data;
    std::vector<double> motion_data;
    std::vector<double> quality_data;
    bool has_eeg = false;
    bool has_motion = false;
    bool has_quality = false;
};

class EmotivBase {
public:
    EmotivBase(bool enable_motion_data = false, bool enable_electrode_quality_stream = false);
    virtual ~EmotivBase();

    void main_loop();

protected:
    int READ_SIZE = 32;
    std::string serial_number;
    std::string device_name = "UnknownEmotivHeadset";
    int KeyModel = 1;

    bool has_motion_data = false;
    bool enable_debug_logging = false;
    bool is_reverse_engineer_mode = false;
    bool enable_electrode_quality_stream = false;
    bool enable_motion_data = false;

    // Cryptography state
    struct AES_ctx ctx;
    bool ctx_initialized = false;

    virtual std::vector<uint8_t> get_crypto_key() = 0;
    virtual std::string get_lsl_source_id();
    hid_device* get_hid_device();

    // LSL outlets
    lsl::stream_info add_lsl_outlet_info_common(lsl::stream_info& info);
    virtual lsl::stream_info get_lsl_outlet_eeg_stream_info();
    virtual lsl::stream_info get_lsl_outlet_motion_stream_info();
    virtual lsl::stream_info get_lsl_outlet_electrode_quality_stream_info();

    // Data handling
    virtual EmotivData decode_data(const std::vector<uint8_t>& data) = 0;
    virtual bool validate_data(const std::vector<uint8_t>& data) = 0;

    // Helpers
    std::string convertEPOC_PLUS(uint8_t value_1, uint8_t value_2);
    std::vector<double> extractQualityValues(const std::vector<uint8_t>& data);

    const std::vector<std::string> eeg_channel_names = {"AF3", "F7", "F3", "FC5", "T7", "P7", "O1", "O2", "P8", "T8", "FC6", "F4", "F8", "AF4"};
    std::vector<std::string> eeg_quality_channel_names() const;
};
