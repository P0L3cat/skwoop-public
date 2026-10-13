idf_component_register(
    SRCS "mqtt_codec.c" "mqtt_url.c" "mqtt_lite.c"
    INCLUDE_DIRS "."
    PRIV_REQUIRES esp-tls mbedtls
)

target_compile_options(${COMPONENT_LIB} PRIVATE -Wall -Wextra -Werror)
