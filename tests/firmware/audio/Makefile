.PHONY: all

all:
	mkdir -p build
	cc -Wall -Wextra -Werror -I../../../firmware/components/audio ../../../firmware/components/audio/resample.c test_resample.c -lm -o build/t && ./build/t
