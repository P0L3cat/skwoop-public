#pragma once

/* skwoop-mood v1.1 on the pod: one reply sentence in, PAD (valence, arousal,
 * dominance) and the nearest Skwoop mood out. C port of the context-free
 * path of mood-model/skwoop_mood (hashed word/char n-grams, linear head,
 * weighted nearest prototype) over an int8 table exported by
 * tools/export_mood.py. Pure C; no allocation. Text is treated as ASCII:
 * non-ASCII bytes separate tokens, and curly apostrophes become '. */

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define MOOD_TEXT_MAX 512
#define MOOD_TOKENS_MAX 160
#define MOOD_SLOTS 4096 /* open-addressing table for summed buckets */

typedef enum {
    MOOD_IDLE,
    MOOD_PLAIN,
    MOOD_HAPPY,
    MOOD_CALM,
    MOOD_SAD,
    MOOD_ALERT,
    MOOD_COUNT,
} mood_name_t;

typedef struct {
    float v;
    float a;
    float d;
} mood_pad_t;

typedef struct {
    uint32_t hash_bits;
    uint32_t char_min;
    uint32_t char_max;
    bool use_chars;
    bool use_bigrams;
    float scale[3];
    float bias[3];
    float protos[MOOD_COUNT][3];
    const int8_t *table; /* (1 << hash_bits) rows of V, A, D */
} mood_model_t;

/* Working memory for one prediction (~30 KB); keep it off small stacks. */
typedef struct {
    char norm[MOOD_TEXT_MAX + 1];
    uint16_t tok_at[MOOD_TOKENS_MAX];
    uint8_t tok_len[MOOD_TOKENS_MAX];
    bool tok_neg[MOOD_TOKENS_MAX];
    uint32_t slot_bucket[MOOD_SLOTS];
    uint16_t slot_count[MOOD_SLOTS];
    uint16_t used[MOOD_SLOTS];
    size_t n_used;
} mood_scratch_t;

/* Parse the exported blob; the model points into blob, which must outlive it. */
int mood_load(mood_model_t *m, const uint8_t *blob, size_t len);

/* Text longer than MOOD_TEXT_MAX is cut there. Returns 0, or -1 on bad args. */
int mood_predict(const mood_model_t *m, const char *text, mood_scratch_t *scratch, mood_pad_t *out);

/* Weighted (1, 1, 0.5) nearest prototype; ties go to the earlier mood. */
mood_name_t mood_nearest(const mood_model_t *m, mood_pad_t pad);

const char *mood_name(mood_name_t mood);
