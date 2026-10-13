.PHONY: all

IDF := $(IDF_PATH)
ifeq ($(IDF),)
IDF := $(HOME)/esp/esp-idf
endif
CJSON := $(IDF)/components/json/cJSON

all:
	mkdir -p build
	cc -O2 -Wall -Wextra -Werror -I../../../firmware/components/cloud -I$(CJSON) \
		../../../firmware/components/cloud/cloud_proto.c $(CJSON)/cJSON.c test_cloud_proto.c \
		-o build/t && ./build/t
