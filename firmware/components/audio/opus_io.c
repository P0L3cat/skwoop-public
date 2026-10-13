#include "opus_io.h"

#include <limits.h>
#include <stdint.h>

#include "opus.h"

#define OPUS_UP_RATE_HZ 16000
#define OPUS_UP_FRAME_MS 60
#define OPUS_UP_BITRATE 24000
#define OPUS_UP_COMPLEXITY 0

#define OPUS_DOWN_RATE_HZ 24000
#define OPUS_DOWN_FRAME_MS 60

_Static_assert(OPUS_UP_FRAME_SAMPLES == OPUS_UP_RATE_HZ * OPUS_UP_FRAME_MS / 1000,
               "uplink frame");
_Static_assert(OPUS_DOWN_FRAME_SAMPLES == OPUS_DOWN_RATE_HZ * OPUS_DOWN_FRAME_MS / 1000,
               "downlink frame");

static OpusEncoder *opus_up;

static OpusDecoder *opus_down;

bool opus_up_open(void)
{
    int err = OPUS_OK;

    if (opus_up != NULL) {
        return true;
    }

    opus_up = opus_encoder_create(OPUS_UP_RATE_HZ, 1, OPUS_APPLICATION_VOIP, &err);

    if (err != OPUS_OK || opus_up == NULL) {
        opus_up = NULL;

        return false;
    }

    opus_encoder_ctl(opus_up, OPUS_SET_BITRATE(OPUS_UP_BITRATE));
    opus_encoder_ctl(opus_up, OPUS_SET_COMPLEXITY(OPUS_UP_COMPLEXITY));
    opus_encoder_ctl(opus_up, OPUS_SET_VBR(1));
    opus_encoder_ctl(opus_up, OPUS_SET_SIGNAL(OPUS_SIGNAL_VOICE));
    opus_encoder_ctl(opus_up, OPUS_SET_MAX_BANDWIDTH(OPUS_BANDWIDTH_WIDEBAND));

    return true;
}

int opus_up_encode(const int16_t pcm[OPUS_UP_FRAME_SAMPLES], uint8_t *out, size_t cap)
{
    if (opus_up == NULL || pcm == NULL || out == NULL || cap == 0 || cap > INT32_MAX) {
        return -1;
    }

    int n = opus_encode(opus_up, pcm, OPUS_UP_FRAME_SAMPLES, out, (opus_int32)cap);

    return n > 0 ? n : -1;
}

bool opus_down_open(void)
{
    int err = OPUS_OK;

    if (opus_down != NULL) {
        return true;
    }

    opus_down = opus_decoder_create(OPUS_DOWN_RATE_HZ, 1, &err);

    if (err != OPUS_OK || opus_down == NULL) {
        opus_down = NULL;

        return false;
    }

    return true;
}

int opus_down_decode(const uint8_t *pkt, size_t len, int16_t *pcm, size_t cap)
{
    if (opus_down == NULL || pkt == NULL || pcm == NULL || len == 0 || len > INT32_MAX ||
        cap == 0 || cap > INT_MAX) {
        return -1;
    }

    int n = opus_decode(opus_down, pkt, (opus_int32)len, pcm, (int)cap, 0);

    return n > 0 ? n : -1;
}
