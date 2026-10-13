#include "mqtt_url.h"

#include <string.h>

int mqtt_url_parse(const char *url, mqtt_url_t *out)
{
    const char *host;
    const char *colon;
    size_t host_len;
    uint32_t port = 0;

    if (url == NULL || out == NULL) {
        return -1;
    }

    if (strncmp(url, "mqtts://", 8) == 0) {
        out->tls = true;
        host = url + 8;
        out->port = 8883;
    } else if (strncmp(url, "mqtt://", 7) == 0) {
        out->tls = false;
        host = url + 7;
        out->port = 1883;
    } else {
        return -1;
    }

    colon = strchr(host, ':');
    host_len = colon != NULL ? (size_t)(colon - host) : strlen(host);

    if (host_len == 0 || host_len >= sizeof out->host || strpbrk(host, "/@?#") != NULL) {
        return -1;
    }

    if (colon != NULL) {
        const char *p = colon + 1;

        if (*p == '\0') {
            return -1;
        }

        for (; *p != '\0'; p++) {
            if (*p < '0' || *p > '9' || (port = port * 10u + (uint32_t)(*p - '0')) > 65535u) {
                return -1;
            }
        }

        if (port == 0) {
            return -1;
        }

        out->port = (uint16_t)port;
    }

    memcpy(out->host, host, host_len);
    out->host[host_len] = '\0';

    return 0;
}
