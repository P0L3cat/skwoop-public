#pragma once

/* Direct cloud for the standalone pod: STT, streaming chat, and TTS over
 * HTTPS with bring-your-own keys from encrypted NVS (skwoop_config). Each
 * role is an endpoint plus a provider; providers are small vtables so a new
 * backend is one more struct. Calls block; run them off the audio tasks. */

#include <stddef.h>
#include <stdint.h>

#include "cloud_proto.h"
#include "skwoop_config.h"

#define CLOUD_MODEL_MAX 64

typedef struct {
    char base[SKWOOP_BASE_URL_MAX + 1];
    char key[SKWOOP_API_KEY_MAX + 1];
    char model[CLOUD_MODEL_MAX];
    char voice[CLOUD_MODEL_MAX];
} cloud_endpoint_t;

/* 24 kHz mono s16 PCM as it arrives. */
typedef void (*cloud_pcm_fn)(const int16_t *pcm, size_t samples, void *ctx);

/* Every role returns 0 on success, an HTTP status (>0) on a non-2xx reply,
 * or a negative errno. A NULL role means the provider does not offer it. */
typedef struct {
    const char *name;
    int (*stt)(const cloud_endpoint_t *ep, const int16_t *pcm16k, size_t samples, char *text, size_t cap);
    int (*chat)(const cloud_endpoint_t *ep, const char *system, const char *user, cloud_token_fn on_token,
                void *ctx);
    int (*tts)(const cloud_endpoint_t *ep, const char *text, cloud_pcm_fn on_pcm, void *ctx);
} cloud_provider_t;

extern const cloud_provider_t cloud_openai_compat;

extern const cloud_provider_t cloud_elevenlabs;

typedef struct {
    cloud_endpoint_t stt;
    cloud_endpoint_t llm;
    cloud_endpoint_t tts;
    const cloud_provider_t *stt_provider;
    const cloud_provider_t *llm_provider;
    const cloud_provider_t *tts_provider;
} cloud_t;

/* Fill from Kconfig defaults, the stored provider base, and the NVS key
 * slots. Returns -EACCES when a role has no key stored. */
int cloud_load(cloud_t *cloud);

int cloud_stt(const cloud_t *cloud, const int16_t *pcm16k, size_t samples, char *text, size_t cap);

int cloud_chat(const cloud_t *cloud, const char *system, const char *user, cloud_token_fn on_token, void *ctx);

int cloud_tts(const cloud_t *cloud, const char *text, cloud_pcm_fn on_pcm, void *ctx);
