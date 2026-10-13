#ifndef SKWOOP_NET_H
#define SKWOOP_NET_H

#include <stddef.h>

/*
 * Host-side HTTPS GET.
 *
 * On success returns the HTTP status code (>0), writes the response body into
 * buf (NUL-terminated when buflen > 0), and sets *out_n to the number of body
 * bytes stored, not counting the NUL. A short buffer still returns the status;
 * the body is truncated and still NUL-terminated.
 *
 * On failure returns a negative errno-style code and sets *out_n to 0 when
 * out_n is non-NULL. Does not call paid STT/TTS APIs.
 */
int skwoop_http_get(const char *url, char *buf, size_t buflen, size_t *out_n);

/*
 * WebSocket secure connect. Host stub only.
 *
 * ESP-IDF esp_websocket_client will own the device path later. This host
 * build does not open a socket. Returns -ENOSYS.
 */
int skwoop_wss_connect(const char *url);

/*
 * Copy the provider base URL from skwoop_config into buf.
 * Returns 0 on success. Returns a negative errno-style code when config is
 * missing or the provider base URL is empty (clear error, no network I/O).
 */
int skwoop_net_provider_base(char *buf, size_t buflen);

#endif
