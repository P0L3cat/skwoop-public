idf_component_register(
    SRCS "timers.c" "alarm_clock.c"
    INCLUDE_DIRS "."
    PRIV_REQUIRES nvs_flash esp_netif esp_timer lwip
)

target_compile_options(${COMPONENT_LIB} PRIVATE -Wall -Wextra -Werror)
