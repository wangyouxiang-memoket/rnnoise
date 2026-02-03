cmake_minimum_required(VERSION 3.16)
project(rnnoise LANGUAGES C)

option(RNNOISE_BUILD_SHARED "Build shared library" ON)
option(RNNOISE_BUILD_STATIC "Build static library" ON)
option(RNNOISE_BUILD_EXAMPLES "Build example programs" ON)
option(RNNOISE_BUILD_TOOLS "Build auxiliary tools" ON)
option(RNNOISE_ENABLE_X86_RTCD "Enable x86 runtime CPU detection" ON)
option(RNNOISE_USE_WEIGHTS_FILE "Build with external model loading" OFF)

set(RNNOISE_SOURCES
  src/denoise.c
  src/rnn.c
  src/pitch.c
  src/kiss_fft.c
  src/celt_lpc.c
  src/nnet.c
  src/nnet_default.c
  src/parse_lpcnet_weights.c
  src/rnnoise_tables.c
)

set(RNNOISE_DATA_C ${CMAKE_CURRENT_SOURCE_DIR}/src/rnnoise_data.c)
set(RNNOISE_DATA_H ${CMAKE_CURRENT_SOURCE_DIR}/src/rnnoise_data.h)

if(NOT EXISTS ${RNNOISE_DATA_H})
  message(FATAL_ERROR "Missing src/rnnoise_data.h. Generate model files and copy them into src/. See README for details.")
endif()

if(EXISTS ${RNNOISE_DATA_C})
  list(APPEND RNNOISE_SOURCES ${RNNOISE_DATA_C})
elseif(NOT RNNOISE_USE_WEIGHTS_FILE)
  message(FATAL_ERROR "Missing src/rnnoise_data.c. Enable RNNOISE_USE_WEIGHTS_FILE=ON or add rnnoise_data.c.")
endif()

set(RNNOISE_X86_SOURCES
  src/x86/x86_dnn_map.c
  src/x86/x86cpu.c
  src/x86/nnet_sse4_1.c
  src/x86/nnet_avx2.c
)

set(RNNOISE_PUBLIC_HEADERS
  include/rnnoise.h
)

set(CMAKE_C_STANDARD 99)
set(CMAKE_C_STANDARD_REQUIRED ON)
set(CMAKE_POSITION_INDEPENDENT_CODE ON)

if(RNNOISE_ENABLE_X86_RTCD AND (CMAKE_SYSTEM_PROCESSOR MATCHES "x86_64|AMD64|i.86"))
  list(APPEND RNNOISE_SOURCES ${RNNOISE_X86_SOURCES})
  add_compile_definitions(RNN_ENABLE_X86_RTCD CPU_INFO_BY_ASM)
  set_source_files_properties(src/x86/nnet_sse4_1.c PROPERTIES COMPILE_OPTIONS "-msse4.1")
  set_source_files_properties(src/x86/nnet_avx2.c PROPERTIES COMPILE_OPTIONS "-mavx;-mfma;-mavx2")
endif()

add_library(rnnoise_obj OBJECT ${RNNOISE_SOURCES})

target_compile_definitions(rnnoise_obj PRIVATE RNNOISE_BUILD)
if(RNNOISE_USE_WEIGHTS_FILE)
  target_compile_definitions(rnnoise_obj PRIVATE USE_WEIGHTS_FILE)
endif()

target_include_directories(rnnoise_obj PUBLIC
  $<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/include>
  $<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/src>
  $<INSTALL_INTERFACE:include>
)

find_library(M_LIB m)

if(RNNOISE_BUILD_SHARED)
  add_library(rnnoise_shared SHARED $<TARGET_OBJECTS:rnnoise_obj>)
  set_target_properties(rnnoise_shared PROPERTIES OUTPUT_NAME rnnoise)
  if(M_LIB)
    target_link_libraries(rnnoise_shared PRIVATE ${M_LIB})
  endif()
endif()

if(RNNOISE_BUILD_STATIC)
  add_library(rnnoise_static STATIC $<TARGET_OBJECTS:rnnoise_obj>)
  set_target_properties(rnnoise_static PROPERTIES OUTPUT_NAME rnnoise)
  if(M_LIB)
    target_link_libraries(rnnoise_static PRIVATE ${M_LIB})
  endif()
endif()

if(RNNOISE_BUILD_EXAMPLES)
  add_executable(rnnoise_demo examples/rnnoise_demo.c)
  target_include_directories(rnnoise_demo PRIVATE ${CMAKE_CURRENT_SOURCE_DIR}/include)
  if(RNNOISE_BUILD_SHARED)
    target_link_libraries(rnnoise_demo PRIVATE rnnoise_shared)
  else()
    target_link_libraries(rnnoise_demo PRIVATE rnnoise_static)
  endif()
  add_executable(rnnoise_wrapper_demo examples/rnnoise_wrapper_demo.c)
  target_include_directories(rnnoise_wrapper_demo PRIVATE ${CMAKE_CURRENT_SOURCE_DIR}/include)
  if(RNNOISE_BUILD_SHARED)
    target_link_libraries(rnnoise_wrapper_demo PRIVATE rnnoise_shared)
  else()
    target_link_libraries(rnnoise_wrapper_demo PRIVATE rnnoise_static)
  endif()
endif()

if(RNNOISE_BUILD_TOOLS)
  add_executable(dump_features
    src/dump_features.c
    src/denoise.c
    src/pitch.c
    src/celt_lpc.c
    src/kiss_fft.c
    src/parse_lpcnet_weights.c
    src/rnnoise_tables.c
  )
  target_compile_definitions(dump_features PRIVATE TRAINING)
  target_include_directories(dump_features PRIVATE ${CMAKE_CURRENT_SOURCE_DIR}/include ${CMAKE_CURRENT_SOURCE_DIR}/src)
  if(M_LIB)
    target_link_libraries(dump_features PRIVATE ${M_LIB})
  endif()

  add_executable(dump_weights_blob src/write_weights.c)
  target_compile_definitions(dump_weights_blob PRIVATE DUMP_BINARY_WEIGHTS)
  target_include_directories(dump_weights_blob PRIVATE ${CMAKE_CURRENT_SOURCE_DIR}/include ${CMAKE_CURRENT_SOURCE_DIR}/src)
  if(M_LIB)
    target_link_libraries(dump_weights_blob PRIVATE ${M_LIB})
  endif()
endif()

install(FILES ${RNNOISE_PUBLIC_HEADERS} DESTINATION include)
if(RNNOISE_BUILD_SHARED)
  install(TARGETS rnnoise_shared LIBRARY DESTINATION lib ARCHIVE DESTINATION lib RUNTIME DESTINATION bin)
endif()
if(RNNOISE_BUILD_STATIC)
  install(TARGETS rnnoise_static ARCHIVE DESTINATION lib)
endif()
