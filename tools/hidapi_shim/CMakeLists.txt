cmake_minimum_required(VERSION 3.16)
project(emotiv_hidapi_shim C)

add_library(hidapi SHARED shim_hidapi.c hidapi.h)
target_include_directories(hidapi PUBLIC ${CMAKE_CURRENT_SOURCE_DIR})
set_target_properties(hidapi PROPERTIES
  OUTPUT_NAME "hidapi"
  PREFIX ""
  WINDOWS_EXPORT_ALL_SYMBOLS OFF
)
if(MSVC)
  target_compile_definitions(hidapi PRIVATE _CRT_SECURE_NO_WARNINGS)
endif()

install(TARGETS hidapi RUNTIME DESTINATION bin LIBRARY DESTINATION lib)
