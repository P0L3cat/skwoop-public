# mood_v11.bin is generated (CC-BY-SA weights) and is not in the tree.
# tools/export_mood.py writes it. A missing file leaves mood disabled.
set(mood_embed_args "")

if(EXISTS "${CMAKE_CURRENT_LIST_DIR}/mood_v11.bin")
    set(mood_embed_args EMBED_FILES "mood_v11.bin")
else()
    if(NOT CMAKE_BUILD_EARLY_EXPANSION)
        message(WARNING
            "Skwoop mood: components/mood/mood_v11.bin is missing, so mood is disabled. "
            "Generate it with tools/export_mood.py (weights are not shipped).")
    endif()
endif()

idf_component_register(
    SRCS "mood.c" "mood_builtin.c"
    INCLUDE_DIRS "."
    REQUIRES log
    ${mood_embed_args}
)

if(EXISTS "${CMAKE_CURRENT_LIST_DIR}/mood_v11.bin")
    target_compile_definitions(${COMPONENT_LIB} PRIVATE MOOD_WEIGHTS_EMBEDDED=1)
    if(NOT CMAKE_BUILD_EARLY_EXPANSION)
        message(STATUS "Skwoop mood: embedding components/mood/mood_v11.bin")
    endif()
endif()

target_compile_options(${COMPONENT_LIB} PRIVATE -Wall -Wextra -Werror)
