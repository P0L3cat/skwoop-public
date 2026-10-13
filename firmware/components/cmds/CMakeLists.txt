idf_component_register(
    SRCS "cmds.c"
    INCLUDE_DIRS "."
)

target_compile_options(${COMPONENT_LIB} PRIVATE -Wall -Wextra -Werror)
