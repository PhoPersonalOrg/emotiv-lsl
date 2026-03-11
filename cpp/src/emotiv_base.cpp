#include "emotiv_base.h"
#include "config.h"
#include <iostream>
#include <iomanip>
#include <sstream>
#include <stdexcept>
#include <chrono>

EmotivBase::EmotivBase(bool enable_motion, bool enable_quality)
    : enable_motion_data(enable_motion), enable_electrode_quality_stream(enable_quality) {
}

EmotivBase::~EmotivBase() {
}

std::string EmotivBase::get_lsl_source_id() {
    std::vector<uint8_t> key = get_crypto_key();
    std::stringstream ss;
    ss << device_name << "_" << KeyModel << "_";
    for (uint8_t b : key) {
        ss << std::hex << std::setw(2) << std::setfill('0') << (int)b;
    }
    return ss.str();
}

hid_device* EmotivBase::get_hid_device() {
    if (hid_init() != 0) {
        throw std::runtime_error("Failed to initialize hidapi");
    }

    struct hid_device_info *devs, *cur_dev;
    devs = hid_enumerate(0x1234, 0x0000); // Vendor ID 0x1234 for Emotiv
    cur_dev = devs;
    hid_device* target_device = nullptr;
    std::string target_path = "";

    while (cur_dev) {
        std::wstring mfg(cur_dev->manufacturer_string ? cur_dev->manufacturer_string : L"");
        if (mfg == L"Emotiv") {
            if (cur_dev->usage == 2 || (cur_dev->usage == 0 && cur_dev->interface_number == 1)) {
                if (cur_dev->serial_number) {
                    std::wstring serial_ws(cur_dev->serial_number);
                    serial_number = std::string(serial_ws.begin(), serial_ws.end());
                }
                target_path = cur_dev->path;
                target_device = hid_open_path(cur_dev->path);
                if (target_device) {
                    break;
                }
            }
        }
        cur_dev = cur_dev->next;
    }
    
    // In case Vendor ID is different or 0x1243 etc., we enumerate all and check mfg string
    if (!target_device) {
        hid_free_enumeration(devs);
        devs = hid_enumerate(0x0, 0x0);
        cur_dev = devs;
        while (cur_dev) {
            std::wstring mfg(cur_dev->manufacturer_string ? cur_dev->manufacturer_string : L"");
            if (mfg == L"Emotiv") {
                if (cur_dev->usage == 2 || (cur_dev->usage == 0 && cur_dev->interface_number == 1)) {
                    if (cur_dev->serial_number) {
                        std::wstring serial_ws(cur_dev->serial_number);
                        serial_number = std::string(serial_ws.begin(), serial_ws.end());
                    }
                    target_device = hid_open_path(cur_dev->path);
                    if (target_device) break;
                }
            }
            cur_dev = cur_dev->next;
        }
    }

    hid_free_enumeration(devs);
    if (!target_device) {
        throw std::runtime_error("Emotiv Epoc Base Headset not found");
    }
    return target_device;
}

lsl::stream_info EmotivBase::add_lsl_outlet_info_common(lsl::stream_info& info) {
    lsl::xml_element desc = info.desc();
    desc.append_child_value("manufacturer", "emotiv_lsl_cpp");
    desc.append_child_value("version", "0.1.0");
    desc.append_child_value("description", "Logged by the open-source tool 'emotiv_lsl' to record raw data from Emotiv headsets.");
    return info;
}

std::vector<std::string> EmotivBase::eeg_quality_channel_names() const {
    std::vector<std::string> names;
    for (const auto& name : eeg_channel_names) {
        names.push_back("q" + name);
    }
    return names;
}

lsl::stream_info EmotivBase::get_lsl_outlet_eeg_stream_info() {
    lsl::stream_info info("Epoc X", "EEG", eeg_channel_names.size(), SRATE, lsl::cf_float32, get_lsl_source_id());
    info = add_lsl_outlet_info_common(info);
    return info;
}

lsl::stream_info EmotivBase::get_lsl_outlet_motion_stream_info() {
    lsl::stream_info info("Epoc X Motion", "SIGNAL", 6, MOTION_SRATE, lsl::cf_float32, get_lsl_source_id());
    info = add_lsl_outlet_info_common(info);
    return info;
}

lsl::stream_info EmotivBase::get_lsl_outlet_electrode_quality_stream_info() {
    lsl::stream_info info("Epoc X eQuality", "RAW", eeg_channel_names.size(), SRATE, lsl::cf_float32, get_lsl_source_id());
    info = add_lsl_outlet_info_common(info);
    return info;
}

std::string EmotivBase::convertEPOC_PLUS(uint8_t value_1, uint8_t value_2) {
    double edk_value = ((value_1 * 0.128205128205129) + 4201.02564096001) + ((value_2 - 128) * 32.82051289);
    // Convert directly? We can actually return double but the original used string conversion, 
    // we'll just return a string for exact fidelity or use double internally. 
    // In Python it did: float(str(edk_value)) which is weird. 
    // We'll just format as string if needed, or simply let the caller use the math:
    std::stringstream ss;
    ss << std::fixed << std::setprecision(8) << edk_value;
    return ss.str();
}

std::vector<double> EmotivBase::extractQualityValues(const std::vector<uint8_t>& data) {
    std::vector<double> q_vals(14, 0.0);
    if (KeyModel == 1 || KeyModel == 2 || KeyModel == 5 || KeyModel == 6 || KeyModel == 8) {
        std::map<std::string, double> q_dict;
        q_dict["AF3"] = data[16] & 0xF;
        q_dict["F7"]  = (data[16] >> 4) & 0xF;
        q_dict["F3"]  = data[17] & 0xF;
        q_dict["FC5"] = (data[17] >> 4) & 0xF;
        q_dict["T7"]  = data[18] & 0xF;
        q_dict["P7"]  = (data[18] >> 4) & 0xF;
        q_dict["O1"]  = data[19] & 0xF;
        q_dict["O2"]  = (data[19] >> 4) & 0xF;
        q_dict["P8"]  = data[20] & 0xF;
        q_dict["T8"]  = (data[20] >> 4) & 0xF;
        q_dict["FC6"] = data[21] & 0xF;
        q_dict["F4"]  = (data[21] >> 4) & 0xF;
        q_dict["F8"]  = data[22] & 0xF;
        q_dict["AF4"] = (data[22] >> 4) & 0xF;

        for (size_t i = 0; i < eeg_channel_names.size(); ++i) {
            q_vals[i] = q_dict[eeg_channel_names[i]];
        }
    } else {
        throw std::runtime_error("Unknown KeyModel for quality extraction");
    }
    return q_vals;
}

void EmotivBase::main_loop() {
    hid_device* device = get_hid_device();
    std::cout << "Connected to Emotiv device: " << device_name << std::endl;
    
    std::unique_ptr<lsl::stream_outlet> eeg_outlet;
    std::unique_ptr<lsl::stream_outlet> motion_outlet;
    std::unique_ptr<lsl::stream_outlet> eeg_quality_outlet;

    uint32_t packet_count = 0;
    std::vector<uint8_t> buffer(READ_SIZE);

    while (true) {
        int bytes_read = hid_read(device, buffer.data(), READ_SIZE);
        if (bytes_read < 0) {
            std::cerr << "Disconnected or read error" << std::endl;
            break;
        } else if (bytes_read == 0) {
            continue; // No data yet
        }

        std::vector<uint8_t> data(buffer.begin(), buffer.begin() + bytes_read);
        packet_count++;

        if (validate_data(data)) {
            EmotivData result = decode_data(data);
            
            if (result.has_quality && enable_electrode_quality_stream) {
                if (!eeg_quality_outlet) {
                    lsl::stream_info info = get_lsl_outlet_electrode_quality_stream_info();
                    eeg_quality_outlet = std::make_unique<lsl::stream_outlet>(info);
                    std::cout << "Set up EEG Sensor Quality outlet!" << std::endl;
                }
                eeg_quality_outlet->push_sample(result.quality_data);
            }

            if (result.has_motion && enable_motion_data) {
                if (!has_motion_data) {
                    has_motion_data = true;
                    std::cout << "Got first motion data!" << std::endl;
                }
                if (!motion_outlet) {
                    lsl::stream_info info = get_lsl_outlet_motion_stream_info();
                    motion_outlet = std::make_unique<lsl::stream_outlet>(info);
                    std::cout << "Set up motion outlet!" << std::endl;
                }
                motion_outlet->push_sample(result.motion_data);
            } else if (result.has_eeg) {
                if (!eeg_outlet) {
                    lsl::stream_info info = get_lsl_outlet_eeg_stream_info();
                    eeg_outlet = std::make_unique<lsl::stream_outlet>(info);
                    std::cout << "Set up EEG outlet! Channels: " << result.eeg_data.size() << std::endl;
                }
                eeg_outlet->push_sample(result.eeg_data);
            }
        }
    }
    hid_close(device);
    hid_exit();
}
