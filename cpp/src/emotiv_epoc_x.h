#pragma once
#include "emotiv_base.h"

class EmotivEpocX : public EmotivBase {
public:
    EmotivEpocX(bool enable_motion_data = false, bool enable_electrode_quality_stream = false);
    ~EmotivEpocX();

protected:
    std::vector<uint8_t> get_crypto_key() override;
    std::string get_lsl_source_id() override;
    
    lsl::stream_info get_lsl_outlet_eeg_stream_info() override;
    lsl::stream_info get_lsl_outlet_motion_stream_info() override;
    lsl::stream_info get_lsl_outlet_electrode_quality_stream_info() override;

    EmotivData decode_data(const std::vector<uint8_t>& data) override;
    bool validate_data(const std::vector<uint8_t>& data) override;

private:
    std::vector<double> decode_motion_data(const std::vector<uint8_t>& data);
};
