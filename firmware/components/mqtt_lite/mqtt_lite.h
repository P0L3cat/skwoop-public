#pragma once

/* Lean MQTT 3.1.1 client: one broker, QoS 0 publishes from a queue,
 * keepalive pings, reconnect with backoff. mqtts:// uses TLS with the
 * firmware cert bundle; mqtt:// is plain TCP (LAN brokers). */

#include <stdbool.h>

#include "esp_err.h"

typedef struct {
    const char *url;
    const char *client_id;
    const char *username; /* NULL or "": none */
    const char *password; /* NULL or "": none */
} mqtt_lite_config_t;

/* Copies the config and starts the client task. */
esp_err_t mqtt_lite_start(const mqtt_lite_config_t *cfg);

/* Queue a QoS 0 publish. false when not started or the queue is full. */
bool mqtt_lite_publish(const char *topic, const char *payload, bool retain);
