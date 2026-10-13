#include "cloud_proto.h"

#include <stdio.h>
#include <string.h>

#include "cJSON.h"

static int put(char *out, size_t cap, size_t *at, const char *s, size_t n)
{
    if (*at + n >= cap) {
        return -1;
    }

    memcpy(out + *at, s, n);
    *at += n;
    out[*at] = '\0';

    return 0;
}

int cloud_json_escape(const char *in, char *out, size_t cap)
{
    size_t at = 0;

    if (in == NULL || out == NULL || cap == 0) {
        return -1;
    }

    out[0] = '\0';

    /* SAFETY: in is a NUL-terminated string; reading it as unsigned char only
     * changes how control bytes compare, not the bytes read. */
    for (const unsigned char *p = (const unsigned char *)in; *p != '\0'; p++) {
        char esc[8];
        size_t n;

        if (*p == '"' || *p == '\\') {
            esc[0] = '\\';
            esc[1] = (char)*p;
            n = 2;
        } else if (*p == '\n') {
            memcpy(esc, "\\n", 2);
            n = 2;
        } else if (*p < 0x20) {
            n = (size_t)snprintf(esc, sizeof esc, "\\u%04x", *p);
        } else {
            esc[0] = (char)*p;
            n = 1;
        }

        if (put(out, cap, &at, esc, n) != 0) {
            return -1;
        }
    }

    return (int)at;
}

/* Appends "<prefix><escaped value><suffix>". */
static int put_field(char *out, size_t cap, size_t *at, const char *prefix, const char *value, const char *suffix)
{
    int n;

    if (put(out, cap, at, prefix, strlen(prefix)) != 0) {
        return -1;
    }

    n = cloud_json_escape(value, out + *at, cap - *at);

    if (n < 0) {
        return -1;
    }

    *at += (size_t)n;

    return put(out, cap, at, suffix, strlen(suffix));
}

int cloud_chat_body(char *out, size_t cap, const char *model, const char *system, const char *user)
{
    size_t at = 0;

    if (out == NULL || model == NULL || system == NULL || user == NULL
        || put_field(out, cap, &at, "{\"model\":\"", model, "\",\"stream\":true,\"messages\":[") != 0
        || put_field(out, cap, &at, "{\"role\":\"system\",\"content\":\"", system, "\"},") != 0
        || put_field(out, cap, &at, "{\"role\":\"user\",\"content\":\"", user, "\"}]}") != 0) {
        return -1;
    }

    return (int)at;
}

int cloud_tts_openai_body(char *out, size_t cap, const char *model, const char *voice, const char *text)
{
    size_t at = 0;

    if (out == NULL || model == NULL || voice == NULL || text == NULL
        || put_field(out, cap, &at, "{\"model\":\"", model, "\",") != 0
        || put_field(out, cap, &at, "\"voice\":\"", voice, "\",") != 0
        || put_field(out, cap, &at, "\"input\":\"", text, "\",\"response_format\":\"pcm\"}") != 0) {
        return -1;
    }

    return (int)at;
}

int cloud_tts_eleven_body(char *out, size_t cap, const char *model, const char *text)
{
    size_t at = 0;

    if (out == NULL || model == NULL || text == NULL
        || put_field(out, cap, &at, "{\"text\":\"", text, "\",") != 0
        || put_field(out, cap, &at, "\"model_id\":\"", model, "\"}") != 0) {
        return -1;
    }

    return (int)at;
}

static void le16(uint8_t *p, uint16_t v)
{
    p[0] = (uint8_t)v;
    p[1] = (uint8_t)(v >> 8);
}

static void le32(uint8_t *p, uint32_t v)
{
    le16(p, (uint16_t)v);
    le16(p + 2, (uint16_t)(v >> 16));
}

void cloud_wav_header(uint8_t out[CLOUD_WAV_HEADER_BYTES], uint32_t samples, uint32_t rate)
{
    uint32_t data_bytes = samples * 2u;

    memcpy(out, "RIFF", 4);
    le32(out + 4, 36u + data_bytes);
    memcpy(out + 8, "WAVEfmt ", 8);
    le32(out + 16, 16u);
    le16(out + 20, 1u);
    le16(out + 22, 1u);
    le32(out + 24, rate);
    le32(out + 28, rate * 2u);
    le16(out + 32, 2u);
    le16(out + 34, 16u);
    memcpy(out + 36, "data", 4);
    le32(out + 40, data_bytes);
}

static int fit(int n, size_t cap)
{
    return (n < 0 || (size_t)n >= cap) ? -1 : n;
}

int cloud_stt_multipart_head(char *out, size_t cap, const char *boundary, const char *model)
{
    if (out == NULL || boundary == NULL || model == NULL) {
        return -1;
    }

    return fit(snprintf(out, cap,
                        "--%s\r\nContent-Disposition: form-data; name=\"model\"\r\n\r\n%s\r\n"
                        "--%s\r\nContent-Disposition: form-data; name=\"file\"; filename=\"say.wav\"\r\n"
                        "Content-Type: audio/wav\r\n\r\n",
                        boundary, model, boundary),
               cap);
}

int cloud_stt_multipart_tail(char *out, size_t cap, const char *boundary)
{
    if (out == NULL || boundary == NULL) {
        return -1;
    }

    return fit(snprintf(out, cap, "\r\n--%s--\r\n", boundary), cap);
}

int cloud_stt_parse(const char *json, size_t len, char *text, size_t cap)
{
    cJSON *root;
    const cJSON *field;
    int rc = -1;

    if (json == NULL || text == NULL || cap == 0) {
        return -1;
    }

    text[0] = '\0';
    root = cJSON_ParseWithLength(json, len);
    field = cJSON_GetObjectItemCaseSensitive(root, "text");

    if (cJSON_IsString(field) && strlen(field->valuestring) < cap) {
        strcpy(text, field->valuestring);
        rc = (int)strlen(text);
    }

    cJSON_Delete(root);

    return rc;
}

void cloud_sse_init(cloud_sse_t *sse)
{
    memset(sse, 0, sizeof *sse);
}

/* One complete SSE line, without the newline. */
static void sse_line(cloud_sse_t *sse, cloud_token_fn on_token, void *ctx)
{
    const char *payload;
    cJSON *root;
    const cJSON *content;

    sse->line[sse->len] = '\0';

    if (strncmp(sse->line, "data:", 5) != 0) {
        return;
    }

    payload = sse->line + 5;

    while (*payload == ' ') {
        payload++;
    }

    if (strcmp(payload, "[DONE]") == 0) {
        sse->done = true;

        return;
    }

    root = cJSON_Parse(payload);
    content = cJSON_GetObjectItemCaseSensitive(
        cJSON_GetObjectItemCaseSensitive(cJSON_GetArrayItem(cJSON_GetObjectItemCaseSensitive(root, "choices"), 0),
                                         "delta"),
        "content");

    if (cJSON_IsString(content) && content->valuestring[0] != '\0') {
        on_token(content->valuestring, strlen(content->valuestring), ctx);
    }

    cJSON_Delete(root);
}

bool cloud_sse_feed(cloud_sse_t *sse, const char *data, size_t len, cloud_token_fn on_token, void *ctx)
{
    for (size_t i = 0; i < len && !sse->done; i++) {
        char c = data[i];

        if (c == '\n') {
            if (!sse->skipping) {
                if (sse->len > 0 && sse->line[sse->len - 1] == '\r') {
                    sse->len--;
                }

                sse_line(sse, on_token, ctx);
            }

            sse->len = 0;
            sse->skipping = false;
        } else if (sse->skipping) {
            continue;
        } else if (sse->len + 1 < sizeof sse->line) {
            sse->line[sse->len++] = c;
        } else {
            sse->skipping = true;
            sse->dropped_lines++;
        }
    }

    return sse->done;
}

void cloud_split_init(cloud_splitter_t *sp)
{
    sp->len = 0;
}

/* Emits buf[0..n) trimmed, then keeps the rest. */
static void emit(cloud_splitter_t *sp, size_t n, cloud_sentence_fn on_sentence, void *ctx)
{
    char sentence[CLOUD_SENTENCE_MAX];
    size_t start = 0;
    size_t end = n;

    while (start < end && (sp->buf[start] == ' ' || sp->buf[start] == '\n')) {
        start++;
    }

    while (end > start && (sp->buf[end - 1] == ' ' || sp->buf[end - 1] == '\n')) {
        end--;
    }

    if (end > start) {
        memcpy(sentence, sp->buf + start, end - start);
        sentence[end - start] = '\0';
        on_sentence(sentence, ctx);
    }

    memmove(sp->buf, sp->buf + n, sp->len - n);
    sp->len -= n;
}

static bool is_break(char c)
{
    return c == '.' || c == '!' || c == '?' || c == '\n';
}

void cloud_split_feed(cloud_splitter_t *sp, const char *token, size_t len, cloud_sentence_fn on_sentence, void *ctx)
{
    for (size_t i = 0; i < len; i++) {
        char c = token[i];

        if ((c == ' ' || c == '\n') && sp->len > 0 && is_break(sp->buf[sp->len - 1])) {
            emit(sp, sp->len, on_sentence, ctx);
        }

        if (sp->len + 1 >= sizeof sp->buf) {
            size_t cut = sp->len;

            while (cut > 0 && sp->buf[cut - 1] != ' ') {
                cut--;
            }

            emit(sp, cut > 0 ? cut : sp->len, on_sentence, ctx);
        }

        sp->buf[sp->len++] = c;
    }
}

void cloud_split_flush(cloud_splitter_t *sp, cloud_sentence_fn on_sentence, void *ctx)
{
    emit(sp, sp->len, on_sentence, ctx);
}
