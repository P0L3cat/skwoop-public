# cloud.c needs the standalone Kconfig strings; cloud_proto.c is pure.
set(cloud_srcs "cloud_proto.c")
if(CONFIG_SKWOOP_STANDALONE)
    list(APPEND cloud_srcs "cloud.c")
endif()

idf_component_register(
    SRCS ${cloud_srcs}
    INCLUDE_DIRS "."
    REQUIRES skwoop
    PRIV_REQUIRES json esp_http_client mbedtls
)

target_compile_options(${COMPONENT_LIB} PRIVATE -Wall -Wextra -Werror)
