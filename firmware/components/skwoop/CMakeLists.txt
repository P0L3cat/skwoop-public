set(skwoop_srcs "src/skwoop_config.c" "shims/skwoop_config_nvs.c")

# Requirements are captured before sdkconfig exists (early expansion), so the
# standalone network libs are listed even when CONFIG_SKWOOP_STANDALONE is off.
# shims/skwoop_net_idf.c defines skwoop_http_get and replaces src/skwoop_net.c.
set(skwoop_priv_reqs nvs_flash nvs_sec_provider mbedtls)
if(CONFIG_SKWOOP_STANDALONE OR CMAKE_BUILD_EARLY_EXPANSION)
    list(APPEND skwoop_priv_reqs esp_http_client esp-tls esp_websocket_client mbedtls lwip)
endif()

if(CONFIG_SKWOOP_STANDALONE)
    list(APPEND skwoop_srcs
        "src/skwoop_harness.c"
        "src/skwoop_memory.c"
        "src/skwoop_clips.c"
        "shims/skwoop_net_idf.c")
endif()

idf_component_register(
    SRCS ${skwoop_srcs}
    INCLUDE_DIRS "include"
    PRIV_REQUIRES ${skwoop_priv_reqs}
)

target_compile_options(${COMPONENT_LIB} PRIVATE -Wall -Wextra -Werror)
