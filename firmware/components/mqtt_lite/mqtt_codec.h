#pragma once

/* MQTT 3.1.1 packet encode/decode: just what a pod publishing light commands
 * needs (CONNECT, PUBLISH QoS 0/1, SUBSCRIBE, PINGREQ, DISCONNECT out;
 * CONNACK, SUBACK, PUBLISH, PUBACK, PINGRESP in). Pure C, no allocation.
 * Encoders return bytes written, or -1 when out is too small or an argument
 * is out of range. */

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define MQTT_REMAINING_MAX 268435455u

enum {
    MQTT_CONNECT = 1,
    MQTT_CONNACK = 2,
    MQTT_PUBLISH = 3,
    MQTT_PUBACK = 4,
    MQTT_SUBSCRIBE = 8,
    MQTT_SUBACK = 9,
    MQTT_PINGREQ = 12,
    MQTT_PINGRESP = 13,
    MQTT_DISCONNECT = 14,
};

typedef struct {
    const char *client_id;
    const char *username; /* NULL: none */
    const char *password; /* NULL: none; needs a username */
    uint16_t keepalive_s;
    bool clean_session;
} mqtt_connect_t;

int mqtt_encode_connect(uint8_t *out, size_t cap, const mqtt_connect_t *c);

/* qos 0 or 1; packet_id is used (and must be nonzero) only for qos 1. */
int mqtt_encode_publish(uint8_t *out, size_t cap, const char *topic, const uint8_t *payload, size_t payload_len,
                        int qos, bool retain, uint16_t packet_id);

int mqtt_encode_subscribe(uint8_t *out, size_t cap, uint16_t packet_id, const char *topic, int qos);

int mqtt_encode_pingreq(uint8_t *out, size_t cap);

int mqtt_encode_disconnect(uint8_t *out, size_t cap);

/* Fixed header from the start of a stream. Returns header bytes (2..5) with
 * *type, *flags, *remaining set; 0 when more bytes are needed; -1 when the
 * remaining-length field is malformed. */
int mqtt_decode_header(const uint8_t *buf, size_t len, uint8_t *type, uint8_t *flags, uint32_t *remaining);

typedef struct {
    uint8_t type;
    bool session_present;   /* CONNACK */
    uint8_t return_code;    /* CONNACK; SUBACK granted QoS or 0x80 */
    uint16_t packet_id;     /* PUBACK, SUBACK, PUBLISH qos 1 */
    const char *topic;      /* PUBLISH: points into the packet, not NUL-terminated */
    uint16_t topic_len;
    const uint8_t *payload; /* PUBLISH */
    size_t payload_len;
    int qos;                /* PUBLISH */
} mqtt_packet_t;

/* Parse one complete packet (header + remaining bytes). -1 when malformed or
 * a type the pod does not accept. */
int mqtt_decode_packet(const uint8_t *buf, size_t len, mqtt_packet_t *out);
