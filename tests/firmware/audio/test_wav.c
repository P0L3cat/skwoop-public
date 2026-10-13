/* Host check for the WAV header parse. No ESP-IDF. */

#include "wav.h"

#include <assert.h>
#include <string.h>

static size_t make(uint8_t *b, uint16_t fmt, uint32_t rate, int list_chunk)
{
    size_t at = 0;

    memcpy(b, "RIFF\0\0\0\0WAVE", 12);
    at = 12;

    if (list_chunk) {
        memcpy(b + at, "LIST\x03\0\0\0abc\0", 12);
        at += 12;
    }

    memcpy(b + at, "fmt \x10\0\0\0", 8);
    b[at + 8] = (uint8_t)fmt;
    b[at + 9] = 0;
    b[at + 10] = 1;
    b[at + 11] = 0;
    b[at + 12] = (uint8_t)rate;
    b[at + 13] = (uint8_t)(rate >> 8);
    b[at + 14] = (uint8_t)(rate >> 16);
    b[at + 15] = 0;
    memset(b + at + 16, 0, 6);
    b[at + 22] = 16;
    b[at + 23] = 0;
    at += 24;
    memcpy(b + at, "data\x40\0\0\0", 8);

    return at + 8;
}

int main(void)
{
    uint8_t b[128];
    wav_info_t w;
    size_t n = make(b, 1, 24000, 0);

    assert(wav_parse(b, n, &w) == 0);
    assert(w.rate == 24000 && w.channels == 1 && w.bits == 16 && w.data_offset == 44 && w.data_bytes == 64);

    n = make(b, 1, 16000, 1);
    assert(wav_parse(b, n, &w) == 0 && w.rate == 16000 && w.data_offset == 56);

    n = make(b, 3, 24000, 0);
    assert(wav_parse(b, n, &w) == -1);
    n = make(b, 1, 24000, 0);
    assert(wav_parse(b, n - 1, &w) == -1);
    assert(wav_parse(b, 30, &w) == -1);
    b[0] = 'X';
    assert(wav_parse(b, n, &w) == -1);

    return 0;
}
