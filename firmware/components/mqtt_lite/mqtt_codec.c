#include "mqtt_codec.h"

#include <string.h>

typedef struct {
    uint8_t *out;
    size_t cap;
    size_t at;
    bool overflow;
} writer_t;

static void put_u8(writer_t *w, uint8_t v)
{
    if (w->at >= w->cap) {
        w->overflow = true;

        return;
    }

    w->out[w->at++] = v;
}

static void put_u16(writer_t *w, uint16_t v)
{
    put_u8(w, (uint8_t)(v >> 8));
    put_u8(w, (uint8_t)v);
}

static void put_bytes(writer_t *w, const void *p, size_t n)
{
    if (w->at + n > w->cap) {
        w->overflow = true;

        return;
    }

    memcpy(w->out + w->at, p, n);
    w->at += n;
}

static bool put_str(writer_t *w, const char *s)
{
    size_t n = strlen(s);

    if (n > UINT16_MAX) {
        return false;
    }

    put_u16(w, (uint16_t)n);
    put_bytes(w, s, n);

    return true;
}

static void put_varint(writer_t *w, uint32_t v)
{
    do {
        uint8_t b = v % 128u;

        v /= 128u;
        put_u8(w, (uint8_t)(v > 0 ? b | 0x80u : b));
    } while (v > 0);
}

/* First byte + remaining length; the body follows. */
static bool put_header(writer_t *w, uint8_t type, uint8_t flags, size_t remaining)
{
    if (remaining > MQTT_REMAINING_MAX) {
        return false;
    }

    put_u8(w, (uint8_t)(type << 4 | flags));
    put_varint(w, (uint32_t)remaining);

    return true;
}

static int finish(const writer_t *w)
{
    return w->overflow ? -1 : (int)w->at;
}

int mqtt_encode_connect(uint8_t *out, size_t cap, const mqtt_connect_t *c)
{
    writer_t w = { out, cap, 0, false };
    uint8_t flags = 0;
    size_t remaining;

    if (c == NULL || c->client_id == NULL || (c->password != NULL && c->username == NULL)) {
        return -1;
    }

    remaining = 10 + 2 + strlen(c->client_id);

    if (c->username != NULL) {
        flags |= 0x80u;
        remaining += 2 + strlen(c->username);
    }

    if (c->password != NULL) {
        flags |= 0x40u;
        remaining += 2 + strlen(c->password);
    }

    if (c->clean_session) {
        flags |= 0x02u;
    }

    if (!put_header(&w, MQTT_CONNECT, 0, remaining) || !put_str(&w, "MQTT")) {
        return -1;
    }

    put_u8(&w, 4);
    put_u8(&w, flags);
    put_u16(&w, c->keepalive_s);

    if (!put_str(&w, c->client_id) || (c->username != NULL && !put_str(&w, c->username))
        || (c->password != NULL && !put_str(&w, c->password))) {
        return -1;
    }

    return finish(&w);
}

int mqtt_encode_publish(uint8_t *out, size_t cap, const char *topic, const uint8_t *payload, size_t payload_len,
                        int qos, bool retain, uint16_t packet_id)
{
    writer_t w = { out, cap, 0, false };
    size_t remaining;

    if (topic == NULL || topic[0] == '\0' || strpbrk(topic, "+#") != NULL || (qos != 0 && qos != 1)
        || (qos == 1 && packet_id == 0) || (payload == NULL && payload_len > 0)) {
        return -1;
    }

    remaining = 2 + strlen(topic) + (qos == 1 ? 2 : 0) + payload_len;

    if (!put_header(&w, MQTT_PUBLISH, (uint8_t)(qos << 1 | (retain ? 1 : 0)), remaining) || !put_str(&w, topic)) {
        return -1;
    }

    if (qos == 1) {
        put_u16(&w, packet_id);
    }

    if (payload_len > 0) {
        put_bytes(&w, payload, payload_len);
    }

    return finish(&w);
}

int mqtt_encode_subscribe(uint8_t *out, size_t cap, uint16_t packet_id, const char *topic, int qos)
{
    writer_t w = { out, cap, 0, false };

    if (topic == NULL || topic[0] == '\0' || packet_id == 0 || qos < 0 || qos > 1
        || !put_header(&w, MQTT_SUBSCRIBE, 0x02, 2 + 2 + strlen(topic) + 1)) {
        return -1;
    }

    put_u16(&w, packet_id);

    if (!put_str(&w, topic)) {
        return -1;
    }

    put_u8(&w, (uint8_t)qos);

    return finish(&w);
}

int mqtt_encode_pingreq(uint8_t *out, size_t cap)
{
    writer_t w = { out, cap, 0, false };

    put_header(&w, MQTT_PINGREQ, 0, 0);

    return finish(&w);
}

int mqtt_encode_disconnect(uint8_t *out, size_t cap)
{
    writer_t w = { out, cap, 0, false };

    put_header(&w, MQTT_DISCONNECT, 0, 0);

    return finish(&w);
}

int mqtt_decode_header(const uint8_t *buf, size_t len, uint8_t *type, uint8_t *flags, uint32_t *remaining)
{
    uint32_t value = 0;
    uint32_t mult = 1;

    if (len < 2) {
        return 0;
    }

    for (size_t i = 1; i <= 4; i++) {
        if (i >= len) {
            return 0;
        }

        value += (uint32_t)(buf[i] & 0x7Fu) * mult;

        if ((buf[i] & 0x80u) == 0) {
            /* Reject non-minimal encodings such as 0x80 0x00. */
            if (i > 1 && buf[i] == 0) {
                return -1;
            }

            *type = buf[0] >> 4;
            *flags = buf[0] & 0x0Fu;
            *remaining = value;

            return (int)i + 1;
        }

        mult *= 128u;
    }

    return -1;
}

static uint16_t rd16(const uint8_t *p)
{
    return (uint16_t)(p[0] << 8 | p[1]);
}

int mqtt_decode_packet(const uint8_t *buf, size_t len, mqtt_packet_t *out)
{
    uint8_t type;
    uint8_t flags;
    uint32_t remaining;
    int hl = mqtt_decode_header(buf, len, &type, &flags, &remaining);
    const uint8_t *body;

    if (hl <= 0 || out == NULL || (size_t)hl + remaining != len) {
        return -1;
    }

    body = buf + hl;
    memset(out, 0, sizeof *out);
    out->type = type;

    switch (type) {
    case MQTT_CONNACK:
        if (flags != 0 || remaining != 2 || (body[0] & 0xFEu) != 0) {
            return -1;
        }

        out->session_present = (body[0] & 1u) != 0;
        out->return_code = body[1];

        return 0;
    case MQTT_PUBACK:
        if (flags != 0 || remaining != 2) {
            return -1;
        }

        out->packet_id = rd16(body);

        return 0;
    case MQTT_SUBACK:
        if (flags != 0 || remaining != 3 || (body[2] > 2 && body[2] != 0x80u)) {
            return -1;
        }

        out->packet_id = rd16(body);
        out->return_code = body[2];

        return 0;
    case MQTT_PINGRESP:
        return flags == 0 && remaining == 0 ? 0 : -1;
    case MQTT_PUBLISH: {
        size_t at;

        out->qos = (flags >> 1) & 3u;

        if (out->qos > 1 || remaining < 2) {
            return -1;
        }

        out->topic_len = rd16(body);
        at = 2u + out->topic_len;

        if (out->topic_len == 0 || at + (out->qos == 1 ? 2u : 0u) > remaining) {
            return -1;
        }

        /* SAFETY: the topic bytes lie inside the checked packet; callers get
         * a length, never a NUL-terminated string. */
        out->topic = (const char *)(body + 2);

        if (out->qos == 1) {
            out->packet_id = rd16(body + at);
            at += 2;

            if (out->packet_id == 0) {
                return -1;
            }
        }

        out->payload = body + at;
        out->payload_len = remaining - at;

        return 0;
    }
    default:
        return -1;
    }
}
