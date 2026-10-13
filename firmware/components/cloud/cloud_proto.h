#pragma once

/* Pure request/response pieces for the direct-cloud path (no network, no
 * ESP-IDF): JSON bodies, the STT multipart frame, the chat SSE stream parser,
 * and the sentence splitter that feeds TTS while the LLM is still streaming.
 * Builders return bytes written (no NUL) or -1 when the output would not fit. */

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define CLOUD_WAV_HEADER_BYTES 44
#define CLOUD_SSE_LINE_MAX 2048
#define CLOUD_SENTENCE_MAX 256

int cloud_json_escape(const char *in, char *out, size_t cap);

/* OpenAI-compatible /chat/completions body with stream=true. */
int cloud_chat_body(char *out, size_t cap, const char *model, const char *system, const char *user);

/* OpenAI-compatible /audio/speech body. response_format pcm = 24 kHz s16le mono. */
int cloud_tts_openai_body(char *out, size_t cap, const char *model, const char *voice, const char *text);

/* ElevenLabs /v1/text-to-speech/{voice} body (voice goes in the URL). */
int cloud_tts_eleven_body(char *out, size_t cap, const char *model, const char *text);

/* RIFF header for mono s16le PCM. */
void cloud_wav_header(uint8_t out[CLOUD_WAV_HEADER_BYTES], uint32_t samples, uint32_t rate);

/* multipart/form-data around a WAV file part named "file" plus a "model" field.
 * The request body is head + WAV header + PCM + tail. */
int cloud_stt_multipart_head(char *out, size_t cap, const char *boundary, const char *model);

int cloud_stt_multipart_tail(char *out, size_t cap, const char *boundary);

/* {"text": "..."} from /audio/transcriptions. Returns text length, or -1 when
 * the body is not that shape or the text does not fit. */
int cloud_stt_parse(const char *json, size_t len, char *text, size_t cap);

typedef void (*cloud_token_fn)(const char *token, size_t len, void *ctx);

/* Line-buffered SSE reader for chat streaming. A line longer than
 * CLOUD_SSE_LINE_MAX is dropped whole and counted in dropped_lines. */
typedef struct {
    char line[CLOUD_SSE_LINE_MAX];
    size_t len;
    bool skipping;
    bool done;
    unsigned dropped_lines;
} cloud_sse_t;

void cloud_sse_init(cloud_sse_t *sse);

/* Calls on_token with each choices[0].delta.content. Returns true once
 * "data: [DONE]" has been seen; bytes after that are ignored. */
bool cloud_sse_feed(cloud_sse_t *sse, const char *data, size_t len, cloud_token_fn on_token, void *ctx);

typedef void (*cloud_sentence_fn)(const char *sentence, void *ctx);

/* Collects streamed tokens and emits a sentence at . ! ? or newline followed
 * by whitespace. A run with no break is cut at the last space before
 * CLOUD_SENTENCE_MAX. Emitted sentences are trimmed and never empty. */
typedef struct {
    char buf[CLOUD_SENTENCE_MAX];
    size_t len;
} cloud_splitter_t;

void cloud_split_init(cloud_splitter_t *sp);

void cloud_split_feed(cloud_splitter_t *sp, const char *token, size_t len, cloud_sentence_fn on_sentence, void *ctx);

void cloud_split_flush(cloud_splitter_t *sp, cloud_sentence_fn on_sentence, void *ctx);
