#include "wav.h"

#include <string.h>

static uint32_t rd32(const uint8_t *p)
{
    return (uint32_t)p[0] | (uint32_t)p[1] << 8 | (uint32_t)p[2] << 16 | (uint32_t)p[3] << 24;
}

static uint16_t rd16(const uint8_t *p)
{
    return (uint16_t)(p[0] | p[1] << 8);
}

int wav_parse(const uint8_t *head, size_t len, wav_info_t *out)
{
    size_t at = 12;
    int have_fmt = 0;

    if (head == NULL || out == NULL || len < 12 || memcmp(head, "RIFF", 4) != 0 || memcmp(head + 8, "WAVE", 4) != 0) {
        return -1;
    }

    memset(out, 0, sizeof *out);

    while (at + 8 <= len) {
        uint32_t size = rd32(head + at + 4);
        const uint8_t *body = head + at + 8;

        if (memcmp(head + at, "fmt ", 4) == 0) {
            if (size < 16 || at + 8 + 16 > len || rd16(body) != 1) {
                return -1;
            }

            out->channels = rd16(body + 2);
            out->rate = rd32(body + 4);
            out->bits = rd16(body + 14);
            have_fmt = 1;
        } else if (memcmp(head + at, "data", 4) == 0) {
            if (!have_fmt || out->channels == 0 || out->bits == 0) {
                return -1;
            }

            out->data_offset = (uint32_t)(at + 8);
            out->data_bytes = size;

            return 0;
        }

        if (size > len) {
            return -1;
        }

        at += 8 + size + (size & 1u);
    }

    return -1;
}
