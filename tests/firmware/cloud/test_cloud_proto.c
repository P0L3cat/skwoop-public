/* Host checks for the direct-cloud protocol pieces, plus a code-path timing
 * of the streaming chat -> sentence path. No network, no ESP-IDF. */

#include "cloud_proto.h"

#include <assert.h>
#include <stdio.h>
#include <string.h>
#include <time.h>

typedef struct {
    char text[4096];
    size_t len;
    int tokens;
} token_sink_t;

typedef struct {
    char list[16][CLOUD_SENTENCE_MAX];
    int count;
} sentence_sink_t;

static void on_token(const char *token, size_t len, void *ctx)
{
    /* SAFETY: every caller in this file passes a token_sink_t. */
    token_sink_t *sink = (token_sink_t *)ctx;

    assert(sink->len + len < sizeof sink->text);
    memcpy(sink->text + sink->len, token, len);
    sink->len += len;
    sink->text[sink->len] = '\0';
    sink->tokens++;
}

static void on_sentence(const char *sentence, void *ctx)
{
    /* SAFETY: every caller in this file passes a sentence_sink_t. */
    sentence_sink_t *sink = (sentence_sink_t *)ctx;

    assert(sink->count < 16);
    assert(strlen(sentence) > 0 && strlen(sentence) < CLOUD_SENTENCE_MAX);
    strcpy(sink->list[sink->count++], sentence);
}

static void test_escape_and_bodies(void)
{
    char out[256];

    assert(cloud_json_escape("a\"b\\c\nd\x01", out, sizeof out) == 16);
    assert(strcmp(out, "a\\\"b\\\\c\\nd\\u0001") == 0);
    assert(cloud_json_escape("toolong", out, 4) == -1);

    assert(cloud_chat_body(out, sizeof out, "m", "sys \"x\"", "hi") > 0);
    assert(strcmp(out,
                  "{\"model\":\"m\",\"stream\":true,\"messages\":[{\"role\":\"system\",\"content\":\"sys \\\"x\\\"\"},"
                  "{\"role\":\"user\",\"content\":\"hi\"}]}")
           == 0);
    assert(cloud_chat_body(out, 20, "m", "s", "u") == -1);

    assert(cloud_tts_openai_body(out, sizeof out, "tts-1", "alloy", "Hi.") > 0);
    assert(strcmp(out, "{\"model\":\"tts-1\",\"voice\":\"alloy\",\"input\":\"Hi.\",\"response_format\":\"pcm\"}") == 0);

    assert(cloud_tts_eleven_body(out, sizeof out, "eleven_flash_v2_5", "Hi.") > 0);
    assert(strcmp(out, "{\"text\":\"Hi.\",\"model_id\":\"eleven_flash_v2_5\"}") == 0);
}

static void test_wav_and_multipart(void)
{
    uint8_t wav[CLOUD_WAV_HEADER_BYTES];
    char head[256];
    char tail[64];

    cloud_wav_header(wav, 16000, 16000);
    assert(memcmp(wav, "RIFF", 4) == 0 && memcmp(wav + 8, "WAVEfmt ", 8) == 0);
    assert(wav[40] == 0x00 && wav[41] == 0x7d && wav[42] == 0x00);
    assert(wav[24] == 0x80 && wav[25] == 0x3e);

    assert(cloud_stt_multipart_head(head, sizeof head, "B", "whisper-1") > 0);
    assert(strstr(head, "--B\r\nContent-Disposition: form-data; name=\"model\"\r\n\r\nwhisper-1\r\n") == head);
    assert(strstr(head, "filename=\"say.wav\"\r\nContent-Type: audio/wav\r\n\r\n") != NULL);
    assert(cloud_stt_multipart_tail(tail, sizeof tail, "B") == 9);
    assert(cloud_stt_multipart_head(head, 16, "B", "whisper-1") == -1);
}

static void test_stt_parse(void)
{
    char text[16];
    const char *ok = "{\"text\":\"set a timer\"}";
    const char *bad = "{\"error\":{\"message\":\"no\"}}";

    assert(cloud_stt_parse(ok, strlen(ok), text, sizeof text) == 11 && strcmp(text, "set a timer") == 0);
    assert(cloud_stt_parse(bad, strlen(bad), text, sizeof text) == -1 && text[0] == '\0');
    assert(cloud_stt_parse(ok, strlen(ok), text, 8) == -1);
    assert(cloud_stt_parse("{\"text\":", 8, text, sizeof text) == -1);
}

static const char *STREAM =
    "data: {\"choices\":[{\"delta\":{\"role\":\"assistant\"}}]}\n\n"
    "data: {\"choices\":[{\"delta\":{\"content\":\"Hello\"}}]}\r\n\r\n"
    ": keep-alive\n\n"
    "data: {\"choices\":[{\"delta\":{\"content\":\" there. How\"}}]}\n\n"
    "data: {\"choices\":[{\"delta\":{\"content\":\" are you?\"}}]}\n\n"
    "data: [DONE]\n\n"
    "data: {\"choices\":[{\"delta\":{\"content\":\"ignored\"}}]}\n\n";

static void test_sse_byte_by_byte(void)
{
    cloud_sse_t sse;
    token_sink_t sink = { .len = 0 };
    bool done = false;

    cloud_sse_init(&sse);

    for (size_t i = 0; STREAM[i] != '\0'; i++) {
        done = cloud_sse_feed(&sse, STREAM + i, 1, on_token, &sink);
    }

    assert(done);
    assert(sink.tokens == 3);
    assert(strcmp(sink.text, "Hello there. How are you?") == 0);
}

static void test_sse_long_line_dropped(void)
{
    static char big[CLOUD_SSE_LINE_MAX + 64];
    cloud_sse_t sse;
    token_sink_t sink = { .len = 0 };
    const char *next = "data: {\"choices\":[{\"delta\":{\"content\":\"ok\"}}]}\n";

    memset(big, 'x', sizeof big - 1);
    big[sizeof big - 2] = '\n';
    big[sizeof big - 1] = '\0';
    cloud_sse_init(&sse);
    assert(!cloud_sse_feed(&sse, big, strlen(big), on_token, &sink));
    assert(!cloud_sse_feed(&sse, next, strlen(next), on_token, &sink));
    assert(sse.dropped_lines == 1);
    assert(strcmp(sink.text, "ok") == 0);
}

static void test_splitter(void)
{
    cloud_splitter_t sp;
    sentence_sink_t sink = { .count = 0 };
    const char *parts[] = { "Hello", " there.", " Version 2.5 is", " out!", " Is it?", "\nTail" };
    char longrun[400];

    cloud_split_init(&sp);

    for (size_t i = 0; i < sizeof parts / sizeof parts[0]; i++) {
        cloud_split_feed(&sp, parts[i], strlen(parts[i]), on_sentence, &sink);
    }

    cloud_split_flush(&sp, on_sentence, &sink);
    assert(sink.count == 4);
    assert(strcmp(sink.list[0], "Hello there.") == 0);
    assert(strcmp(sink.list[1], "Version 2.5 is out!") == 0);
    assert(strcmp(sink.list[2], "Is it?") == 0);
    assert(strcmp(sink.list[3], "Tail") == 0);

    sink.count = 0;
    memset(longrun, 'a', sizeof longrun - 1);
    longrun[sizeof longrun - 1] = '\0';
    longrun[100] = ' ';
    cloud_split_feed(&sp, longrun, strlen(longrun), on_sentence, &sink);
    cloud_split_flush(&sp, on_sentence, &sink);
    assert(sink.count == 3);
    assert(strlen(sink.list[0]) == 100);
    assert(strlen(sink.list[1]) == CLOUD_SENTENCE_MAX - 1);
    assert(strlen(sink.list[2]) == 398 - 100 - (CLOUD_SENTENCE_MAX - 1));
}

typedef struct {
    cloud_splitter_t sp;
    sentence_sink_t sentences;
} pipeline_t;

static void pipeline_token(const char *token, size_t len, void *ctx)
{
    /* SAFETY: bench_stream passes its pipeline_t. */
    pipeline_t *p = (pipeline_t *)ctx;

    cloud_split_feed(&p->sp, token, len, on_sentence, &p->sentences);
}

static double now_us(void)
{
    struct timespec ts;

    clock_gettime(CLOCK_MONOTONIC, &ts);

    return (double)ts.tv_sec * 1e6 + (double)ts.tv_nsec / 1e3;
}

/* Code-path latency of a 3-sentence, ~60-token reply arriving in 512-byte
 * TCP reads: SSE parse + JSON delta + sentence split. Network and model
 * time are not included; they need hardware and a key. */
static void bench_stream(void)
{
    static char stream[16384];
    size_t at = 0;
    const char *words = "Sure thing. I set a timer for ten minutes and I will tell you when it ends. Anything else? ";
    int iters = 2000;
    double t0;
    double total;
    double first_sentence = 0;

    for (const char *w = words; *w != '\0';) {
        const char *end = strchr(w + 1, ' ');
        size_t n = end != NULL ? (size_t)(end - w) : strlen(w);

        at += (size_t)snprintf(stream + at, sizeof stream - at,
                               "data: {\"id\":\"c1\",\"object\":\"chat.completion.chunk\",\"choices\":[{\"index\":0,"
                               "\"delta\":{\"content\":\"%.*s\"}}]}\n\n",
                               (int)n, w);
        w += n;
    }

    at += (size_t)snprintf(stream + at, sizeof stream - at, "data: [DONE]\n\n");

    t0 = now_us();

    for (int k = 0; k < iters; k++) {
        cloud_sse_t sse;
        pipeline_t p;
        double start = now_us();

        cloud_sse_init(&sse);
        cloud_split_init(&p.sp);
        p.sentences.count = 0;

        for (size_t off = 0; off < at; off += 512) {
            size_t n = at - off < 512 ? at - off : 512;

            cloud_sse_feed(&sse, stream + off, n, pipeline_token, &p);

            if (k == 0 && first_sentence == 0 && p.sentences.count > 0) {
                first_sentence = now_us() - start;
            }
        }

        cloud_split_flush(&p.sp, on_sentence, &p.sentences);
        assert(p.sentences.count == 3);
    }

    total = (now_us() - t0) / iters;
    printf("cloud: stream %zu bytes, parse+split %.1f us/reply (host), first sentence after %.1f us\n", at, total,
           first_sentence);
}

int main(void)
{
    test_escape_and_bodies();
    test_wav_and_multipart();
    test_stt_parse();
    test_sse_byte_by_byte();
    test_sse_long_line_dropped();
    test_splitter();
    bench_stream();
    puts("cloud ok");

    return 0;
}
