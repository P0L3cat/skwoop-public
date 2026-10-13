#include "cloud.h"

#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "esp_crt_bundle.h"
#include "esp_http_client.h"

#define CLOUD_TIMEOUT_MS 15000
#define CLOUD_READ_CHUNK 1024
#define CLOUD_URL_MAX (SKWOOP_BASE_URL_MAX + 96)
#define CLOUD_AUTH_MAX (SKWOOP_API_KEY_MAX + 8)
#define CLOUD_BODY_MAX 2048
#define CLOUD_STT_REPLY_MAX 1024
#define CLOUD_BOUNDARY "skwoopBoundary7d1"
#define CLOUD_STT_RATE 16000

typedef struct {
    const void *ptr;
    size_t len;
} cloud_part_t;

typedef void (*cloud_body_fn)(const char *chunk, size_t len, void *ctx);

typedef struct {
    const char *url;
    const char *auth_header;
    const char *auth_value;
    const char *content_type;
    const cloud_part_t *parts;
    size_t part_count;
} cloud_post_t;

/* POST parts back to back, stream the reply body to on_body. */
static int cloud_post(const cloud_post_t *req, cloud_body_fn on_body, void *ctx)
{
    esp_http_client_config_t cfg = {
        .url = req->url,
        .method = HTTP_METHOD_POST,
        .crt_bundle_attach = esp_crt_bundle_attach,
        .timeout_ms = CLOUD_TIMEOUT_MS,
        .buffer_size = CLOUD_READ_CHUNK,
    };
    esp_http_client_handle_t http;
    size_t total = 0;
    int rc = -EIO;
    char *chunk;

    if (strncmp(req->url, "https://", 8) != 0) {
        return -EINVAL;
    }

    for (size_t i = 0; i < req->part_count; i++) {
        total += req->parts[i].len;
    }

    chunk = malloc(CLOUD_READ_CHUNK);
    http = esp_http_client_init(&cfg);

    if (chunk == NULL || http == NULL) {
        free(chunk);
        esp_http_client_cleanup(http);

        return -ENOMEM;
    }

    esp_http_client_set_header(http, req->auth_header, req->auth_value);
    esp_http_client_set_header(http, "Content-Type", req->content_type);

    if (esp_http_client_open(http, (int)total) != ESP_OK) {
        goto done;
    }

    for (size_t i = 0; i < req->part_count; i++) {
        /* SAFETY: parts point at caller-owned bytes of the stated length. */
        if (esp_http_client_write(http, (const char *)req->parts[i].ptr, (int)req->parts[i].len)
            != (int)req->parts[i].len) {
            goto done;
        }
    }

    if (esp_http_client_fetch_headers(http) < 0) {
        goto done;
    }

    int status = esp_http_client_get_status_code(http);

    if (status < 200 || status > 299) {
        rc = status;
        goto done;
    }

    for (;;) {
        int n = esp_http_client_read(http, chunk, CLOUD_READ_CHUNK);

        if (n < 0) {
            goto done;
        }

        if (n == 0) {
            break;
        }

        on_body(chunk, (size_t)n, ctx);
    }

    rc = 0;

done:
    esp_http_client_close(http);
    esp_http_client_cleanup(http);
    free(chunk);

    return rc;
}

static int bearer(const cloud_endpoint_t *ep, char *auth, size_t cap)
{
    return snprintf(auth, cap, "Bearer %s", ep->key) < (int)cap ? 0 : -ENAMETOOLONG;
}

static int join_url(char *url, size_t cap, const char *base, const char *path)
{
    return snprintf(url, cap, "%s%s", base, path) < (int)cap ? 0 : -ENAMETOOLONG;
}

typedef struct {
    char *buf;
    size_t len;
    size_t cap;
} cloud_collect_t;

static void collect(const char *chunk, size_t len, void *ctx)
{
    /* SAFETY: openai_stt passes its cloud_collect_t. */
    cloud_collect_t *c = (cloud_collect_t *)ctx;
    size_t take = len < c->cap - 1 - c->len ? len : c->cap - 1 - c->len;

    memcpy(c->buf + c->len, chunk, take);
    c->len += take;
    c->buf[c->len] = '\0';
}

static int openai_stt(const cloud_endpoint_t *ep, const int16_t *pcm16k, size_t samples, char *text, size_t cap)
{
    char url[CLOUD_URL_MAX];
    char auth[CLOUD_AUTH_MAX];
    char head[384];
    char tail[64];
    char reply[CLOUD_STT_REPLY_MAX];
    uint8_t wav[CLOUD_WAV_HEADER_BYTES];
    cloud_collect_t got = { .buf = reply, .len = 0, .cap = sizeof reply };
    int head_len = cloud_stt_multipart_head(head, sizeof head, CLOUD_BOUNDARY, ep->model);
    int tail_len = cloud_stt_multipart_tail(tail, sizeof tail, CLOUD_BOUNDARY);
    int rc;

    if (pcm16k == NULL || samples == 0 || head_len < 0 || tail_len < 0
        || join_url(url, sizeof url, ep->base, "/audio/transcriptions") != 0 || bearer(ep, auth, sizeof auth) != 0) {
        return -EINVAL;
    }

    cloud_wav_header(wav, (uint32_t)samples, CLOUD_STT_RATE);
    reply[0] = '\0';

    const cloud_part_t parts[] = {
        { head, (size_t)head_len },
        { wav, sizeof wav },
        { pcm16k, samples * sizeof(int16_t) },
        { tail, (size_t)tail_len },
    };
    const cloud_post_t req = {
        .url = url,
        .auth_header = "Authorization",
        .auth_value = auth,
        .content_type = "multipart/form-data; boundary=" CLOUD_BOUNDARY,
        .parts = parts,
        .part_count = sizeof parts / sizeof parts[0],
    };

    rc = cloud_post(&req, collect, &got);

    if (rc != 0) {
        return rc;
    }

    return cloud_stt_parse(reply, got.len, text, cap) >= 0 ? 0 : -EBADMSG;
}

typedef struct {
    cloud_sse_t sse;
    cloud_token_fn on_token;
    void *ctx;
} cloud_chat_stream_t;

static void chat_body(const char *chunk, size_t len, void *ctx)
{
    /* SAFETY: openai_chat passes its cloud_chat_stream_t. */
    cloud_chat_stream_t *s = (cloud_chat_stream_t *)ctx;

    (void)cloud_sse_feed(&s->sse, chunk, len, s->on_token, s->ctx);
}

static int openai_chat(const cloud_endpoint_t *ep, const char *system, const char *user, cloud_token_fn on_token,
                       void *ctx)
{
    char url[CLOUD_URL_MAX];
    char auth[CLOUD_AUTH_MAX];
    char *body = malloc(CLOUD_BODY_MAX);
    cloud_chat_stream_t *stream = malloc(sizeof *stream);
    int body_len = body != NULL ? cloud_chat_body(body, CLOUD_BODY_MAX, ep->model, system, user) : -1;
    int rc = -EINVAL;

    if (stream != NULL && body_len > 0 && join_url(url, sizeof url, ep->base, "/chat/completions") == 0
        && bearer(ep, auth, sizeof auth) == 0) {
        const cloud_part_t part = { body, (size_t)body_len };
        const cloud_post_t req = {
            .url = url,
            .auth_header = "Authorization",
            .auth_value = auth,
            .content_type = "application/json",
            .parts = &part,
            .part_count = 1,
        };

        cloud_sse_init(&stream->sse);
        stream->on_token = on_token;
        stream->ctx = ctx;
        rc = cloud_post(&req, chat_body, stream);
    } else if (body == NULL || stream == NULL) {
        rc = -ENOMEM;
    }

    free(stream);
    free(body);

    return rc;
}

/* Byte stream -> whole s16 samples; an odd trailing byte waits for the next chunk. */
typedef struct {
    int16_t pcm[CLOUD_READ_CHUNK / 2 + 1];
    uint8_t carry;
    bool has_carry;
    cloud_pcm_fn on_pcm;
    void *ctx;
} cloud_pcm_stream_t;

static void pcm_body(const char *chunk, size_t len, void *ctx)
{
    /* SAFETY: the TTS providers pass their cloud_pcm_stream_t. */
    cloud_pcm_stream_t *s = (cloud_pcm_stream_t *)ctx;
    /* SAFETY: s->pcm holds CLOUD_READ_CHUNK + 2 bytes, and len <= CLOUD_READ_CHUNK. */
    uint8_t *bytes = (uint8_t *)s->pcm;
    size_t n = 0;

    if (s->has_carry) {
        bytes[n++] = s->carry;
        s->has_carry = false;
    }

    memcpy(bytes + n, chunk, len);
    n += len;

    if (n % 2 != 0) {
        s->carry = bytes[n - 1];
        s->has_carry = true;
        n--;
    }

    if (n > 0) {
        s->on_pcm(s->pcm, n / 2, s->ctx);
    }
}

static int tts_post(const char *url, const char *auth_header, const char *auth_value, const char *body,
                    size_t body_len, cloud_pcm_fn on_pcm, void *ctx)
{
    cloud_pcm_stream_t *stream = malloc(sizeof *stream);
    const cloud_part_t part = { body, body_len };
    const cloud_post_t req = {
        .url = url,
        .auth_header = auth_header,
        .auth_value = auth_value,
        .content_type = "application/json",
        .parts = &part,
        .part_count = 1,
    };
    int rc;

    if (stream == NULL) {
        return -ENOMEM;
    }

    stream->has_carry = false;
    stream->on_pcm = on_pcm;
    stream->ctx = ctx;
    rc = cloud_post(&req, pcm_body, stream);
    free(stream);

    return rc;
}

static int openai_tts(const cloud_endpoint_t *ep, const char *text, cloud_pcm_fn on_pcm, void *ctx)
{
    char url[CLOUD_URL_MAX];
    char auth[CLOUD_AUTH_MAX];
    char body[CLOUD_SENTENCE_MAX * 2 + 160];
    int body_len = cloud_tts_openai_body(body, sizeof body, ep->model, ep->voice, text);

    if (body_len < 0 || join_url(url, sizeof url, ep->base, "/audio/speech") != 0
        || bearer(ep, auth, sizeof auth) != 0) {
        return -EINVAL;
    }

    return tts_post(url, "Authorization", auth, body, (size_t)body_len, on_pcm, ctx);
}

static int eleven_tts(const cloud_endpoint_t *ep, const char *text, cloud_pcm_fn on_pcm, void *ctx)
{
    char url[CLOUD_URL_MAX];
    char body[CLOUD_SENTENCE_MAX * 2 + 96];
    int body_len = cloud_tts_eleven_body(body, sizeof body, ep->model, text);

    if (body_len < 0
        || snprintf(url, sizeof url, "%s/v1/text-to-speech/%s?output_format=pcm_24000", ep->base, ep->voice)
               >= (int)sizeof url) {
        return -EINVAL;
    }

    return tts_post(url, "xi-api-key", ep->key, body, (size_t)body_len, on_pcm, ctx);
}

const cloud_provider_t cloud_openai_compat = {
    .name = "openai-compatible",
    .stt = openai_stt,
    .chat = openai_chat,
    .tts = openai_tts,
};

const cloud_provider_t cloud_elevenlabs = {
    .name = "elevenlabs",
    .stt = NULL,
    .chat = NULL,
    .tts = eleven_tts,
};

static int endpoint(cloud_endpoint_t *ep, const char *base, const char *model, const char *voice, const char *slot)
{
    if (strlen(base) > SKWOOP_BASE_URL_MAX || strlen(model) >= sizeof ep->model || strlen(voice) >= sizeof ep->voice) {
        return -ENAMETOOLONG;
    }

    strcpy(ep->base, base);
    strcpy(ep->model, model);
    strcpy(ep->voice, voice);

    return skwoop_config_get_api_key(slot, ep->key, sizeof ep->key) == 0 && ep->key[0] != '\0' ? 0 : -EACCES;
}

int cloud_load(cloud_t *cloud)
{
    char llm_base[SKWOOP_BASE_URL_MAX + 1];
    int rc;

    if (cloud == NULL) {
        return -EINVAL;
    }

    memset(cloud, 0, sizeof *cloud);

    if (skwoop_config_get_provider_base(llm_base, sizeof llm_base) != 0 || llm_base[0] == '\0') {
        strcpy(llm_base, CONFIG_SKWOOP_CLOUD_LLM_BASE);
    }

    cloud->stt_provider = &cloud_openai_compat;
    cloud->llm_provider = &cloud_openai_compat;
#if CONFIG_SKWOOP_CLOUD_TTS_ELEVENLABS
    cloud->tts_provider = &cloud_elevenlabs;
#else
    cloud->tts_provider = &cloud_openai_compat;
#endif

    rc = endpoint(&cloud->stt, CONFIG_SKWOOP_CLOUD_STT_BASE, CONFIG_SKWOOP_CLOUD_STT_MODEL, "",
                  CONFIG_SKWOOP_CLOUD_STT_KEY);

    if (rc == 0) {
        rc = endpoint(&cloud->llm, llm_base, CONFIG_SKWOOP_CLOUD_LLM_MODEL, "", CONFIG_SKWOOP_CLOUD_LLM_KEY);
    }

    if (rc == 0) {
        rc = endpoint(&cloud->tts, CONFIG_SKWOOP_CLOUD_TTS_BASE, CONFIG_SKWOOP_CLOUD_TTS_MODEL,
                      CONFIG_SKWOOP_CLOUD_TTS_VOICE, CONFIG_SKWOOP_CLOUD_TTS_KEY);
    }

    return rc;
}

int cloud_stt(const cloud_t *cloud, const int16_t *pcm16k, size_t samples, char *text, size_t cap)
{
    return cloud->stt_provider->stt != NULL ? cloud->stt_provider->stt(&cloud->stt, pcm16k, samples, text, cap)
                                            : -ENOTSUP;
}

int cloud_chat(const cloud_t *cloud, const char *system, const char *user, cloud_token_fn on_token, void *ctx)
{
    return cloud->llm_provider->chat != NULL
               ? cloud->llm_provider->chat(&cloud->llm, system, user, on_token, ctx)
               : -ENOTSUP;
}

int cloud_tts(const cloud_t *cloud, const char *text, cloud_pcm_fn on_pcm, void *ctx)
{
    return cloud->tts_provider->tts != NULL ? cloud->tts_provider->tts(&cloud->tts, text, on_pcm, ctx) : -ENOTSUP;
}
