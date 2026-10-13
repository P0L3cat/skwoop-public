.PHONY: all

all:
	mkdir -p build
	cc -Wall -Wextra -Werror -I../../../firmware/components/net ../../../firmware/components/net/prov_form.c test_prov_form.c -o build/t && ./build/t
