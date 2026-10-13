#pragma once

/* RIFF/WAVE header parse for cached clips. Pure C. */

#include <stddef.h>
#include <stdint.h>

typedef struct {
    uint32_t rate;
    uint16_t channels;
    uint16_t bits;
    uint32_t data_offset;
    uint32_t data_bytes;
} wav_info_t;

/* head holds the first len bytes of the file. Accepts PCM (format 1) only.
 * Returns 0, or -1 when the header is not complete PCM WAV within len. */
int wav_parse(const uint8_t *head, size_t len, wav_info_t *out);
