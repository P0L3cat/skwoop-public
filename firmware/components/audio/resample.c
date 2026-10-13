#include "resample.h"

#include <string.h>

#define RESAMPLE_PHASE_TAPS (RESAMPLE_24TO16_HISTORY + 1)

static const int16_t resample_lpf_q15[RESAMPLE_PHASE_TAPS * 2] = {
    -130, -38, 213, 554, 508, -461, -2055, -2679,
    -314, 5634, 13137, 18398, 18398, 13137, 5634, -314,
    -2679, -2055, -461, 508, 554, 213, -38, -130,
};

static int16_t resample_q15(int64_t acc)
{
    int negative = acc < 0;

    if (negative) {
        acc = -acc;
    }

    acc = (acc + 16384) >> 15;

    if (negative) {
        acc = -acc;
    }

    if (acc > 32767) {
        return 32767;
    }

    if (acc < -32768) {
        return -32768;
    }

    return (int16_t)acc;
}

static int16_t resample_conv(const resample_24to16_t *st, int16_t sample)
{
    int64_t acc = 0;
    unsigned tap;

    for (tap = 0; tap < RESAMPLE_PHASE_TAPS; tap++) {
        int16_t past;
        int16_t coeff = resample_lpf_q15[st->input_phase + tap * 2];

        if (tap == 0) {
            past = sample;
        } else if (tap - 1 < st->history_len) {
            past = st->history[tap - 1];
        } else {
            past = 0;
        }

        acc += (int64_t)coeff * (int64_t)past;
    }

    return resample_q15(acc);
}

static void resample_remember(resample_24to16_t *st, int16_t sample)
{
    unsigned i;

    if (st->history_len < RESAMPLE_24TO16_HISTORY) {
        st->history_len++;
    }

    for (i = st->history_len; i > 1; i--) {
        st->history[i - 1] = st->history[i - 2];
    }

    st->history[0] = sample;
}

void resample_24to16_init(resample_24to16_t *st)
{
    if (st == NULL) {
        return;
    }

    memset(st, 0, sizeof(*st));
}

size_t resample_24to16(resample_24to16_t *st, const int16_t *in, size_t n_in,
                       int16_t *out, size_t out_cap)
{
    size_t i;
    size_t n_out = 0;

    if (st == NULL) {
        return 0;
    }

    if (n_in == 0) {
        return 0;
    }

    if (in == NULL || out == NULL) {
        return 0;
    }

    for (i = 0; i < n_in; i++) {
        if (st->input_phase != 2) {
            if (n_out >= out_cap) {
                break;
            }

            out[n_out] = resample_conv(st, in[i]);
            n_out++;
        }

        resample_remember(st, in[i]);
        st->input_phase = (uint8_t)((st->input_phase + 1) % 3);
    }

    return n_out;
}
