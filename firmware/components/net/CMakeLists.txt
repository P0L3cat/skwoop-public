idf_component_register(
    SRCS "wifi.c" "prov_form.c"
    INCLUDE_DIRS "."
    REQUIRES esp_wifi esp_netif esp_event esp_http_server esp_timer skwoop
)

target_compile_options(${COMPONENT_LIB} PRIVATE -Wall -Wextra -Werror)
