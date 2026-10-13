#pragma once

#include "esp_err.h"

typedef enum {
    WIFI_CONNECTING,
    WIFI_GOT_IP,
    WIFI_FAILED,
    WIFI_AP_MODE,
} wifi_state_t;

/* Called on the default event loop task after wifi_start returns. Must not block. */
typedef void (*wifi_state_cb)(wifi_state_t);

/* Load saved STA credentials and connect. No credentials: cb(WIFI_FAILED), ESP_OK.
 * AP mode is not started here. */
esp_err_t wifi_start(wifi_state_cb cb);
