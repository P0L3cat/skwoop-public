cmake_minimum_required(VERSION 3.16)

include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(skwoop)

# Repack the esp-sr model image. models/wakenet and models/multinet are
# optional; empty directories do not fail the build. The flash target writes
# build/srmodels/srmodels.bin, so the repack runs before it.
idf_build_get_property(build_dir BUILD_DIR)
idf_build_get_property(python PYTHON)
partition_table_get_partition_info(model_size "--partition-name model" "size")
add_custom_target(skwoop_srmodels ALL
    COMMAND ${python} ${CMAKE_SOURCE_DIR}/tools/pack_srmodels.py
        --esp-sr ${CMAKE_SOURCE_DIR}/managed_components/espressif__esp-sr
        --base ${build_dir}/srmodels
        --extra ${CMAKE_SOURCE_DIR}/models/wakenet
        --extra ${CMAKE_SOURCE_DIR}/models/multinet
        --out ${build_dir}/srmodels/srmodels.bin
        --max-bytes ${model_size}
    DEPENDS srmodels_bin
    COMMENT "Packing esp-sr models + models/"
    VERBATIM)
add_dependencies(flash skwoop_srmodels)
