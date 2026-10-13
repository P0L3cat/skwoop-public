idf_component_register(
    SRCS "bridge_msg.c" "bridge.c"
    INCLUDE_DIRS "."
    REQUIRES json esp_http_client esp_wifi
    PRIV_REQUIRES esp_websocket_client
)

target_compile_options(${COMPONENT_LIB} PRIVATE -Wall -Wextra -Werror)
