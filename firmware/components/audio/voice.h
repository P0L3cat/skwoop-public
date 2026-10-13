#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "esp_err.h"

esp_err_t voice_start(void);

void voice_set_listening(bool on);

void voice_feed_opus(const uint8_t *pkt, size_t len);

uint8_t voice_level(void);

/* 16 kHz mono AEC frame (OPUS_UP_FRAME_SAMPLES) handed over while listening. */
typedef void (*voice_frame_fn)(const int16_t *pcm, size_t samples);

/* Non-NULL: listening frames go to fn (on the uplink task) instead of the
 * bridge. NULL restores the bridge uplink. */
void voice_set_capture(voice_frame_fn fn);

/* Play 24 kHz mono PCM from one caller task at a time; updates voice_level. */
void voice_play_pcm(const int16_t *pcm, size_t samples);
