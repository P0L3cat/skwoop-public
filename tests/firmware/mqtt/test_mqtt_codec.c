/* Host checks for the MQTT 3.1.1 codec against hand-built packets from the
 * spec (section numbers in comments). No network. */

#include "mqtt_codec.h"

#include <assert.h>
#include <string.h>

static void test_connect(void)
{
    uint8_t buf[128];
    const mqtt_connect_t c = { .client_id = "pod", .username = "u", .password = "pw", .keepalive_s = 60,
                               .clean_session = true };
    /* 3.1: fixed header, "MQTT", level 4, flags user|pass|clean, keepalive 60. */
    const uint8_t want[] = { 0x10, 22, 0, 4, 'M', 'Q', 'T', 'T', 4, 0xC2, 0, 60, 0, 3, 'p', 'o', 'd',
                             0, 1, 'u', 0, 2, 'p', 'w' };
    const mqtt_connect_t nouser = { .client_id = "pod", .password = "pw" };

    assert(mqtt_encode_connect(buf, sizeof buf, &c) == (int)sizeof want);
    assert(memcmp(buf, want, sizeof want) == 0);
    assert(mqtt_encode_connect(buf, 10, &c) == -1);
    assert(mqtt_encode_connect(buf, sizeof buf, &nouser) == -1);
}

static void test_publish_subscribe(void)
{
    uint8_t buf[64];
    const uint8_t on[] = { 'O', 'N' };
    const uint8_t want0[] = { 0x31, 7, 0, 3, 'a', '/', 'b', 'O', 'N' };
    const uint8_t want1[] = { 0x32, 9, 0, 3, 'a', '/', 'b', 0x12, 0x34, 'O', 'N' };
    const uint8_t sub[] = { 0x82, 8, 0, 7, 0, 3, 'a', '/', '#', 0 };

    assert(mqtt_encode_publish(buf, sizeof buf, "a/b", on, 2, 0, true, 0) == (int)sizeof want0);
    assert(memcmp(buf, want0, sizeof want0) == 0);
    assert(mqtt_encode_publish(buf, sizeof buf, "a/b", on, 2, 1, false, 0x1234) == (int)sizeof want1);
    assert(memcmp(buf, want1, sizeof want1) == 0);
    assert(mqtt_encode_publish(buf, sizeof buf, "a/+", on, 2, 0, false, 0) == -1);
    assert(mqtt_encode_publish(buf, sizeof buf, "a/b", on, 2, 1, false, 0) == -1);
    assert(mqtt_encode_publish(buf, sizeof buf, "a/b", on, 2, 2, false, 1) == -1);
    assert(mqtt_encode_publish(buf, 5, "a/b", on, 2, 0, false, 0) == -1);

    assert(mqtt_encode_subscribe(buf, sizeof buf, 7, "a/#", 0) == (int)sizeof sub);
    assert(memcmp(buf, sub, sizeof sub) == 0);
    assert(mqtt_encode_pingreq(buf, sizeof buf) == 2 && buf[0] == 0xC0 && buf[1] == 0);
    assert(mqtt_encode_disconnect(buf, sizeof buf) == 2 && buf[0] == 0xE0);
}

static void test_remaining_length(void)
{
    /* 2.2.3 boundaries. */
    static const struct {
        uint8_t bytes[5];
        size_t n;
        uint32_t value;
    } cases[] = {
        { { 0x30, 0x00 }, 2, 0 },
        { { 0x30, 0x7F }, 2, 127 },
        { { 0x30, 0x80, 0x01 }, 3, 128 },
        { { 0x30, 0xFF, 0x7F }, 3, 16383 },
        { { 0x30, 0x80, 0x80, 0x01 }, 4, 16384 },
        { { 0x30, 0xFF, 0xFF, 0xFF, 0x7F }, 5, MQTT_REMAINING_MAX },
    };
    uint8_t type;
    uint8_t flags;
    uint32_t rem;
    uint8_t big[600];
    static const uint8_t five[] = { 0x30, 0xFF, 0xFF, 0xFF, 0xFF, 0x01 };
    static const uint8_t nonmin[] = { 0x30, 0x80, 0x00 };

    for (size_t i = 0; i < sizeof cases / sizeof cases[0]; i++) {
        assert(mqtt_decode_header(cases[i].bytes, cases[i].n, &type, &flags, &rem) == (int)cases[i].n);
        assert(rem == cases[i].value && type == 3);
        assert(mqtt_decode_header(cases[i].bytes, cases[i].n - 1, &type, &flags, &rem) == 0);
    }

    assert(mqtt_decode_header(five, sizeof five, &type, &flags, &rem) == -1);
    assert(mqtt_decode_header(nonmin, sizeof nonmin, &type, &flags, &rem) == -1);

    memset(big, 'x', sizeof big);
    assert(mqtt_encode_publish(big, sizeof big, "t", big + 300, 200, 0, false, 0) == 2 + 1 + 3 + 200);
    assert(big[1] == (0x80 | ((3 + 200) % 128)) && big[2] == (3 + 200) / 128);
}

static void test_decode(void)
{
    mqtt_packet_t p;
    const uint8_t connack[] = { 0x20, 2, 0x01, 0x00 };
    const uint8_t refused[] = { 0x20, 2, 0x00, 0x05 };
    const uint8_t badack[] = { 0x20, 2, 0x02, 0x00 };
    const uint8_t suback[] = { 0x90, 3, 0, 7, 0x00 };
    const uint8_t subfail[] = { 0x90, 3, 0, 7, 0x80 };
    const uint8_t puback[] = { 0x40, 2, 0x12, 0x34 };
    const uint8_t ping[] = { 0xD0, 0 };
    const uint8_t pub[] = { 0x32, 9, 0, 3, 'a', '/', 'b', 0, 5, 'O', 'N' };
    const uint8_t pub_trunc[] = { 0x30, 4, 0, 9, 'a', '/' };
    const uint8_t pub_id0[] = { 0x32, 5, 0, 1, 'a', 0, 0 };
    const uint8_t pub_qos2[] = { 0x34, 5, 0, 1, 'a', 0, 1 };
    const uint8_t connect_in[] = { 0x10, 0 };

    assert(mqtt_decode_packet(connack, sizeof connack, &p) == 0 && p.session_present && p.return_code == 0);
    assert(mqtt_decode_packet(refused, sizeof refused, &p) == 0 && p.return_code == 5);
    assert(mqtt_decode_packet(badack, sizeof badack, &p) == -1);
    assert(mqtt_decode_packet(suback, sizeof suback, &p) == 0 && p.packet_id == 7 && p.return_code == 0);
    assert(mqtt_decode_packet(subfail, sizeof subfail, &p) == 0 && p.return_code == 0x80);
    assert(mqtt_decode_packet(puback, sizeof puback, &p) == 0 && p.packet_id == 0x1234);
    assert(mqtt_decode_packet(ping, sizeof ping, &p) == 0 && p.type == MQTT_PINGRESP);
    assert(mqtt_decode_packet(pub, sizeof pub, &p) == 0);
    assert(p.qos == 1 && p.packet_id == 5 && p.topic_len == 3 && memcmp(p.topic, "a/b", 3) == 0);
    assert(p.payload_len == 2 && memcmp(p.payload, "ON", 2) == 0);
    assert(mqtt_decode_packet(pub_trunc, sizeof pub_trunc, &p) == -1);
    assert(mqtt_decode_packet(pub_id0, sizeof pub_id0, &p) == -1);
    assert(mqtt_decode_packet(pub_qos2, sizeof pub_qos2, &p) == -1);
    assert(mqtt_decode_packet(connect_in, sizeof connect_in, &p) == -1);
    assert(mqtt_decode_packet(connack, sizeof connack - 1, &p) == -1);
}

int main(void)
{
    test_connect();
    test_publish_subscribe();
    test_remaining_length();
    test_decode();

    return 0;
}
