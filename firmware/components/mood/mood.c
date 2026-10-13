#include "mood.h"

#include <math.h>
#include <stdio.h>
#include <string.h>

#define MOOD_MAGIC "MOOD"
#define MOOD_VERSION 1u
#define MOOD_HEADER_BYTES (4 + 5 * 4 + 3 * 4 + 3 * 4 + MOOD_COUNT * 3 * 4)
#define MOOD_KEY_MAX 160
#define MOOD_LEN_CAP 40

static const char *const NEGATORS[] = {
    "no", "not", "never", "without", "zero", "nothing", "none", "nobody", "neither", "nor", "cannot",
};

static uint32_t rd_u32(const uint8_t *p)
{
    return (uint32_t)p[0] | (uint32_t)p[1] << 8 | (uint32_t)p[2] << 16 | (uint32_t)p[3] << 24;
}

static float rd_f32(const uint8_t *p)
{
    uint32_t bits = rd_u32(p);
    float f;

    memcpy(&f, &bits, sizeof f);

    return f;
}

int mood_load(mood_model_t *m, const uint8_t *blob, size_t len)
{
    const uint8_t *p = blob + 4;
    uint32_t flags;

    if (m == NULL || blob == NULL || len < MOOD_HEADER_BYTES || memcmp(blob, MOOD_MAGIC, 4) != 0
        || rd_u32(p) != MOOD_VERSION) {
        return -1;
    }

    m->hash_bits = rd_u32(p + 4);
    m->char_min = rd_u32(p + 8);
    m->char_max = rd_u32(p + 12);
    flags = rd_u32(p + 16);
    p += 20;

    if (m->hash_bits < 4 || m->hash_bits > 22 || m->char_min < 1 || m->char_max < m->char_min
        || m->char_max > 8 || len != MOOD_HEADER_BYTES + ((size_t)3 << m->hash_bits)) {
        return -1;
    }

    m->use_chars = (flags & 1u) != 0;
    m->use_bigrams = (flags & 2u) != 0;

    for (int c = 0; c < 3; c++, p += 4) {
        m->scale[c] = rd_f32(p);
    }

    for (int c = 0; c < 3; c++, p += 4) {
        m->bias[c] = rd_f32(p);
    }

    for (int k = 0; k < MOOD_COUNT; k++) {
        for (int c = 0; c < 3; c++, p += 4) {
            m->protos[k][c] = rd_f32(p);
        }
    }

    /* SAFETY: the table is the rest of the blob, length checked above;
     * int8_t and uint8_t share size and alignment. */
    m->table = (const int8_t *)(blob + MOOD_HEADER_BYTES);

    return 0;
}

static uint32_t crc32(const char *s, size_t n)
{
    uint32_t crc = 0xFFFFFFFFu;

    for (size_t i = 0; i < n; i++) {
        crc ^= (uint8_t)s[i];

        for (int k = 0; k < 8; k++) {
            crc = (crc >> 1) ^ (0xEDB88320u & (0u - (crc & 1u)));
        }
    }

    return ~crc;
}

static void add_key(const mood_model_t *m, mood_scratch_t *s, const char *key, size_t n)
{
    uint32_t bucket = crc32(key, n) & ((1u << m->hash_bits) - 1u);
    uint32_t slot = (bucket * 2654435761u) & (MOOD_SLOTS - 1u);

    while (s->slot_count[slot] != 0 && s->slot_bucket[slot] != bucket) {
        slot = (slot + 1u) & (MOOD_SLOTS - 1u);
    }

    if (s->slot_count[slot] == 0) {
        if (s->n_used >= MOOD_SLOTS - 1) {
            return;
        }

        s->slot_bucket[slot] = bucket;
        s->used[s->n_used++] = (uint16_t)slot;
    }

    s->slot_count[slot]++;
}

/* key = prefix + up to three parts; dropped if it would not fit. */
static void add_key3(const mood_model_t *m, mood_scratch_t *s, const char *prefix, const char *a, size_t an,
                     const char *b, size_t bn, const char *c, size_t cn)
{
    char key[MOOD_KEY_MAX];
    size_t pn = strlen(prefix);
    size_t n = pn + an + bn + cn;

    if (n > sizeof key) {
        return;
    }

    memcpy(key, prefix, pn);
    memcpy(key + pn, a, an);
    memcpy(key + pn + an, b, bn);
    memcpy(key + pn + an + bn, c, cn);
    add_key(m, s, key, n);
}

static bool is_clause_end(char c)
{
    return c == '.' || c == ',' || c == ';' || c == ':' || c == '!' || c == '?';
}

static bool is_word_char(char c)
{
    return (c >= 'a' && c <= 'z') || (c >= '0' && c <= '9');
}

/* Lower-case ASCII, curly quotes to ', digits to 0. */
static void normalize(const char *text, char *out)
{
    size_t o = 0;

    for (size_t i = 0; text[i] != '\0' && o < MOOD_TEXT_MAX; i++) {
        unsigned char c = (unsigned char)text[i];

        if (c == 0xE2 && (unsigned char)text[i + 1] == 0x80
            && ((unsigned char)text[i + 2] == 0x98 || (unsigned char)text[i + 2] == 0x99)) {
            out[o++] = '\'';
            i += 2;
        } else if (c >= 'A' && c <= 'Z') {
            out[o++] = (char)(c - 'A' + 'a');
        } else if (c >= '0' && c <= '9') {
            out[o++] = '0';
        } else {
            out[o++] = (char)c;
        }
    }

    out[o] = '\0';
}

/* [a-z0-9]+(?:'[a-z]+)?|[!?.,;:] */
static size_t tokenize(mood_scratch_t *s)
{
    const char *t = s->norm;
    size_t n = 0;
    size_t i = 0;

    while (t[i] != '\0' && n < MOOD_TOKENS_MAX) {
        size_t start = i;

        if (is_word_char(t[i])) {
            while (is_word_char(t[i])) {
                i++;
            }

            if (t[i] == '\'' && t[i + 1] >= 'a' && t[i + 1] <= 'z') {
                i++;

                while (t[i] >= 'a' && t[i] <= 'z') {
                    i++;
                }
            }
        } else if (is_clause_end(t[i])) {
            i++;
        } else {
            i++;
            continue;
        }

        if (i - start > UINT8_MAX) {
            continue;
        }

        s->tok_at[n] = (uint16_t)start;
        s->tok_len[n] = (uint8_t)(i - start);
        n++;
    }

    return n;
}

static bool tok_is_punct(const mood_scratch_t *s, size_t k)
{
    return s->tok_len[k] == 1 && is_clause_end(s->norm[s->tok_at[k]]);
}

static bool is_negator(const char *w, size_t n)
{
    if (n >= 3 && memcmp(w + n - 3, "n't", 3) == 0) {
        return true;
    }

    for (size_t i = 0; i < sizeof NEGATORS / sizeof NEGATORS[0]; i++) {
        if (strlen(NEGATORS[i]) == n && memcmp(NEGATORS[i], w, n) == 0) {
            return true;
        }
    }

    return false;
}

static void mark_negation(mood_scratch_t *s, size_t ntok)
{
    bool negating = false;

    for (size_t k = 0; k < ntok; k++) {
        if (tok_is_punct(s, k)) {
            negating = false;
            s->tok_neg[k] = false;
            continue;
        }

        s->tok_neg[k] = negating;

        if (is_negator(s->norm + s->tok_at[k], s->tok_len[k])) {
            negating = true;
        }
    }
}

/* Marked token k as (prefix, text): "NEG_" + word when negated. */
static const char *neg_prefix(const mood_scratch_t *s, size_t k)
{
    return s->tok_neg[k] ? "NEG_" : "";
}

static bool has_allcaps_word(const char *text)
{
    size_t run = 0;
    bool ok = true;

    for (size_t i = 0;; i++) {
        unsigned char c = (unsigned char)text[i];
        bool space = c == ' ' || (c >= '\t' && c <= '\r') || c == '\0';

        if (space) {
            if (ok && run > 1) {
                return true;
            }

            if (c == '\0') {
                return false;
            }

            run = 0;
            ok = true;
        } else {
            ok = ok && c >= 'A' && c <= 'Z';
            run++;
        }
    }
}

static void word_keys(const mood_model_t *m, mood_scratch_t *s, size_t ntok)
{
    for (size_t k = 0; k < ntok; k++) {
        const char *pre = neg_prefix(s, k);

        add_key3(m, s, "w:", pre, strlen(pre), s->norm + s->tok_at[k], s->tok_len[k], "", 0);
    }

    if (!m->use_bigrams) {
        return;
    }

    for (size_t k = 0; k <= ntok; k++) {
        char left[MOOD_KEY_MAX / 2];
        char right[MOOD_KEY_MAX / 2];
        int ln;
        int rn;

        if (k == 0) {
            ln = snprintf(left, sizeof left, "<s>");
        } else {
            ln = snprintf(left, sizeof left, "%s%.*s", neg_prefix(s, k - 1), s->tok_len[k - 1],
                          s->norm + s->tok_at[k - 1]);
        }

        if (k == ntok) {
            rn = snprintf(right, sizeof right, "</s>");
        } else {
            rn = snprintf(right, sizeof right, "%s%.*s", neg_prefix(s, k), s->tok_len[k], s->norm + s->tok_at[k]);
        }

        if (ln > 0 && rn > 0 && (size_t)ln < sizeof left && (size_t)rn < sizeof right) {
            add_key3(m, s, "b:", left, (size_t)ln, " ", 1, right, (size_t)rn);
        }
    }
}

static void char_keys(const mood_model_t *m, mood_scratch_t *s, size_t ntok)
{
    char padded[UINT8_MAX + 3];

    for (size_t k = 0; k < ntok; k++) {
        size_t len = (size_t)s->tok_len[k] + 2;

        if (tok_is_punct(s, k)) {
            continue;
        }

        padded[0] = '<';
        memcpy(padded + 1, s->norm + s->tok_at[k], s->tok_len[k]);
        padded[len - 1] = '>';

        for (size_t n = m->char_min; n <= m->char_max; n++) {
            for (size_t i = 0; i + n <= len; i++) {
                add_key3(m, s, "c:", padded + i, n, "", 0, "", 0);
            }
        }
    }
}

int mood_predict(const mood_model_t *m, const char *text, mood_scratch_t *s, mood_pad_t *out)
{
    size_t ntok;
    size_t nwords = 0;
    char len_key[16];
    float acc[3];
    double norm = 0.0;

    if (m == NULL || text == NULL || s == NULL || out == NULL) {
        return -1;
    }

    memset(s->slot_count, 0, sizeof s->slot_count);
    s->n_used = 0;
    normalize(text, s->norm);
    ntok = tokenize(s);
    mark_negation(s, ntok);

    for (size_t k = 0; k < ntok; k++) {
        nwords += tok_is_punct(s, k) ? 0 : 1;
    }

    add_key(m, s, "b:bias", 6);
    word_keys(m, s, ntok);

    if (m->use_chars) {
        char_keys(m, s, ntok);
    }

    if (strchr(text, '!') != NULL) {
        add_key(m, s, "p:exclaim", 9);
    }

    if (strchr(text, '?') != NULL) {
        add_key(m, s, "p:question", 10);
    }

    if (has_allcaps_word(text)) {
        add_key(m, s, "p:allcaps", 9);
    }

    add_key(m, s, len_key,
            (size_t)snprintf(len_key, sizeof len_key, "len:%u",
                             (unsigned)((nwords < MOOD_LEN_CAP ? nwords : MOOD_LEN_CAP) / 5)));

    for (size_t i = 0; i < s->n_used; i++) {
        double v = 1.0 + log((double)s->slot_count[s->used[i]]);

        norm += v * v;
    }

    norm = sqrt(norm);
    acc[0] = acc[1] = acc[2] = 0.0f;

    for (size_t i = 0; i < s->n_used; i++) {
        uint16_t slot = s->used[i];
        float v = (float)((1.0 + log((double)s->slot_count[slot])) / norm);
        const int8_t *row = m->table + (size_t)s->slot_bucket[slot] * 3u;

        for (int c = 0; c < 3; c++) {
            acc[c] += v * ((float)row[c] * m->scale[c]);
        }
    }

    for (int c = 0; c < 3; c++) {
        acc[c] += m->bias[c];
        acc[c] = acc[c] < -1.0f ? -1.0f : (acc[c] > 1.0f ? 1.0f : acc[c]);
    }

    out->v = acc[0];
    out->a = acc[1];
    out->d = acc[2];

    return 0;
}

mood_name_t mood_nearest(const mood_model_t *m, mood_pad_t p)
{
    mood_name_t best = MOOD_IDLE;
    float best_dist = 0.0f;

    for (int k = 0; k < MOOD_COUNT; k++) {
        float dv = p.v - m->protos[k][0];
        float da = p.a - m->protos[k][1];
        float dd = p.d - m->protos[k][2];
        float dist = dv * dv + da * da + 0.5f * dd * dd;

        if (k == 0 || dist < best_dist) {
            best = (mood_name_t)k;
            best_dist = dist;
        }
    }

    return best;
}

const char *mood_name(mood_name_t mood)
{
    static const char *const NAMES[MOOD_COUNT] = { "idle", "plain", "happy", "calm", "sad", "alert" };

    return (unsigned)mood < MOOD_COUNT ? NAMES[mood] : "idle";
}
