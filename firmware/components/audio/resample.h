#pragma once

#include <stddef.h>
#include <stdint.h>

/* 24 kHz mono int16 to 16 kHz. Three input samples produce two output samples.
 * history[] holds the samples the 24-tap polyphase still needs from earlier chunks. */
#define RESAMPLE_24TO16_HISTORY 11

typedef struct {
    int16_t history[RESAMPLE_24TO16_HISTORY];
    uint8_t history_len;
    uint8_t input_phase;
} resample_24to16_t;

void resample_24to16_init(resample_24to16_t *st);

/* Consume n_in samples. Returns the number of output samples written, which is
 * n_in * 2 / 3 once every group of three inputs has been seen. Stops before an
 * input that would need another output slot past out_cap. */
size_t resample_24to16(resample_24to16_t *st, const int16_t *in, size_t n_in,
                       int16_t *out, size_t out_cap);
