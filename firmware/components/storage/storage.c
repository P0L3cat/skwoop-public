#include "storage.h"

#include "esp_log.h"
#include "esp_partition.h"
#include "esp_vfs_fat.h"
#include "nvs_flash.h"

#include <stdbool.h>

#define STORAGE_NVS_KEYS_LABEL "nvs_keys"
#define STORAGE_PART_LABEL "storage"
#define STORAGE_MOUNT_PATH "/storage"
#define STORAGE_MAX_FILES 4

static const char *TAG = "storage";

static wl_handle_t s_wl = WL_INVALID_HANDLE;

static esp_err_t storage_nvs_cfg(nvs_sec_cfg_t *cfg)
{
    const esp_partition_t *keys = esp_partition_find_first(
        ESP_PARTITION_TYPE_DATA,
        ESP_PARTITION_SUBTYPE_DATA_NVS_KEYS,
        STORAGE_NVS_KEYS_LABEL);
    esp_err_t err;

    if (keys == NULL) {
        ESP_LOGE(TAG, "nvs_keys partition missing");

        return ESP_ERR_NOT_FOUND;
    }

    err = nvs_flash_read_security_cfg(keys, cfg);

    if (err == ESP_ERR_NVS_KEYS_NOT_INITIALIZED || err == ESP_ERR_NVS_CORRUPT_KEY_PART) {
        ESP_LOGW(TAG, "generating nvs keys (%s)", esp_err_to_name(err));
        err = nvs_flash_generate_keys(keys, cfg);
    }

    return err;
}

static esp_err_t storage_nvs_init(void)
{
    nvs_sec_cfg_t cfg = {0};
    esp_err_t err = storage_nvs_cfg(&cfg);

    if (err != ESP_OK) {
        return err;
    }

    err = nvs_flash_secure_init(&cfg);

    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_LOGW(TAG, "erasing nvs (%s)", esp_err_to_name(err));
        err = nvs_flash_erase();

        if (err == ESP_OK) {
            err = nvs_flash_secure_init(&cfg);
        }
    }

    return err;
}

static esp_err_t storage_mount_fat(void)
{
    const esp_vfs_fat_mount_config_t mount = {
        .format_if_mount_failed = true,
        .max_files = STORAGE_MAX_FILES,
        .allocation_unit_size = 0,
        .disk_status_check_enable = false,
        .use_one_fat = false,
    };
    esp_err_t err = esp_vfs_fat_spiflash_mount_rw_wl(
        STORAGE_MOUNT_PATH, STORAGE_PART_LABEL, &mount, &s_wl);

    if (err != ESP_OK) {
        ESP_LOGE(TAG, "fat mount failed: %s", esp_err_to_name(err));

        return err;
    }

    if (s_wl == WL_INVALID_HANDLE) {
        ESP_LOGE(TAG, "wear levelling handle missing");

        return ESP_ERR_INVALID_STATE;
    }

    ESP_LOGI(TAG, "mounted %s", STORAGE_MOUNT_PATH);

    return ESP_OK;
}

esp_err_t storage_init(void)
{
    esp_err_t err = storage_nvs_init();

    if (err != ESP_OK) {
        return err;
    }

    return storage_mount_fat();
}
