/* Host check for the xiaozhi v1 JSON boundary. No ESP-IDF. */

#include "bridge_msg.h"

#include <assert.h>
#include <errno.h>
#include <stdio.h>
#include <string.h>

static void expect_err_msg(const char *json, size_t len, int err)
{
    bridge_msg_t msg;
    bridge_msg_t saved;
    int rc;

    memset(&msg, 0x5a, sizeof(msg));
    saved = msg;
    rc = bridge_parse_server(json, len, &msg);

    assert(rc == err);
    assert(memcmp(&msg, &saved, sizeof(msg)) == 0);
}

static void expect_err_ota(const char *json, size_t len, int err)
{
    bridge_ota_t ota;
    bridge_ota_t saved;
    int rc;

    memset(&ota, 0x5a, sizeof(ota));
    saved = ota;
    rc = bridge_parse_ota(json, len, &ota);

    assert(rc == err);
    assert(memcmp(&ota, &saved, sizeof(ota)) == 0);
}

static void expect_zeros(const char *s, size_t cap)
{
    size_t n;
    size_t i;

    n = strlen(s);

    assert(n + 1 <= cap);

    for (i = n + 1; i < cap; i++) {
        assert(s[i] == '\0');
    }
}

static void test_hello(void)
{
    const char *json =
        "{\"type\":\"hello\",\"transport\":\"websocket\",\"session_id\":\"abc\","
        "\"audio_params\":{\"format\":\"opus\",\"sample_rate\":24000,\"channels\":1,"
        "\"frame_duration\":60},\"features\":{\"barge_in\":true}}";
    bridge_msg_t msg;
    int rc;

    memset(&msg, 0x5a, sizeof(msg));
    rc = bridge_parse_server(json, strlen(json), &msg);

    assert(rc == 0);
    assert(msg.kind == BRIDGE_HELLO);
    assert(msg.hello.sample_rate == 24000);
    assert(msg.hello.frame_ms == 60);

    expect_err_msg("{\"type\":\"hello\"}", strlen("{\"type\":\"hello\"}"), -EINVAL);
    expect_err_msg("{\"type\":\"hello\",\"audio_params\":{\"sample_rate\":\"24000\",\"frame_duration\":60}}",
                   strlen("{\"type\":\"hello\",\"audio_params\":{\"sample_rate\":\"24000\",\"frame_duration\":60}}"),
                   -EINVAL);
    expect_err_msg("{\"type\":\"hello\",\"audio_params\":{\"sample_rate\":7999,\"frame_duration\":60}}",
                   strlen("{\"type\":\"hello\",\"audio_params\":{\"sample_rate\":7999,\"frame_duration\":60}}"),
                   -EINVAL);
    expect_err_msg("{\"type\":\"hello\",\"audio_params\":{\"sample_rate\":16000.5,\"frame_duration\":60}}",
                   strlen("{\"type\":\"hello\",\"audio_params\":{\"sample_rate\":16000.5,\"frame_duration\":60}}"),
                   -EINVAL);
    expect_err_msg("{\"type\":\"hello\",\"audio_params\":{\"sample_rate\":16000,\"frame_duration\":4}}",
                   strlen("{\"type\":\"hello\",\"audio_params\":{\"sample_rate\":16000,\"frame_duration\":4}}"),
                   -EINVAL);
}

static void test_tts(void)
{
    bridge_msg_t msg;
    const char *start = "{\"type\":\"tts\",\"state\":\"start\"}";
    const char *stop = "{\"type\":\"tts\",\"state\":\"stop\"}";
    const char *sentence = "{\"type\":\"tts\",\"state\":\"sentence_start\",\"text\":\"He said \\\"hi\\\"\"}";

    memset(&msg, 0x5a, sizeof(msg));
    assert(bridge_parse_server(start, strlen(start), &msg) == 0);
    assert(msg.kind == BRIDGE_TTS);
    assert(msg.tts.state == BRIDGE_TTS_START);
    assert(msg.tts.text[0] == '\0');
    expect_zeros(msg.tts.text, sizeof(msg.tts.text));

    assert(bridge_parse_server(stop, strlen(stop), &msg) == 0);
    assert(msg.tts.state == BRIDGE_TTS_STOP);

    assert(bridge_parse_server(sentence, strlen(sentence), &msg) == 0);
    assert(msg.tts.state == BRIDGE_TTS_SENTENCE_START);
    assert(strcmp(msg.tts.text, "He said \"hi\"") == 0);

    expect_err_msg("{\"type\":\"tts\"}", strlen("{\"type\":\"tts\"}"), -EINVAL);
    expect_err_msg("{\"type\":\"tts\",\"state\":\"pause\"}", strlen("{\"type\":\"tts\",\"state\":\"pause\"}"), -EINVAL);
    expect_err_msg("{\"type\":\"tts\",\"state\":\"start\",\"text\":1}",
                   strlen("{\"type\":\"tts\",\"state\":\"start\",\"text\":1}"), -EINVAL);
}

static void test_llm_and_stt(void)
{
    bridge_msg_t msg;
    const char *llm = "{\"type\":\"llm\",\"emotion\":\"thinking\",\"text\":\"\"}";
    const char *stt = "  {\"type\":\"stt\",\"text\":\"turn the light off\"}";

    memset(&msg, 0x5a, sizeof(msg));
    assert(bridge_parse_server(llm, strlen(llm), &msg) == 0);
    assert(msg.kind == BRIDGE_LLM);
    assert(strcmp(msg.llm.emotion, "thinking") == 0);
    expect_zeros(msg.llm.emotion, sizeof(msg.llm.emotion));

    assert(bridge_parse_server(stt, strlen(stt), &msg) == 0);
    assert(msg.kind == BRIDGE_STT);
    assert(strcmp(msg.stt.text, "turn the light off") == 0);
    expect_zeros(msg.stt.text, sizeof(msg.stt.text));

    expect_err_msg("{\"type\":\"llm\"}", strlen("{\"type\":\"llm\"}"), -EINVAL);
    expect_err_msg("{\"type\":\"stt\"}", strlen("{\"type\":\"stt\"}"), -EINVAL);
    expect_err_msg("{\"type\":\"llm\",\"emotion\":false}", strlen("{\"type\":\"llm\",\"emotion\":false}"), -EINVAL);
}

static void test_listen_stop_and_alert(void)
{
    bridge_msg_t msg;
    const char *listen = "{\"type\":\"listen\",\"state\":\"stop\",\"reason\":\"followup_timeout\"}";
    const char *alert =
        "{\"type\":\"alert\",\"status\":\"Error\","
        "\"message\":\"Voice line hiccup - try again\",\"emotion\":\"sad\"}";

    memset(&msg, 0x5a, sizeof(msg));
    assert(bridge_parse_server(listen, strlen(listen), &msg) == 0);
    assert(msg.kind == BRIDGE_LISTEN_STOP);

    assert(bridge_parse_server(alert, strlen(alert), &msg) == 0);
    assert(msg.kind == BRIDGE_ALERT);
    assert(strcmp(msg.alert.text, "Voice line hiccup - try again") == 0);
    expect_zeros(msg.alert.text, sizeof(msg.alert.text));

    expect_err_msg("{\"type\":\"listen\",\"state\":\"start\"}",
                   strlen("{\"type\":\"listen\",\"state\":\"start\"}"), -EINVAL);
    expect_err_msg("{\"type\":\"listen\"}", strlen("{\"type\":\"listen\"}"), -EINVAL);
    expect_err_msg("{\"type\":\"alert\",\"status\":\"Error\"}",
                   strlen("{\"type\":\"alert\",\"status\":\"Error\"}"), -EINVAL);
}

static void test_unknown(void)
{
    bridge_msg_t msg;
    const char *mcp = "{\"type\":\"mcp\",\"payload\":{\"method\":\"ping\"}}";
    const char *empty = "{\"type\":\"\"}";

    memset(&msg, 0x5a, sizeof(msg));
    assert(bridge_parse_server(mcp, strlen(mcp), &msg) == 0);
    assert(msg.kind == BRIDGE_UNKNOWN);

    assert(bridge_parse_server(empty, strlen(empty), &msg) == 0);
    assert(msg.kind == BRIDGE_UNKNOWN);
}

static void test_malformed_and_missing_type(void)
{
    bridge_msg_t msg;

    expect_err_msg("{", 1, -EINVAL);
    expect_err_msg("{\"type\":", strlen("{\"type\":"), -EINVAL);
    expect_err_msg("not json", strlen("not json"), -EINVAL);
    expect_err_msg("", 0, -EINVAL);
    expect_err_msg("[{\"type\":\"stt\",\"text\":\"hi\"}]",
                   strlen("[{\"type\":\"stt\",\"text\":\"hi\"}]"), -EINVAL);
    expect_err_msg("\"hello\"", strlen("\"hello\""), -EINVAL);
    expect_err_msg("42", 2, -EINVAL);
    expect_err_msg("null", 4, -EINVAL);
    expect_err_msg("{}", 2, -EINVAL);
    expect_err_msg("{\"audio_params\":{}}", strlen("{\"audio_params\":{}}"), -EINVAL);
    expect_err_msg("{\"type\":1}", strlen("{\"type\":1}"), -EINVAL);
    expect_err_msg("{\"type\":null}", strlen("{\"type\":null}"), -EINVAL);

    memset(&msg, 0x5a, sizeof(msg));
    assert(bridge_parse_server(NULL, 1, &msg) == -EINVAL);
    assert(bridge_parse_server("{}", 2, NULL) == -EINVAL);
}

static void test_oversize(void)
{
    char json[] = "{\"type\":\"stt\",\"text\":\"hi\"}";
    char big[BRIDGE_JSON_MAX];
    bridge_msg_t msg;
    bridge_ota_t ota;
    const char *ota_json = "{\"websocket\":{\"url\":\"ws://bridge.example/xiaozhi/v1/\"}}";

    expect_err_msg(json, BRIDGE_JSON_MAX + 1, -EMSGSIZE);
    expect_err_ota(ota_json, BRIDGE_JSON_MAX + 1, -EMSGSIZE);

    memset(big, ' ', sizeof(big));
    memcpy(big, json, strlen(json));
    memset(&msg, 0x5a, sizeof(msg));
    assert(bridge_parse_server(big, sizeof(big), &msg) == 0);
    assert(msg.kind == BRIDGE_STT);
    assert(strcmp(msg.stt.text, "hi") == 0);

    memset(&ota, 0x5a, sizeof(ota));
    assert(bridge_parse_ota(ota_json, strlen(ota_json), &ota) == 0);
    assert(strcmp(ota.ws_url, "ws://bridge.example/xiaozhi/v1/") == 0);
}

static void test_truncation(void)
{
    char text[400];
    char emotion[80];
    char message[200];
    char body[700];
    char euro_emotion[40];
    bridge_msg_t msg;
    int n;
    size_t i;

    memset(text, 'a', 300);
    text[300] = '\0';
    memset(emotion, 'b', 40);
    emotion[40] = '\0';
    memset(message, 'c', 180);
    message[180] = '\0';

    n = snprintf(body, sizeof(body),
                 "{\"type\":\"stt\",\"text\":\"%s\"}", text);
    assert(n > 0 && (size_t)n < sizeof(body));
    memset(&msg, 0x5a, sizeof(msg));
    assert(bridge_parse_server(body, (size_t)n, &msg) == 0);
    assert(msg.kind == BRIDGE_STT);
    assert(strlen(msg.stt.text) == sizeof(msg.stt.text) - 1);
    assert(msg.stt.text[0] == 'a');
    expect_zeros(msg.stt.text, sizeof(msg.stt.text));

    n = snprintf(body, sizeof(body),
                 "{\"type\":\"llm\",\"emotion\":\"%s\"}", emotion);
    assert(n > 0 && (size_t)n < sizeof(body));
    assert(bridge_parse_server(body, (size_t)n, &msg) == 0);
    assert(strlen(msg.llm.emotion) == sizeof(msg.llm.emotion) - 1);
    assert(msg.llm.emotion[0] == 'b');
    expect_zeros(msg.llm.emotion, sizeof(msg.llm.emotion));

    n = snprintf(body, sizeof(body),
                 "{\"type\":\"alert\",\"message\":\"%s\"}", message);
    assert(n > 0 && (size_t)n < sizeof(body));
    assert(bridge_parse_server(body, (size_t)n, &msg) == 0);
    assert(msg.kind == BRIDGE_ALERT);
    assert(strlen(msg.alert.text) == sizeof(msg.alert.text) - 1);
    assert(msg.alert.text[0] == 'c');
    expect_zeros(msg.alert.text, sizeof(msg.alert.text));

    /* 22 ASCII bytes plus a 3-byte euro must not split the codepoint into emotion[24]. */
    for (i = 0; i < 22; i++) {
        euro_emotion[i] = 'a';
    }

    euro_emotion[22] = (char)0xe2;
    euro_emotion[23] = (char)0x82;
    euro_emotion[24] = (char)0xac;
    euro_emotion[25] = '\0';
    n = snprintf(body, sizeof(body),
                 "{\"type\":\"llm\",\"emotion\":\"%s\"}", euro_emotion);
    assert(n > 0 && (size_t)n < sizeof(body));
    assert(bridge_parse_server(body, (size_t)n, &msg) == 0);
    assert(strlen(msg.llm.emotion) == 22);
    assert(msg.llm.emotion[22] == '\0');
    expect_zeros(msg.llm.emotion, sizeof(msg.llm.emotion));
}

static void test_ota(void)
{
    bridge_ota_t ota;
    const char *with_token =
        "{\"server_time\":{\"timestamp\":1,\"timezone_offset\":-420},"
        "\"websocket\":{\"url\":\"ws://bridge.example:8787/xiaozhi/v1/\",\"version\":1,\"token\":\"example-token\"}}";
    const char *wss = "{\"websocket\":{\"url\":\"wss://example.com/xiaozhi/v1/\"}}";
    char long_url[220];
    char long_token[140];
    char body[420];
    int n;
    size_t i;

    memset(&ota, 0x5a, sizeof(ota));
    assert(bridge_parse_ota(with_token, strlen(with_token), &ota) == 0);
    assert(strcmp(ota.ws_url, "ws://bridge.example:8787/xiaozhi/v1/") == 0);
    assert(strcmp(ota.token, "example-token") == 0);
    expect_zeros(ota.ws_url, sizeof(ota.ws_url));
    expect_zeros(ota.token, sizeof(ota.token));

    memset(&ota, 0x5a, sizeof(ota));
    assert(bridge_parse_ota(wss, strlen(wss), &ota) == 0);
    assert(strcmp(ota.ws_url, "wss://example.com/xiaozhi/v1/") == 0);
    assert(ota.token[0] == '\0');
    expect_zeros(ota.token, sizeof(ota.token));

    expect_err_ota("{\"websocket\":{\"url\":\"http://bridge.example/xiaozhi/v1/\"}}",
                   strlen("{\"websocket\":{\"url\":\"http://bridge.example/xiaozhi/v1/\"}}"), -EINVAL);
    expect_err_ota("{\"websocket\":{\"url\":\"ws:/nope\"}}",
                   strlen("{\"websocket\":{\"url\":\"ws:/nope\"}}"), -EINVAL);
    expect_err_ota("{\"websocket\":{}}", strlen("{\"websocket\":{}}"), -EINVAL);
    expect_err_ota("{}", 2, -EINVAL);
    expect_err_ota("[]", 2, -EINVAL);
    expect_err_ota("{\"websocket\":{\"url\":\"ws://ok\",\"token\":1}}",
                   strlen("{\"websocket\":{\"url\":\"ws://ok\",\"token\":1}}"), -EINVAL);
    assert(bridge_parse_ota(NULL, 1, &ota) == -EINVAL);
    assert(bridge_parse_ota(wss, strlen(wss), NULL) == -EINVAL);

    memset(long_url, 'a', sizeof(long_url) - 1);
    long_url[sizeof(long_url) - 1] = '\0';
    memcpy(long_url, "wss://", 6);
    memset(long_token, 't', sizeof(long_token) - 1);
    long_token[sizeof(long_token) - 1] = '\0';
    n = snprintf(body, sizeof(body),
                 "{\"websocket\":{\"url\":\"%s\",\"token\":\"%s\"}}", long_url, long_token);
    assert(n > 0 && (size_t)n < sizeof(body));
    memset(&ota, 0x5a, sizeof(ota));
    assert(bridge_parse_ota(body, (size_t)n, &ota) == 0);
    assert(strlen(ota.ws_url) == sizeof(ota.ws_url) - 1);
    assert(strncmp(ota.ws_url, "wss://", 6) == 0);
    expect_zeros(ota.ws_url, sizeof(ota.ws_url));
    assert(strlen(ota.token) == sizeof(ota.token) - 1);
    expect_zeros(ota.token, sizeof(ota.token));

    for (i = 0; i < strlen(ota.token); i++) {
        assert(ota.token[i] == 't');
    }
}

static void test_build_hello(void)
{
    const char *expect =
        "{\"type\":\"hello\",\"transport\":\"websocket\","
        "\"audio_params\":{\"format\":\"opus\",\"sample_rate\":16000,\"channels\":1,\"frame_duration\":60},"
        "\"features\":{\"barge_in\":true}}";
    char buf[256];
    char dirty[256];
    size_t n;

    memset(buf, 0x5a, sizeof(buf));
    n = bridge_build_hello(buf, sizeof(buf));

    assert(n == strlen(expect));
    assert(strcmp(buf, expect) == 0);
    assert(buf[n] == '\0');

    memset(dirty, 0x5a, sizeof(dirty));
    assert(bridge_build_hello(dirty, n) == 0);
    assert(dirty[0] == 0x5a);
    assert(bridge_build_hello(NULL, sizeof(buf)) == 0);
    assert(bridge_build_hello(buf, 0) == 0);
}

static void test_build_listen_and_abort(void)
{
    char buf[128];
    char dirty[128];
    const char *start = "{\"type\":\"listen\",\"state\":\"start\",\"mode\":\"auto\"}";
    const char *stop = "{\"type\":\"listen\",\"state\":\"stop\",\"mode\":\"manual\"}";
    const char *detect = "{\"type\":\"listen\",\"state\":\"detect\",\"mode\":\"auto\"}";
    const char *escaped = "{\"type\":\"listen\",\"state\":\"start\",\"mode\":\"a\\\"b\"}";
    const char *abort_json = "{\"type\":\"abort\"}";
    size_t n;

    memset(buf, 0x5a, sizeof(buf));
    n = bridge_build_listen(buf, sizeof(buf), "start", "auto");
    assert(n == strlen(start));
    assert(strcmp(buf, start) == 0);

    n = bridge_build_listen(buf, sizeof(buf), "stop", "manual");
    assert(n == strlen(stop));
    assert(strcmp(buf, stop) == 0);

    n = bridge_build_listen(buf, sizeof(buf), "detect", "auto");
    assert(n == strlen(detect));
    assert(strcmp(buf, detect) == 0);

    n = bridge_build_listen(buf, sizeof(buf), "start", "a\"b");
    assert(n == strlen(escaped));
    assert(strcmp(buf, escaped) == 0);

    memset(dirty, 0x5a, sizeof(dirty));
    assert(bridge_build_listen(dirty, sizeof(dirty), "pause", "auto") == 0);
    assert(dirty[0] == 0x5a);
    assert(bridge_build_listen(dirty, sizeof(dirty), NULL, "auto") == 0);
    assert(bridge_build_listen(dirty, sizeof(dirty), "start", NULL) == 0);
    assert(dirty[0] == 0x5a);

    n = bridge_build_abort(buf, sizeof(buf));
    assert(n == strlen(abort_json));
    assert(strcmp(buf, abort_json) == 0);

    memset(dirty, 0x5a, sizeof(dirty));
    assert(bridge_build_abort(dirty, n) == 0);
    assert(dirty[0] == 0x5a);
    assert(bridge_build_abort(NULL, 32) == 0);
}

int main(void)
{
    test_hello();
    test_tts();
    test_llm_and_stt();
    test_listen_stop_and_alert();
    test_unknown();
    test_malformed_and_missing_type();
    test_oversize();
    test_truncation();
    test_ota();
    test_build_hello();
    test_build_listen_and_abort();
    printf("ok\n");

    return 0;
}
