.PHONY: all

IDF := $(IDF_PATH)
ifeq ($(IDF),)
IDF := $(HOME)/esp/esp-idf
endif
CJSON := $(IDF)/components/json/cJSON

all:
	mkdir -p build
	cc -Wall -Wextra -Werror -I../../../firmware/components/bridge -I$(CJSON) \
		../../../firmware/components/bridge/bridge_msg.c $(CJSON)/cJSON.c test_bridge_msg.c \
		-o build/t && ./build/t
