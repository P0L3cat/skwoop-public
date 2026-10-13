/* Host check for 24 kHz -> 16 kHz resampling. No ESP-IDF.
 * cc -Wall -Wextra -Werror -I../../../firmware/components/audio ../../../firmware/components/audio/resample.c test_resample.c -lm
 */

#include "resample.h"

#include <assert.h>
#include <math.h>
#include <stddef.h>
#include <stdint.h>

#define SINE_AMP 10000
#define SINE_N 2400

static const double k_pi = 3.14159265358979323846;

static int16_t sine_24k(size_t i)
{
    double y = sin(2.0 * k_pi * 1000.0 * (double)i / 24000.0) * (double)SINE_AMP;

    if (y >= 0.0) {
        return (int16_t)(y + 0.5);
    }

    return (int16_t)(y - 0.5);
}

static int32_t peak_abs(const int16_t *x, size_t n)
{
    size_t i;
    int32_t peak = 0;

    for (i = 0; i < n; i++) {
        int32_t a = x[i];

        if (a < 0) {
            a = -a;
        }

        if (a > peak) {
            peak = a;
        }
    }

    return peak;
}

static void test_block_length_and_level(void)
{
    resample_24to16_t st;
    int16_t in[SINE_N];
    int16_t out[SINE_N];
    size_t i;
    size_t n_out;
    int32_t peak;

    for (i = 0; i < SINE_N; i++) {
        in[i] = sine_24k(i);
    }

    resample_24to16_init(&st);
    n_out = resample_24to16(&st, in, SINE_N, out, SINE_N);
    peak = peak_abs(out, n_out);

    assert(n_out == SINE_N * 2 / 3);
    assert(peak >= (SINE_AMP * 95) / 100);
    assert(peak <= (SINE_AMP * 105) / 100);
}

static void test_odd_chunks_seamless(void)
{
    resample_24to16_t whole;
    resample_24to16_t parts;
    int16_t in[SINE_N];
    int16_t one[SINE_N];
    int16_t many[SINE_N];
    size_t i;
    size_t offset = 0;
    size_t n_one;
    size_t n_many = 0;
    size_t chunk = 1;
    int32_t peak;
    double expected = 2.0 * (double)SINE_AMP * sin(k_pi * 1000.0 / 16000.0);

    for (i = 0; i < SINE_N; i++) {
        in[i] = sine_24k(i);
    }

    resample_24to16_init(&whole);
    n_one = resample_24to16(&whole, in, SINE_N, one, SINE_N);

    resample_24to16_init(&parts);

    while (offset < SINE_N) {
        size_t left = SINE_N - offset;
        size_t n = chunk;
        size_t boundary = n_many;
        size_t produced;

        if (n > left) {
            n = left;
        }

        if ((n % 2) == 0) {
            n--;
        }

        produced = resample_24to16(&parts, in + offset, n, many + n_many, SINE_N - n_many);

        if (boundary > 0 && produced > 0) {
            int32_t step = (int32_t)many[boundary] - (int32_t)many[boundary - 1];

            if (step < 0) {
                step = -step;
            }

            assert((double)step <= expected);
        }

        n_many += produced;
        offset += n;
        chunk += 2;

        if (chunk > 31) {
            chunk = 1;
        }
    }

    assert(n_many == SINE_N * 2 / 3);
    assert(n_many == n_one);

    for (i = 0; i < n_many; i++) {
        assert(many[i] == one[i]);
    }

    peak = peak_abs(many, n_many);
    assert(peak >= (SINE_AMP * 95) / 100);
    assert(peak <= (SINE_AMP * 105) / 100);
}

int main(void)
{
    test_block_length_and_level();
    test_odd_chunks_seamless();

    return 0;
}
