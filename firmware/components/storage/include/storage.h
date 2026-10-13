#pragma once

#include "esp_err.h"

/* Secure-init NVS from the nvs_keys partition, then mount the FAT "storage" partition at /storage. */
esp_err_t storage_init(void);
