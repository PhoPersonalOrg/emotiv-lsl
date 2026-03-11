cmake_minimum_required(VERSION 3.14)
project(emotiv_lsl_cpp VERSION 0.1.0 LANGUAGES C CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

include(FetchContent)

# 1. Fetch hidapi
FetchContent_Declare(
    hidapi
    GIT_REPOSITORY https://github.com/libusb/hidapi.git
    GIT_TAG        hidapi-0.14.0
)
FetchContent_MakeAvailable(hidapi)

# 2. Fetch liblsl
FetchContent_Declare(
    liblsl
    GIT_REPOSITORY https://github.com/sccn/liblsl.git
    GIT_TAG        v1.16.2
)
set(LSL_BUILD_EXAMPLES OFF CACHE BOOL "Disable liblsl examples" FORCE)
set(LSL_BUILD_TESTING OFF CACHE BOOL "Disable liblsl tests" FORCE)
set(LSL_UNITTESTS OFF CACHE BOOL "Disable liblsl tests" FORCE)
FetchContent_MakeAvailable(liblsl)

# 3. Fetch tiny-AES-c
FetchContent_Declare(
    tiny_aes
    GIT_REPOSITORY https://github.com/kokke/tiny-AES-c.git
    GIT_TAG        master
)
FetchContent_GetProperties(tiny_aes)
if(NOT tiny_aes_POPULATED)
    FetchContent_Populate(tiny_aes)
endif()

file(GLOB SOURCES "src/*.cpp")
add_executable(${PROJECT_NAME} ${SOURCES} "${tiny_aes_SOURCE_DIR}/aes.c")

target_include_directories(${PROJECT_NAME} PRIVATE
    src
    ${tiny_aes_SOURCE_DIR}
)

# tiny-AES-c options: only need ECB
target_compile_definitions(${PROJECT_NAME} PRIVATE
    CBC=0
    CTR=0
    ECB=1
)

if(WIN32)
    target_link_libraries(${PROJECT_NAME} PRIVATE hidapi_winapi lsl)
    
    # Copy DLLs to the output directory on Windows
    add_custom_command(TARGET ${PROJECT_NAME} POST_BUILD
        COMMAND ${CMAKE_COMMAND} -E copy_if_different
        $<TARGET_FILE:hidapi_winapi>
        $<TARGET_FILE_DIR:${PROJECT_NAME}>
    )
    add_custom_command(TARGET ${PROJECT_NAME} POST_BUILD
        COMMAND ${CMAKE_COMMAND} -E copy_if_different
        $<TARGET_FILE:lsl>
        $<TARGET_FILE_DIR:${PROJECT_NAME}>
    )
elseif(APPLE)
    target_link_libraries(${PROJECT_NAME} PRIVATE hidapi_darwin lsl)
else()
    target_link_libraries(${PROJECT_NAME} PRIVATE hidapi_libusb lsl)
endif()
