idf_component_register(
    SRCS "storage.c"
    INCLUDE_DIRS "include"
    REQUIRES nvs_flash esp_partition fatfs wear_levelling log
)

target_compile_options(${COMPONENT_LIB} PRIVATE -Wall -Wextra -Werror)
