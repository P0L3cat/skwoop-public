idf_component_register(
    SRCS "resample.c" "endpoint.c" "wav.c" "wake_pick.c" "opus_io.c" "voice.c" "wake.c"
    INCLUDE_DIRS "."
    REQUIRES opus espressif__esp-sr board bridge cmds
)

target_compile_options(${COMPONENT_LIB} PRIVATE -Wall -Wextra -Werror)
