#include "emotiv_epoc_x.h"
#include "config.h"
#include <iostream>

EmotivEpocX::EmotivEpocX(bool enable_motion, bool enable_quality)
    : EmotivBase(enable_motion, enable_quality) {
    device_name = "Emotiv Epoc X";
    KeyModel = 8;
}

EmotivEpocX::~EmotivEpocX() {}

std::vector<uint8_t> EmotivEpocX::get_crypto_key() {
    if (serial_number.empty()) {
        hid_device* dev = get_hid_device();
        // serial_number is now populated by get_hid_device()
        hid_close(dev);
    }
    std::string serial = serial_number;
    std::vector<uint8_t> key;
    if (serial.length() >= 16) {
        int len = serial.length();
        key.push_back(serial[len - 1]);
        key.push_back(serial[len - 2]);
        key.push_back(serial[len - 4]);
        key.push_back(serial[len - 4]);
        key.push_back(serial[len - 2]);
        key.push_back(serial[len - 1]);
        key.push_back(serial[len - 2]);
        key.push_back(serial[len - 4]);
        key.push_back(serial[len - 1]);
        key.push_back(serial[len - 4]);
        key.push_back(serial[len - 3]);
        key.push_back(serial[len - 2]);
        key.push_back(serial[len - 1]);
        key.push_back(serial[len - 2]);
        key.push_back(serial[len - 2]);
        key.push_back(serial[len - 3]);
    } else {
        throw std::runtime_error("Serial number too short to derive key");
    }
    return key;
}

std::string EmotivEpocX::get_lsl_source_id() {
    std::vector<uint8_t> key = get_crypto_key();
    std::string hex_key = "";
    const char* hex_chars = "0123456789abcdef";
    for (uint8_t b : key) {
        hex_key += hex_chars[b >> 4];
        hex_key += hex_chars[b & 0x0F];
    }
    return device_name + "_" + std::to_string(KeyModel) + "_" + hex_key;
}

lsl::stream_info EmotivEpocX::get_lsl_outlet_motion_stream_info() {
    lsl::stream_info info("Epoc X Motion", "SIGNAL", 6, MOTION_SRATE, lsl::cf_float32, get_lsl_source_id());
    lsl::xml_element chns = info.desc().append_child("channels");
    std::vector<std::string> acc_names = {"AccX", "AccY", "AccZ"};
    for (const auto& label : acc_names) {
        lsl::xml_element ch = chns.append_child("channel");
        ch.append_child_value("label", label);
        ch.append_child_value("unit", "g");
        ch.append_child_value("type", "ACC");
        ch.append_child_value("scaling_factor", "1");
    }
    std::vector<std::string> gyro_names = {"GyroX", "GyroY", "GyroZ"};
    for (const auto& label : gyro_names) {
        lsl::xml_element ch = chns.append_child("channel");
        ch.append_child_value("label", label);
        ch.append_child_value("unit", "deg/s");
        ch.append_child_value("type", "GYRO");
        ch.append_child_value("scaling_factor", "1");
    }
    return add_lsl_outlet_info_common(info);
}

lsl::stream_info EmotivEpocX::get_lsl_outlet_eeg_stream_info() {
    lsl::stream_info info("Epoc X", "EEG", eeg_channel_names.size(), SRATE, lsl::cf_float32, get_lsl_source_id());
    lsl::xml_element chns = info.desc().append_child("channels");
    for (const auto& label : eeg_channel_names) {
        lsl::xml_element ch = chns.append_child("channel");
        ch.append_child_value("label", label);
        ch.append_child_value("unit", "microvolts");
        ch.append_child_value("type", "EEG");
        ch.append_child_value("scaling_factor", "1");
    }
    lsl::xml_element cap = info.desc().append_child("cap");
    cap.append_child_value("name", "easycap-M1");
    cap.append_child_value("labelscheme", "10-20");
    return add_lsl_outlet_info_common(info);
}

lsl::stream_info EmotivEpocX::get_lsl_outlet_electrode_quality_stream_info() {
    lsl::stream_info info("Epoc X eQuality", "RAW", eeg_channel_names.size(), SRATE, lsl::cf_float32, get_lsl_source_id());
    lsl::xml_element chns = info.desc().append_child("channels");
    for (const auto& label : eeg_quality_channel_names()) {
        lsl::xml_element ch = chns.append_child("channel");
        ch.append_child_value("label", label);
        ch.append_child_value("unit", "microvolts");
        ch.append_child_value("type", "RAW");
        ch.append_child_value("scaling_factor", "1");
    }
    lsl::xml_element cap = info.desc().append_child("cap");
    cap.append_child_value("name", "easycap-M1");
    cap.append_child_value("labelscheme", "10-20");
    return add_lsl_outlet_info_common(info);
}

EmotivData EmotivEpocX::decode_data(const std::vector<uint8_t>& data) {
    if (!ctx_initialized) {
        std::vector<uint8_t> key = get_crypto_key();
        AES_init_ctx(&ctx, key.data());
        ctx_initialized = true;
    }

    std::vector<uint8_t> dec_data = data;
    for (size_t i = 0; i < dec_data.size(); i++) {
        dec_data[i] ^= 0x55;
    }

    // Since our buffer is exactly 32 bytes and ECB processes 16 bytes at a time
    AES_ECB_decrypt(&ctx, dec_data.data());
    AES_ECB_decrypt(&ctx, dec_data.data() + 16);

    EmotivData result;

    if (dec_data.size() > 1 && dec_data[1] == 32) {
        // Motion packet
        result.motion_data = decode_motion_data(dec_data);
        result.has_motion = true;
        return result;
    }

    if (enable_electrode_quality_stream) {
        try {
            result.quality_data = extractQualityValues(dec_data);
            result.has_quality = true;
        } catch (...) {
            // failed to extract quality
        }
    }

    std::vector<double> packet_data;
    for (int i = 2; i < 16; i += 2) {
        packet_data.push_back(std::stod(convertEPOC_PLUS(dec_data[i], dec_data[i+1]))); 
    }
    for (int i = 18; i < dec_data.size(); i += 2) {
        packet_data.push_back(std::stod(convertEPOC_PLUS(dec_data[i], dec_data[i+1]))); 
    }

    if (packet_data.size() == 14) {
        // Swap positions
        std::swap(packet_data[0], packet_data[2]);   // AF3 and F3
        std::swap(packet_data[13], packet_data[11]); // AF4 and F4
        std::swap(packet_data[1], packet_data[3]);   // F7 and FC5
        std::swap(packet_data[10], packet_data[12]); // FC6 and F8
        result.eeg_data = packet_data;
        result.has_eeg = true;
    }

    return result;
}

std::vector<double> EmotivEpocX::decode_motion_data(const std::vector<uint8_t>& data) {
    const int motion_positions[] = {2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 30, 31};
    int len_pos = sizeof(motion_positions) / sizeof(motion_positions[0]);
    std::vector<double> motion_data;

    for (int i = 0; i < len_pos; i += 2) {
        if (i + 1 < len_pos) {
            int pos1 = motion_positions[i];
            int pos2 = motion_positions[i + 1];
            if (pos1 < data.size() && pos2 < data.size()) {
                double value = ((8191.88296790168 + (data[pos1] * 1.00343814821)) + ((data[pos2] - 128.00001) * 64.00318037383));
                motion_data.push_back(value);
            }
        }
    }

    std::vector<double> result(6, 0.0);
    if (motion_data.size() >= 6) {
        double acc_scale = 1.0 / 16384.0;
        double gyro_scale = 1.0 / 131.0;
        
        result[0] = motion_data[0] * acc_scale;
        result[1] = motion_data[1] * acc_scale;
        result[2] = motion_data[2] * acc_scale;
        result[3] = motion_data[3] * gyro_scale;
        result[4] = motion_data[4] * gyro_scale;
        result[5] = motion_data[5] * gyro_scale;
    }
    return result;
}

bool EmotivEpocX::validate_data(const std::vector<uint8_t>& data) {
    return data.size() == READ_SIZE;
}
