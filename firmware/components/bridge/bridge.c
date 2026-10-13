#include "bridge.h"

#include <stdio.h>
#include <string.h>

#include "esp_event.h"
#include "esp_http_client.h"
#include "esp_log.h"
#include "esp_websocket_client.h"
#include "esp_wifi.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "sdkconfig.h"

enum {
    BRIDGE_TASK_STACK = 8192,
    BRIDGE_TASK_PRIO = 5,
    BRIDGE_WS_STACK = 8192,
    BRIDGE_WS_BUFFER = 4096,
    BRIDGE_OTA_CAP = 2048,
    BRIDGE_RX_CAP = 4096,
    BRIDGE_BACKOFF_MAX_S = 30,
    BRIDGE_HTTP_TIMEOUT_MS = 10000,
    BRIDGE_SEND_TIMEOUT_MS = 2000,
    BRIDGE_WS_OP_TEXT = 0x1,
    BRIDGE_WS_OP_BINARY = 0x2,
};

static const char *TAG = "bridge";

static bridge_callbacks_t s_callbacks;

static esp_websocket_client_handle_t s_ws;

static bool s_started;

static char s_sta_mac[18];

static char s_ws_headers[256];

/* Same bound as bridge_ota_t.ws_url. */
static char s_ws_url[160];

static char s_ota_body[BRIDGE_OTA_CAP];

static char s_text_rx[BRIDGE_RX_CAP];

static int s_text_filled;

static int s_text_total;

static bool s_text_drop;

static int bridge_next_backoff_s(int backoff_s)
{
    if (backoff_s >= BRIDGE_BACKOFF_MAX_S) {
        return BRIDGE_BACKOFF_MAX_S;
    }
    int next_s = backoff_s * 2;
    if (next_s > BRIDGE_BACKOFF_MAX_S) {
        return BRIDGE_BACKOFF_MAX_S;
    }
    return next_s;
}

static bool bridge_format_sta_mac(void)
{
    uint8_t mac[6];
    if (esp_wifi_get_mac(WIFI_IF_STA, mac) != ESP_OK) {
        return false;
    }
    int wrote = snprintf(s_sta_mac, sizeof(s_sta_mac), "%02x:%02x:%02x:%02x:%02x:%02x",
                         mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);
    return wrote == (int)sizeof(s_sta_mac) - 1;
}

static bool bridge_format_ota_url(char *url, size_t cap)
{
    const char *host = CONFIG_SKWOOP_BRIDGE_HOST;
    int wrote;

    if (host[0] == '\0') {
        return false;
    }

    wrote = snprintf(url, cap, "http://%s:%d/xiaozhi/ota/", host, CONFIG_SKWOOP_BRIDGE_PORT);
    return wrote > 0 && (size_t)wrote < cap;
}

static int bridge_read_ota_body(esp_http_client_handle_t http)
{
    int filled = 0;
    while (filled < BRIDGE_OTA_CAP) {
        int nread = esp_http_client_read(http, s_ota_body + filled, BRIDGE_OTA_CAP - filled);
        if (nread < 0) {
            return -1;
        }
        if (nread == 0) {
            break;
        }
        filled += nread;
    }
    return filled;
}

static bool bridge_fetch_ota(bridge_ota_t *ota)
{
    char url[192];
    if (!bridge_format_sta_mac() || !bridge_format_ota_url(url, sizeof(url))) {
        return false;
    }

    esp_http_client_config_t http_cfg = {
        .url = url,
        .method = HTTP_METHOD_POST,
        .timeout_ms = BRIDGE_HTTP_TIMEOUT_MS,
    };
    esp_http_client_handle_t http = esp_http_client_init(&http_cfg);
    if (http == NULL) {
        return false;
    }

    bool got_ota = false;
    if (esp_http_client_set_header(http, "Device-Id", s_sta_mac) == ESP_OK &&
        esp_http_client_set_header(http, "Content-Type", "application/json") == ESP_OK &&
        esp_http_client_open(http, 2) == ESP_OK) {
        if (esp_http_client_write(http, "{}", 2) == 2) {
            (void)esp_http_client_fetch_headers(http);
            if (esp_http_client_get_status_code(http) == 200) {
                int body_len = bridge_read_ota_body(http);
                if (body_len >= 0 && bridge_parse_ota(s_ota_body, (size_t)body_len, ota) == 0) {
                    got_ota = true;
                }
            }
        }
        esp_http_client_close(http);
    }
    esp_http_client_cleanup(http);
    return got_ota;
}

static bool bridge_format_ws_headers(const bridge_ota_t *ota)
{
    int wrote;
    if (ota->token[0] != '\0') {
        wrote = snprintf(s_ws_headers, sizeof(s_ws_headers),
                         "Authorization: Bearer %s\r\n"
                         "Device-Id: %s\r\n"
                         "Protocol-Version: 1\r\n",
                         ota->token, s_sta_mac);
    } else {
        wrote = snprintf(s_ws_headers, sizeof(s_ws_headers),
                         "Device-Id: %s\r\n"
                         "Protocol-Version: 1\r\n",
                         s_sta_mac);
    }
    return wrote > 0 && (size_t)wrote < sizeof(s_ws_headers);
}

static bool bridge_copy_ws_url(const bridge_ota_t *ota)
{
    size_t url_len = strnlen(ota->ws_url, sizeof(ota->ws_url));
    if (url_len == 0 || url_len >= sizeof(s_ws_url)) {
        return false;
    }
    memcpy(s_ws_url, ota->ws_url, url_len + 1);
    return true;
}

static void bridge_send_hello(esp_websocket_client_handle_t ws)
{
    char hello[512];
    size_t hello_len = bridge_build_hello(hello, sizeof(hello));
    s_text_filled = 0;
    s_text_total = 0;
    s_text_drop = false;
    if (ws == NULL || hello_len == 0 || hello_len > 0x7fffffffU) {
        return;
    }
    (void)esp_websocket_client_send_text(ws, hello, (int)hello_len, pdMS_TO_TICKS(BRIDGE_SEND_TIMEOUT_MS));
}

static void bridge_deliver_audio(const esp_websocket_event_data_t *frame)
{
    if (s_callbacks.on_audio == NULL || frame->data_ptr == NULL || frame->data_len <= 0) {
        return;
    }
    /* SAFETY: WEBSOCKET_EVENT_DATA data_ptr addresses the binary payload bytes. */
    const uint8_t *audio = (const uint8_t *)frame->data_ptr;
    s_callbacks.on_audio(audio, (size_t)frame->data_len);
}

static void bridge_reassemble_text(const esp_websocket_event_data_t *frame)
{
    if (frame->data_len < 0 || frame->payload_len < 0 || frame->payload_offset < 0) {
        s_text_drop = true;
        return;
    }
    if (frame->payload_offset == 0) {
        s_text_filled = 0;
        s_text_total = frame->payload_len;
        s_text_drop = frame->payload_len > BRIDGE_RX_CAP;
    }
    if (s_text_drop || frame->payload_len > BRIDGE_RX_CAP || frame->payload_len != s_text_total) {
        s_text_drop = true;
        return;
    }
    if (frame->payload_offset != s_text_filled || frame->data_len > BRIDGE_RX_CAP - s_text_filled) {
        s_text_drop = true;
        return;
    }
    if (frame->data_len > 0 && frame->data_ptr == NULL) {
        s_text_drop = true;
        return;
    }
    if (frame->data_len > 0) {
        /* SAFETY: data_ptr addresses this text chunk; bounds checked against BRIDGE_RX_CAP. */
        memcpy(s_text_rx + s_text_filled, frame->data_ptr, (size_t)frame->data_len);
        s_text_filled += frame->data_len;
    }
    if (s_text_filled != s_text_total) {
        return;
    }

    bridge_msg_t msg;
    if (bridge_parse_server(s_text_rx, (size_t)s_text_filled, &msg) == 0 &&
        s_callbacks.on_msg != NULL) {
        s_callbacks.on_msg(&msg);
    }
}

static void bridge_on_ws_event(void *handler_args, esp_event_base_t base, int32_t event_id, void *event_data)
{
    (void)base;

    if (event_id == WEBSOCKET_EVENT_CONNECTED) {
        /* SAFETY: events are registered with the websocket client handle. */
        bridge_send_hello((esp_websocket_client_handle_t)handler_args);
        return;
    }
    if (event_id != WEBSOCKET_EVENT_DATA || event_data == NULL) {
        return;
    }

    /* SAFETY: WEBSOCKET_EVENT_DATA carries esp_websocket_event_data_t. */
    const esp_websocket_event_data_t *frame = (const esp_websocket_event_data_t *)event_data;
    if (frame->op_code == BRIDGE_WS_OP_TEXT) {
        bridge_reassemble_text(frame);
        return;
    }
    if (frame->op_code == BRIDGE_WS_OP_BINARY) {
        bridge_deliver_audio(frame);
    }
}

static bool bridge_open_ws(const bridge_ota_t *ota)
{
    if (!bridge_format_ws_headers(ota) || !bridge_copy_ws_url(ota)) {
        return false;
    }

    esp_websocket_client_config_t ws_cfg = {
        .uri = s_ws_url,
        .headers = s_ws_headers,
        .buffer_size = BRIDGE_WS_BUFFER,
        .task_stack = BRIDGE_WS_STACK,
        .disable_auto_reconnect = false,
        .network_timeout_ms = BRIDGE_HTTP_TIMEOUT_MS,
    };
    esp_websocket_client_handle_t ws = esp_websocket_client_init(&ws_cfg);
    if (ws == NULL) {
        return false;
    }
    if (esp_websocket_register_events(ws, WEBSOCKET_EVENT_ANY, bridge_on_ws_event, ws) != ESP_OK) {
        (void)esp_websocket_client_destroy(ws);
        return false;
    }
    s_ws = ws;
    if (esp_websocket_client_start(ws) != ESP_OK) {
        s_ws = NULL;
        (void)esp_websocket_client_destroy(ws);
        return false;
    }
    return true;
}

static void bridge_task(void *arg)
{
    (void)arg;
    int backoff_s = 1;

    for (;;) {
        bridge_ota_t ota;
        memset(&ota, 0, sizeof(ota));
        if (!bridge_fetch_ota(&ota)) {
            ESP_LOGW(TAG, "ota check failed, retry in %d s", backoff_s);
            vTaskDelay(pdMS_TO_TICKS((uint32_t)backoff_s * 1000U));
            backoff_s = bridge_next_backoff_s(backoff_s);
            continue;
        }
        if (!bridge_open_ws(&ota)) {
            ESP_LOGW(TAG, "websocket start failed, retry in %d s", backoff_s);
            vTaskDelay(pdMS_TO_TICKS((uint32_t)backoff_s * 1000U));
            backoff_s = bridge_next_backoff_s(backoff_s);
            continue;
        }
        break;
    }

    vTaskDelete(NULL);
}

esp_err_t bridge_start(const bridge_callbacks_t *callbacks)
{
    if (callbacks == NULL) {
        return ESP_ERR_INVALID_ARG;
    }
    if (s_started) {
        return ESP_ERR_INVALID_STATE;
    }
    s_callbacks = *callbacks;
    if (xTaskCreate(bridge_task, "bridge", BRIDGE_TASK_STACK, NULL, BRIDGE_TASK_PRIO, NULL) != pdPASS) {
        memset(&s_callbacks, 0, sizeof(s_callbacks));
        return ESP_ERR_NO_MEM;
    }
    s_started = true;
    return ESP_OK;
}

bool bridge_send_audio(const uint8_t *data, size_t len)
{
    if (data == NULL || len == 0 || len > 0x7fffffffU || s_ws == NULL || !esp_websocket_client_is_connected(s_ws)) {
        return false;
    }
    /* SAFETY: esp_websocket_client_send_bin takes the payload as char bytes. */
    int sent = esp_websocket_client_send_bin(s_ws, (const char *)data, (int)len, pdMS_TO_TICKS(BRIDGE_SEND_TIMEOUT_MS));
    return sent > 0;
}

bool bridge_send_text(const char *json)
{
    if (json == NULL || s_ws == NULL || !esp_websocket_client_is_connected(s_ws)) {
        return false;
    }
    size_t len = strlen(json);
    if (len == 0 || len > 0x7fffffffU) {
        return false;
    }
    int sent = esp_websocket_client_send_text(s_ws, json, (int)len, pdMS_TO_TICKS(BRIDGE_SEND_TIMEOUT_MS));
    return sent > 0;
}
