/* Device skwoop_net: HTTPS GET on esp_http_client (esp-tls/mbedTLS, cert
 * bundle), WSS on esp_websocket_client. Same API as host skwoop_net.c. */
#include "skwoop_net.h"
#include "skwoop_config.h"
#include <errno.h>
#include <string.h>
#include "esp_http_client.h"
#include "esp_crt_bundle.h"
#include "esp_websocket_client.h"

int skwoop_http_get(const char *url, char *buf, size_t buflen, size_t *out_n) {
    if (out_n) *out_n = 0;
    if (!url || !buf || buflen < 2 || !out_n || strncmp(url, "https://", 8)) return -EINVAL;
    buf[0] = 0;
    esp_http_client_config_t c = { .url = url, .crt_bundle_attach = esp_crt_bundle_attach, .timeout_ms = 10000 };
    esp_http_client_handle_t h = esp_http_client_init(&c);
    if (!h) return -ENOMEM;
    int rc = -EIO;
    if (esp_http_client_open(h, 0) == ESP_OK) {
        esp_http_client_fetch_headers(h);
        size_t got = 0; int r;
        while (got < buflen - 1 && (r = esp_http_client_read(h, buf + got, buflen - 1 - got)) > 0) got += r;
        buf[got] = 0; *out_n = got;
        rc = esp_http_client_get_status_code(h);
    }
    esp_http_client_cleanup(h);
    return rc;
}

static esp_websocket_client_handle_t s_ws;
int skwoop_wss_connect(const char *url) {
    if (!url || strncmp(url, "wss://", 6)) return -EINVAL;
    esp_websocket_client_config_t c = { .uri = url, .crt_bundle_attach = esp_crt_bundle_attach };
    s_ws = esp_websocket_client_init(&c);
    if (!s_ws) return -ENOMEM;
    return esp_websocket_client_start(s_ws) == ESP_OK ? 0 : -EIO;
}
int skwoop_wss_send_text(const char *s) {
    return s_ws ? esp_websocket_client_send_text(s_ws, s, strlen(s), 1000) : -ENOTCONN;
}

int skwoop_net_provider_base(char *buf, size_t buflen) {
    if (!buf || !buflen) return -EINVAL;
    buf[0] = 0;
    if (skwoop_config_get_provider_base(buf, buflen) != 0) { buf[0] = 0; return -ENOENT; }
    return 0;
}
