#include <iostream>
#include "emotiv_epoc_x.h"

int main(int argc, char* argv[]) {
    try {
        bool enable_quality = true;
        bool enable_motion = true;

        std::cout << "Starting Emotiv LSL C++ Server..." << std::endl;
        EmotivEpocX epocX(enable_motion, enable_quality);
        
        epocX.main_loop();
    } catch (const std::exception& e) {
        std::cerr << "Error: " << e.what() << std::endl;
        return 1;
    }
    
    return 0;
}
