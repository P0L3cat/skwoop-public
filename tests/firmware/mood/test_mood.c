/* Host check for the C mood port against tools/export_mood.py output:
 * PAD must match the dequantized-int8 numpy result (featurizer parity), and
 * mood names are compared with the float model (int8 cost). */

#include "mood.h"

#include <assert.h>
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#define PAD_TOL 2e-4f

static uint8_t *read_file(const char *path, size_t *len)
{
    FILE *f = fopen(path, "rb");
    uint8_t *buf;

    assert(f != NULL);
    fseek(f, 0, SEEK_END);
    *len = (size_t)ftell(f);
    fseek(f, 0, SEEK_SET);
    buf = malloc(*len);
    assert(buf != NULL && fread(buf, 1, *len, f) == *len);
    fclose(f);

    return buf;
}

static mood_name_t parse_name(const char *s)
{
    for (int k = 0; k < MOOD_COUNT; k++) {
        if (strcmp(s, mood_name((mood_name_t)k)) == 0) {
            return (mood_name_t)k;
        }
    }

    assert(0 && "unknown mood");

    return MOOD_IDLE;
}

int main(int argc, char **argv)
{
    static mood_scratch_t scratch;
    static char line[2048];
    const char *bin_path = argc > 1 ? argv[1] : "../../firmware/components/mood/mood_v11.bin";
    const char *gold_path = argc > 2 ? argv[2] : "mood/golden.tsv";
    size_t len;
    uint8_t *blob;
    FILE *gold;
    FILE *probe;
    mood_model_t model;
    int total = 0;
    int pad_ok = 0;
    int name_agree = 0;
    float worst = 0.0f;
    double t_total = 0.0;

    probe = fopen(bin_path, "rb");

    if (probe == NULL) {
        fprintf(stderr,
                "skip mood: %s absent (weights are not shipped). "
                "Generate components/mood/mood_v11.bin with firmware/tools/export_mood.py\n",
                bin_path);
        return 0;
    }

    fclose(probe);
    blob = read_file(bin_path, &len);
    gold = fopen(gold_path, "r");

    assert(mood_load(&model, blob, len) == 0);
    assert(mood_load(&model, blob, len - 1) == -1);
    blob[0] ^= 1;
    assert(mood_load(&model, blob, len) == -1);
    blob[0] ^= 1;
    assert(mood_load(&model, blob, len) == 0);
    assert(gold != NULL);

    while (fgets(line, sizeof line, gold) != NULL) {
        char *text = line;
        char *f[4];
        mood_pad_t want;
        mood_pad_t got;
        struct timespec t0;
        struct timespec t1;
        float err;

        if (line[0] == '#') {
            continue;
        }

        line[strcspn(line, "\n")] = '\0';
        f[0] = strchr(text, '\t');
        assert(f[0] != NULL);
        *f[0]++ = '\0';

        for (int i = 1; i < 4; i++) {
            f[i] = strchr(f[i - 1], '\t');
            assert(f[i] != NULL);
            *f[i]++ = '\0';
        }

        want.v = strtof(f[0], NULL);
        want.a = strtof(f[1], NULL);
        want.d = strtof(f[2], NULL);
        clock_gettime(CLOCK_MONOTONIC, &t0);
        assert(mood_predict(&model, text, &scratch, &got) == 0);
        clock_gettime(CLOCK_MONOTONIC, &t1);
        t_total += (double)(t1.tv_sec - t0.tv_sec) * 1e6 + (double)(t1.tv_nsec - t0.tv_nsec) / 1e3;
        err = fmaxf(fabsf(got.v - want.v), fmaxf(fabsf(got.a - want.a), fabsf(got.d - want.d)));
        worst = fmaxf(worst, err);

        if (err <= PAD_TOL) {
            pad_ok++;
        } else {
            printf("PAD mismatch %.5f: %s\n", err, text);
        }

        name_agree += mood_nearest(&model, got) == parse_name(f[3]);
        total++;
    }

    fclose(gold);
    printf("mood: %d lines, PAD parity %d/%d (worst %.2g), int8 vs float mood %d/%d, %.1f us/line (host)\n", total,
           pad_ok, total, worst, name_agree, total, t_total / total);
    assert(total > 100);
    assert(pad_ok == total);
    assert(name_agree * 100 >= total * 97);
    free(blob);

    return 0;
}
