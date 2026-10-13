idf_component_register(
    SRCS "board.c" "axp2101.c" "button.c"
    INCLUDE_DIRS "include"
    REQUIRES driver esp_lcd esp_timer freertos log
)

target_compile_options(${COMPONENT_LIB} PRIVATE -Wall -Wextra -Werror)
