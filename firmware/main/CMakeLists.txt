set(srcs "main.c")

if(CONFIG_SKWOOP_STANDALONE)
    list(APPEND srcs "standalone.c")
endif()

idf_component_register(
    SRCS ${srcs}
    REQUIRES storage board face net bridge audio skwoop cloud clipcache cmds timers mood mqtt_lite
)

target_compile_options(${COMPONENT_LIB} PRIVATE -Wall -Wextra -Werror)
