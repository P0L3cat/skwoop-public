#pragma once

#include <stddef.h>

/* Largest JSON body bridge_parse_server and bridge_parse_ota will accept. */
#define BRIDGE_JSON_MAX 4096

/* One server text frame from the xiaozhi v1 bridge.
 * Strings are NUL-terminated. Bytes after the NUL are zero. A longer source
 * string is cut on a UTF-8 boundary so it always fits. */
typedef enum {
    BRIDGE_UNKNOWN = 0,
    BRIDGE_HELLO,
    BRIDGE_TTS,
    BRIDGE_LLM,
    BRIDGE_STT,
    BRIDGE_LISTEN_STOP,
    BRIDGE_ALERT,
} bridge_kind_t;

typedef enum {
    BRIDGE_TTS_START = 1,
    BRIDGE_TTS_STOP,
    BRIDGE_TTS_SENTENCE_START,
} bridge_tts_state_t;

typedef struct {
    bridge_kind_t kind;

    union {
        struct {
            int sample_rate;
            int frame_ms;
        } hello;

        struct {
            bridge_tts_state_t state;
            char text[256];
        } tts;

        struct {
            char emotion[24];
        } llm;

        struct {
            char text[256];
        } stt;

        struct {
            char text[128];
        } alert;
    };
} bridge_msg_t;

/* OTA check-in. ws_url comes from websocket.url. token comes from
 * websocket.token and is empty when the bridge omits it. */
typedef struct {
    char ws_url[160];
    char token[96];
} bridge_ota_t;

/* Parse one server text frame into *out.
 *
 * Returns 0 on success.
 * On failure returns a negative errno and leaves *out unchanged:
 *   -EMSGSIZE  len > BRIDGE_JSON_MAX
 *   -EINVAL    null argument, malformed JSON, non-object, missing or
 *              non-string type, or a known type whose fields are unusable
 *
 * An unrecognized type string succeeds as BRIDGE_UNKNOWN.
 *
 * hello reads audio_params.sample_rate (8000..48000) and frame_duration
 * (5..120 ms) into hello.frame_ms.
 * tts state is start, stop, or sentence_start. text may be absent.
 * llm reads emotion. stt reads text. alert reads message into alert.text.
 * listen is BRIDGE_LISTEN_STOP only when state is stop.
 */
int bridge_parse_server(const char *json, size_t len, bridge_msg_t *out);

/* Parse an OTA check-in body. websocket.url is required and must start with
 * ws:// or wss://. websocket.token is optional. Same return codes as
 * bridge_parse_server. String fields that do not fit are truncated.
 */
int bridge_parse_ota(const char *json, size_t len, bridge_ota_t *out);

/* Pod -> server frames. Each writes one NUL-terminated JSON object and
 * returns the number of bytes excluding that NUL.
 * Returns 0 and writes nothing when buf is NULL, cap is too small, or a
 * listen state/mode cannot be sent.
 *
 * hello is opus, 16000 Hz, 1 channel, 60 ms, features.barge_in true.
 * listen state must be "start", "stop", or "detect". mode is sent as given.
 */
size_t bridge_build_hello(char *buf, size_t cap);

size_t bridge_build_listen(char *buf, size_t cap, const char *state, const char *mode);

size_t bridge_build_abort(char *buf, size_t cap);
