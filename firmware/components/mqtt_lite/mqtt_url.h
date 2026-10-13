#pragma once

/* mqtt://host[:port] or mqtts://host[:port] (no path, no credentials). */

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define MQTT_HOST_MAX 128

typedef struct {
    char host[MQTT_HOST_MAX];
    uint16_t port;
    bool tls;
} mqtt_url_t;

/* Default ports 1883 / 8883. Returns 0, or -1 on anything else. */
int mqtt_url_parse(const char *url, mqtt_url_t *out);
