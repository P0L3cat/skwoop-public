#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

/* 16 kHz mono, 60 ms. opus_up_encode reads exactly this many samples. */
#define OPUS_UP_FRAME_SAMPLES 960

/* 24 kHz mono. The codec sizes one downlink frame as 60 ms, so cap must
 * be at least this many samples. Shorter packets still return their real
 * sample count. */
#define OPUS_DOWN_FRAME_SAMPLES 1440

bool opus_up_open(void);

/* Encode one uplink frame into out. Returns packet bytes, or <0. */
int opus_up_encode(const int16_t pcm[OPUS_UP_FRAME_SAMPLES], uint8_t *out, size_t cap);

bool opus_down_open(void);

/* Decode one downlink packet into pcm. cap is the sample capacity.
 * Returns samples written, or <0. */
int opus_down_decode(const uint8_t *pkt, size_t len, int16_t *pcm, size_t cap);
