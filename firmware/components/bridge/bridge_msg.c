#include "bridge_msg.h"

#include "cJSON.h"

#include <errno.h>
#include <string.h>

#define BRIDGE_RATE_MIN 8000
#define BRIDGE_RATE_MAX 48000
#define BRIDGE_FRAME_MIN 5
#define BRIDGE_FRAME_MAX 120

static size_t utf8_seq(unsigned char lead)
{
    if ((lead & 0x80) == 0x00) {
        return 1;
    }

    if ((lead & 0xe0) == 0xc0) {
        return 2;
    }

    if ((lead & 0xf0) == 0xe0) {
        return 3;
    }

    if ((lead & 0xf8) == 0xf0) {
        return 4;
    }

    return 1;
}

/* Copy src into dst[cap], always NUL-terminated when cap > 0.
 * A sequence that would be split to fit, or that ends early in src, is dropped. */
static void copy_trunc(char *dst, size_t cap, const char *src)
{
    size_t n = 0;

    if (cap == 0) {
        return;
    }

    if (src != NULL) {
        while (src[n] != '\0') {
            size_t seq = utf8_seq((unsigned char)src[n]);
            size_t k;

            if (n + seq >= cap) {
                break;
            }

            for (k = 0; k < seq; k++) {
                if (src[n + k] == '\0') {
                    seq = 0;
                    break;
                }

                dst[n + k] = src[n + k];
            }

            if (seq == 0) {
                break;
            }

            n += seq;
        }
    }

    while (n < cap) {
        dst[n] = '\0';
        n++;
    }
}

static int read_whole(const cJSON *item, int min_v, int max_v, int *out)
{
    double v;
    int n;

    if (!cJSON_IsNumber(item)) {
        return -EINVAL;
    }

    v = cJSON_GetNumberValue(item);

    if (!(v >= (double)min_v && v <= (double)max_v)) {
        return -EINVAL;
    }

    n = (int)v;

    if ((double)n != v) {
        return -EINVAL;
    }

    *out = n;

    return 0;
}

static const char *json_string(const cJSON *object, const char *key, int required, int *rc)
{
    const cJSON *item;

    item = cJSON_GetObjectItemCaseSensitive(object, key);

    if (item == NULL) {
        if (required) {
            *rc = -EINVAL;
        }

        return NULL;
    }

    if (!cJSON_IsString(item) || item->valuestring == NULL) {
        *rc = -EINVAL;

        return NULL;
    }

    return item->valuestring;
}

static int parse_hello(const cJSON *root, bridge_msg_t *msg)
{
    const cJSON *params;
    int sample_rate;
    int frame_ms;
    int rc;

    params = cJSON_GetObjectItemCaseSensitive(root, "audio_params");

    if (!cJSON_IsObject(params)) {
        return -EINVAL;
    }

    rc = read_whole(cJSON_GetObjectItemCaseSensitive(params, "sample_rate"),
                    BRIDGE_RATE_MIN, BRIDGE_RATE_MAX, &sample_rate);

    if (rc != 0) {
        return rc;
    }

    rc = read_whole(cJSON_GetObjectItemCaseSensitive(params, "frame_duration"),
                    BRIDGE_FRAME_MIN, BRIDGE_FRAME_MAX, &frame_ms);

    if (rc != 0) {
        return rc;
    }

    msg->kind = BRIDGE_HELLO;
    msg->hello.sample_rate = sample_rate;
    msg->hello.frame_ms = frame_ms;

    return 0;
}

static int parse_tts(const cJSON *root, bridge_msg_t *msg)
{
    int rc = 0;
    const char *state;
    const char *text;

    state = json_string(root, "state", 1, &rc);

    if (state == NULL) {
        return rc;
    }

    if (strcmp(state, "start") == 0) {
        msg->tts.state = BRIDGE_TTS_START;
    } else if (strcmp(state, "stop") == 0) {
        msg->tts.state = BRIDGE_TTS_STOP;
    } else if (strcmp(state, "sentence_start") == 0) {
        msg->tts.state = BRIDGE_TTS_SENTENCE_START;
    } else {
        return -EINVAL;
    }

    text = json_string(root, "text", 0, &rc);

    if (rc != 0) {
        return rc;
    }

    copy_trunc(msg->tts.text, sizeof(msg->tts.text), text);
    msg->kind = BRIDGE_TTS;

    return 0;
}

static int parse_llm(const cJSON *root, bridge_msg_t *msg)
{
    int rc = 0;
    const char *emotion;

    emotion = json_string(root, "emotion", 1, &rc);

    if (emotion == NULL) {
        return rc;
    }

    copy_trunc(msg->llm.emotion, sizeof(msg->llm.emotion), emotion);
    msg->kind = BRIDGE_LLM;

    return 0;
}

static int parse_stt(const cJSON *root, bridge_msg_t *msg)
{
    int rc = 0;
    const char *text;

    text = json_string(root, "text", 1, &rc);

    if (text == NULL) {
        return rc;
    }

    copy_trunc(msg->stt.text, sizeof(msg->stt.text), text);
    msg->kind = BRIDGE_STT;

    return 0;
}

static int parse_listen(const cJSON *root, bridge_msg_t *msg)
{
    int rc = 0;
    const char *state;

    state = json_string(root, "state", 1, &rc);

    if (state == NULL) {
        return rc;
    }

    if (strcmp(state, "stop") != 0) {
        return -EINVAL;
    }

    msg->kind = BRIDGE_LISTEN_STOP;

    return 0;
}

static int parse_alert(const cJSON *root, bridge_msg_t *msg)
{
    int rc = 0;
    const char *message;

    message = json_string(root, "message", 1, &rc);

    if (message == NULL) {
        return rc;
    }

    copy_trunc(msg->alert.text, sizeof(msg->alert.text), message);
    msg->kind = BRIDGE_ALERT;

    return 0;
}

static int reject_len(const char *json, size_t len, const void *out)
{
    if (json == NULL || out == NULL) {
        return -EINVAL;
    }

    if (len > BRIDGE_JSON_MAX) {
        return -EMSGSIZE;
    }

    return 0;
}

int bridge_parse_server(const char *json, size_t len, bridge_msg_t *out)
{
    cJSON *root;
    int rc = 0;
    const char *type;
    bridge_msg_t msg;

    rc = reject_len(json, len, out);

    if (rc != 0) {
        return rc;
    }

    root = cJSON_ParseWithLength(json, len);

    if (!cJSON_IsObject(root)) {
        cJSON_Delete(root);

        return -EINVAL;
    }

    type = json_string(root, "type", 1, &rc);

    if (type == NULL) {
        cJSON_Delete(root);

        return rc;
    }

    memset(&msg, 0, sizeof(msg));

    if (strcmp(type, "hello") == 0) {
        rc = parse_hello(root, &msg);
    } else if (strcmp(type, "tts") == 0) {
        rc = parse_tts(root, &msg);
    } else if (strcmp(type, "llm") == 0) {
        rc = parse_llm(root, &msg);
    } else if (strcmp(type, "stt") == 0) {
        rc = parse_stt(root, &msg);
    } else if (strcmp(type, "listen") == 0) {
        rc = parse_listen(root, &msg);
    } else if (strcmp(type, "alert") == 0) {
        rc = parse_alert(root, &msg);
    } else {
        msg.kind = BRIDGE_UNKNOWN;
        rc = 0;
    }

    cJSON_Delete(root);

    if (rc != 0) {
        return rc;
    }

    *out = msg;

    return 0;
}

static int is_ws_url(const char *url)
{
    static const char wss[] = "wss://";
    static const char ws[] = "ws://";

    if (strncmp(url, wss, sizeof(wss) - 1) == 0) {
        return 1;
    }

    if (strncmp(url, ws, sizeof(ws) - 1) == 0) {
        return 1;
    }

    return 0;
}

int bridge_parse_ota(const char *json, size_t len, bridge_ota_t *out)
{
    cJSON *root;
    const cJSON *ws;
    int rc = 0;
    const char *url;
    const char *token;
    bridge_ota_t msg;

    rc = reject_len(json, len, out);

    if (rc != 0) {
        return rc;
    }

    root = cJSON_ParseWithLength(json, len);

    if (!cJSON_IsObject(root)) {
        cJSON_Delete(root);

        return -EINVAL;
    }

    ws = cJSON_GetObjectItemCaseSensitive(root, "websocket");

    if (!cJSON_IsObject(ws)) {
        cJSON_Delete(root);

        return -EINVAL;
    }

    url = json_string(ws, "url", 1, &rc);

    if (url == NULL || !is_ws_url(url)) {
        cJSON_Delete(root);

        return -EINVAL;
    }

    token = json_string(ws, "token", 0, &rc);

    if (rc != 0) {
        cJSON_Delete(root);

        return rc;
    }

    memset(&msg, 0, sizeof(msg));
    copy_trunc(msg.ws_url, sizeof(msg.ws_url), url);
    copy_trunc(msg.token, sizeof(msg.token), token);
    cJSON_Delete(root);
    *out = msg;

    return 0;
}

static size_t emit_json(cJSON *root, char *buf, size_t cap)
{
    char *printed;
    size_t n;

    if (root == NULL) {
        return 0;
    }

    if (buf == NULL || cap == 0) {
        cJSON_Delete(root);

        return 0;
    }

    printed = cJSON_PrintUnformatted(root);
    cJSON_Delete(root);

    if (printed == NULL) {
        return 0;
    }

    n = strlen(printed);

    if (n + 1 > cap) {
        cJSON_free(printed);

        return 0;
    }

    memcpy(buf, printed, n + 1);
    cJSON_free(printed);

    return n;
}

static cJSON *object_with_type(const char *type)
{
    cJSON *root;

    root = cJSON_CreateObject();

    if (root == NULL) {
        return NULL;
    }

    if (cJSON_AddStringToObject(root, "type", type) == NULL) {
        cJSON_Delete(root);

        return NULL;
    }

    return root;
}

size_t bridge_build_hello(char *buf, size_t cap)
{
    cJSON *root;
    cJSON *audio;
    cJSON *features;

    root = object_with_type("hello");

    if (root == NULL) {
        return 0;
    }

    if (cJSON_AddStringToObject(root, "transport", "websocket") == NULL) {
        cJSON_Delete(root);

        return 0;
    }

    audio = cJSON_AddObjectToObject(root, "audio_params");

    if (audio == NULL
        || cJSON_AddStringToObject(audio, "format", "opus") == NULL
        || cJSON_AddNumberToObject(audio, "sample_rate", 16000) == NULL
        || cJSON_AddNumberToObject(audio, "channels", 1) == NULL
        || cJSON_AddNumberToObject(audio, "frame_duration", 60) == NULL) {
        cJSON_Delete(root);

        return 0;
    }

    features = cJSON_AddObjectToObject(root, "features");

    if (features == NULL || cJSON_AddBoolToObject(features, "barge_in", 1) == NULL) {
        cJSON_Delete(root);

        return 0;
    }

    return emit_json(root, buf, cap);
}

size_t bridge_build_listen(char *buf, size_t cap, const char *state, const char *mode)
{
    cJSON *root;

    if (state == NULL || mode == NULL) {
        return 0;
    }

    if (strcmp(state, "start") != 0 && strcmp(state, "stop") != 0 && strcmp(state, "detect") != 0) {
        return 0;
    }

    root = object_with_type("listen");

    if (root == NULL) {
        return 0;
    }

    if (cJSON_AddStringToObject(root, "state", state) == NULL
        || cJSON_AddStringToObject(root, "mode", mode) == NULL) {
        cJSON_Delete(root);

        return 0;
    }

    return emit_json(root, buf, cap);
}

size_t bridge_build_abort(char *buf, size_t cap)
{
    return emit_json(object_with_type("abort"), buf, cap);
}
