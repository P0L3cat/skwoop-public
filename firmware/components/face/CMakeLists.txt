idf_component_register(
    SRCS "face.c" "face_geom.c"
    INCLUDE_DIRS "include"
    REQUIRES lvgl__lvgl log
    PRIV_REQUIRES espressif__esp_lvgl_port esp_hw_support
)

target_compile_options(${COMPONENT_LIB} PRIVATE -Wall -Wextra -Werror)
