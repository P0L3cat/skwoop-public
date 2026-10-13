#include "skwoop_memory.h"

#include <ctype.h>
#include <errno.h>
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/time.h>
#include <time.h>

#define K1 SKWOOP_MEM_K1
#define B  SKWOOP_MEM_B

static const char *const k_stop[] = {
    "a",    "an",  "the", "and", "or",  "of",   "to",  "in",   "on",  "for",
    "is",   "it",  "be",  "as",  "at",  "by",   "from", "with", "that", "this",
    "are",  "was", "were", "but", "not", "you", "we",  "they", "he",  "she",
    "i",    "my",  "your",
};

typedef struct {
    char *id;
    char *date;
    char *tags;
    char *text;
} fact_rec;

struct skwoop_memory {
    char *dir;
    fact_rec *facts;
    int nfact;
    int cap;
    char **core;
    int ncore;
    int core_cap;
};

static int is_stop(const char *s) {
    size_t i;
    for (i = 0; i < sizeof k_stop / sizeof k_stop[0]; i++) {
        if (strcmp(s, k_stop[i]) == 0) {
            return 1;
        }
    }
    return 0;
}

static char *xstrdup(const char *s) {
    size_t n = strlen(s) + 1;
    char *p = (char *)malloc(n);
    if (p == NULL) {
        return NULL;
    }
    memcpy(p, s, n);
    return p;
}

static void fact_clear(fact_rec *f) {
    free(f->id);
    free(f->date);
    free(f->tags);
    free(f->text);
    f->id = NULL;
    f->date = NULL;
    f->tags = NULL;
    f->text = NULL;
}

static void free_facts(skwoop_memory *mem) {
    int i;
    for (i = 0; i < mem->nfact; i++) {
        fact_clear(&mem->facts[i]);
    }
    free(mem->facts);
    mem->facts = NULL;
    mem->nfact = 0;
    mem->cap = 0;
    for (i = 0; i < mem->ncore; i++) {
        free(mem->core[i]);
    }
    free(mem->core);
    mem->core = NULL;
    mem->ncore = 0;
    mem->core_cap = 0;
}

static int ensure_facts(skwoop_memory *mem) {
    fact_rec *grown;
    int cap;
    if (mem->nfact < mem->cap) {
        return SKWOOP_MEM_OK;
    }
    cap = mem->cap == 0 ? 32 : mem->cap * 2;
    grown = (fact_rec *)realloc(mem->facts, (size_t)cap * sizeof(fact_rec));
    if (grown == NULL) {
        return SKWOOP_MEM_ERR_NOMEM;
    }
    mem->facts = grown;
    mem->cap = cap;
    return SKWOOP_MEM_OK;
}

static int ensure_core(skwoop_memory *mem) {
    char **grown;
    int cap;
    if (mem->ncore < mem->core_cap) {
        return SKWOOP_MEM_OK;
    }
    cap = mem->core_cap == 0 ? 16 : mem->core_cap * 2;
    grown = (char **)realloc(mem->core, (size_t)cap * sizeof(char *));
    if (grown == NULL) {
        return SKWOOP_MEM_ERR_NOMEM;
    }
    mem->core = grown;
    mem->core_cap = cap;
    return SKWOOP_MEM_OK;
}

static int is_dir(const char *path) {
    struct stat st;
    if (stat(path, &st) != 0) {
        return 0;
    }
    return S_ISDIR(st.st_mode);
}

static int mkdir_one(const char *path) {
    if (mkdir(path, 0700) == 0 || errno == EEXIST) {
        return is_dir(path) ? SKWOOP_MEM_OK : SKWOOP_MEM_ERR_IO;
    }
    return SKWOOP_MEM_ERR_IO;
}

/* Create every path component. path is mutable and restored. */
static int mkdir_p(char *path) {
    char *p;
    size_t len = strlen(path);
    if (len == 0) {
        return SKWOOP_MEM_ERR_ARG;
    }
    for (p = path + 1; *p; p++) {
        if (*p == '/') {
            *p = '\0';
            if (mkdir_one(path) != SKWOOP_MEM_OK) {
                *p = '/';
                return SKWOOP_MEM_ERR_IO;
            }
            *p = '/';
        }
    }
    return mkdir_one(path);
}

static void strip_inplace(char *s) {
    char *start = s;
    char *end;
    size_t n;
    while (*start && isspace((unsigned char)*start)) {
        start++;
    }
    end = start + strlen(start);
    while (end > start && isspace((unsigned char)end[-1])) {
        end--;
    }
    n = (size_t)(end - start);
    memmove(s, start, n);
    s[n] = '\0';
}

static int collapse_ws(const char *in, char **out) {
    size_t cap = strlen(in) + 1;
    char *buf = (char *)malloc(cap);
    size_t n = 0;
    int sp = 0;
    const unsigned char *p;
    if (buf == NULL) {
        return SKWOOP_MEM_ERR_NOMEM;
    }
    for (p = (const unsigned char *)in; *p; p++) {
        if (isspace(*p)) {
            sp = 1;
            continue;
        }
        if (sp && n > 0) {
            buf[n++] = ' ';
        }
        sp = 0;
        buf[n++] = (char)*p;
    }
    buf[n] = '\0';
    *out = buf;
    return SKWOOP_MEM_OK;
}

/* Normalize a comma-separated tag field into "a,b,c". */
static int normalize_tags(const char *field, char **out) {
    char *copy;
    char *buf;
    size_t cap;
    size_t n = 0;
    char *save = NULL;
    char *part;
    if (field == NULL) {
        field = "";
    }
    copy = xstrdup(field);
    buf = (char *)malloc(strlen(field) + 1);
    if (copy == NULL || buf == NULL) {
        free(copy);
        free(buf);
        return SKWOOP_MEM_ERR_NOMEM;
    }
    for (part = strtok_r(copy, ",", &save); part != NULL; part = strtok_r(NULL, ",", &save)) {
        strip_inplace(part);
        if (part[0] == '\0') {
            continue;
        }
        if (n > 0) {
            buf[n++] = ',';
        }
        cap = strlen(part);
        memcpy(buf + n, part, cap);
        n += cap;
    }
    buf[n] = '\0';
    free(copy);
    *out = buf;
    return SKWOOP_MEM_OK;
}

static int upsert_fact(skwoop_memory *mem, const char *id, const char *date,
                       const char *tags_field, const char *text) {
    char *nid = NULL;
    char *ndate = NULL;
    char *ntags = NULL;
    char *ntext = NULL;
    int i;
    int rc;
    if (id == NULL || id[0] == '\0') {
        return SKWOOP_MEM_OK;
    }
    nid = xstrdup(id);
    ndate = xstrdup(date != NULL ? date : "");
    ntext = xstrdup(text != NULL ? text : "");
    rc = normalize_tags(tags_field, &ntags);
    if (nid == NULL || ndate == NULL || ntext == NULL || rc != SKWOOP_MEM_OK) {
        free(nid);
        free(ndate);
        free(ntags);
        free(ntext);
        return SKWOOP_MEM_ERR_NOMEM;
    }
    for (i = 0; i < mem->nfact; i++) {
        if (strcmp(mem->facts[i].id, nid) == 0) {
            free(mem->facts[i].date);
            free(mem->facts[i].tags);
            free(mem->facts[i].text);
            free(nid);
            mem->facts[i].date = ndate;
            mem->facts[i].tags = ntags;
            mem->facts[i].text = ntext;
            return SKWOOP_MEM_OK;
        }
    }
    rc = ensure_facts(mem);
    if (rc != SKWOOP_MEM_OK) {
        free(nid);
        free(ndate);
        free(ntags);
        free(ntext);
        return rc;
    }
    mem->facts[mem->nfact].id = nid;
    mem->facts[mem->nfact].date = ndate;
    mem->facts[mem->nfact].tags = ntags;
    mem->facts[mem->nfact].text = ntext;
    mem->nfact++;
    return SKWOOP_MEM_OK;
}

static void drop_fact(skwoop_memory *mem, const char *id) {
    int i;
    int j;
    for (i = 0; i < mem->nfact; i++) {
        if (strcmp(mem->facts[i].id, id) != 0) {
            continue;
        }
        fact_clear(&mem->facts[i]);
        for (j = i; j < mem->nfact - 1; j++) {
            mem->facts[j] = mem->facts[j + 1];
        }
        mem->nfact--;
        return;
    }
}

static int split_max(char *line, char **fields, int max_fields) {
    int n = 0;
    char *p = line;
    while (n < max_fields - 1) {
        char *bar = strchr(p, '|');
        if (bar == NULL) {
            break;
        }
        *bar = '\0';
        fields[n++] = p;
        p = bar + 1;
    }
    fields[n++] = p;
    return n;
}

static int apply_line(skwoop_memory *mem, char *line, int journal) {
    char *fields[5];
    int n;
    char *id;
    strip_inplace(line);
    if (line[0] == '\0' || line[0] == '#') {
        return SKWOOP_MEM_OK;
    }
    if (journal) {
        n = split_max(line, fields, 5);
        if (n >= 5 && strcmp(fields[0], "add") == 0) {
            id = fields[1];
            strip_inplace(id);
            strip_inplace(fields[2]);
            return upsert_fact(mem, id, fields[2], fields[3], fields[4]);
        }
        if (n >= 2 && strcmp(fields[0], "del") == 0) {
            id = fields[1];
            strip_inplace(id);
            drop_fact(mem, id);
        }
        return SKWOOP_MEM_OK;
    }
    n = split_max(line, fields, 4);
    if (n < 4) {
        return SKWOOP_MEM_OK;
    }
    id = fields[0];
    strip_inplace(id);
    strip_inplace(fields[1]);
    return upsert_fact(mem, id, fields[1], fields[2], fields[3]);
}

static int read_lines(skwoop_memory *mem, const char *name, int journal) {
    char path[1024];
    FILE *f;
    char buf[8192];
    int rc = snprintf(path, sizeof path, "%s/%s", mem->dir, name);
    if (rc < 0 || (size_t)rc >= sizeof path) {
        return SKWOOP_MEM_ERR_ARG;
    }
    f = fopen(path, "rb");
    if (f == NULL) {
        return SKWOOP_MEM_OK;
    }
    while (fgets(buf, (int)sizeof buf, f) != NULL) {
        size_t len = strlen(buf);
        /* A line longer than the buffer is truncated. Seed lines are short. */
        if (len > 0 && buf[len - 1] != '\n' && !feof(f)) {
            int ch;
            buf[sizeof buf - 1] = '\0';
            while ((ch = fgetc(f)) != EOF && ch != '\n') {
            }
        }
        rc = apply_line(mem, buf, journal);
        if (rc != SKWOOP_MEM_OK) {
            fclose(f);
            return rc;
        }
    }
    fclose(f);
    return SKWOOP_MEM_OK;
}

static int read_core(skwoop_memory *mem) {
    char path[1024];
    FILE *f;
    char buf[512];
    int rc = snprintf(path, sizeof path, "%s/core.txt", mem->dir);
    if (rc < 0 || (size_t)rc >= sizeof path) {
        return SKWOOP_MEM_ERR_ARG;
    }
    f = fopen(path, "rb");
    if (f == NULL) {
        return SKWOOP_MEM_OK;
    }
    while (fgets(buf, (int)sizeof buf, f) != NULL) {
        char *id;
        strip_inplace(buf);
        if (buf[0] == '\0' || buf[0] == '#') {
            continue;
        }
        rc = ensure_core(mem);
        if (rc != SKWOOP_MEM_OK) {
            fclose(f);
            return rc;
        }
        id = xstrdup(buf);
        if (id == NULL) {
            fclose(f);
            return SKWOOP_MEM_ERR_NOMEM;
        }
        mem->core[mem->ncore++] = id;
    }
    fclose(f);
    return SKWOOP_MEM_OK;
}

static void fill_fact(const fact_rec *src, skwoop_fact *dst) {
    snprintf(dst->id, sizeof dst->id, "%s", src->id);
    snprintf(dst->date, sizeof dst->date, "%s", src->date);
    snprintf(dst->tags, sizeof dst->tags, "%s", src->tags);
    dst->text = src->text;
}

static const fact_rec *find_fact(const skwoop_memory *mem, const char *id) {
    int i;
    for (i = 0; i < mem->nfact; i++) {
        if (strcmp(mem->facts[i].id, id) == 0) {
            return &mem->facts[i];
        }
    }
    return NULL;
}

/* ---- tokenize: lowercase [a-z0-9]+, drop apostrophes, fold, stop ---- */

static int fold_token(const char *raw, char *out, size_t out_cap) {
    size_t n = strlen(raw);
    if (n + 1 > out_cap) {
        return 0;
    }
    memcpy(out, raw, n + 1);
    if (n > 5 && strcmp(out + n - 3, "ing") == 0) {
        out[n - 3] = '\0';
        return 1;
    }
    if (n > 4 && strcmp(out + n - 2, "es") == 0) {
        out[n - 2] = '\0';
        return 1;
    }
    if (n > 3 && out[n - 1] == 's' && !(n >= 2 && out[n - 2] == 's')) {
        out[n - 1] = '\0';
        return 1;
    }
    return 1;
}

static int push_term(char ***terms, int *n, int *cap, const char *s) {
    char **grown;
    if (*n >= *cap) {
        int next = *cap == 0 ? 16 : *cap * 2;
        grown = (char **)realloc(*terms, (size_t)next * sizeof(char *));
        if (grown == NULL) {
            return SKWOOP_MEM_ERR_NOMEM;
        }
        *terms = grown;
        *cap = next;
    }
    (*terms)[*n] = xstrdup(s);
    if ((*terms)[*n] == NULL) {
        return SKWOOP_MEM_ERR_NOMEM;
    }
    (*n)++;
    return SKWOOP_MEM_OK;
}

static void free_terms(char **terms, int n) {
    int i;
    for (i = 0; i < n; i++) {
        free(terms[i]);
    }
    free(terms);
}

/* Drop ASCII and typographic apostrophes, then emit folded tokens. */
static int tokenize(const char *text, char ***terms, int *n_out) {
    char *tmp;
    size_t i;
    size_t w = 0;
    char tok[256];
    size_t tn = 0;
    char folded[256];
    char **out = NULL;
    int n = 0;
    int cap = 0;
    int rc;
    *terms = NULL;
    *n_out = 0;
    if (text == NULL) {
        return SKWOOP_MEM_OK;
    }
    tmp = (char *)malloc(strlen(text) + 1);
    if (tmp == NULL) {
        return SKWOOP_MEM_ERR_NOMEM;
    }
    for (i = 0; text[i] != '\0';) {
        unsigned char c = (unsigned char)text[i];
        if (c == '\'') {
            i++;
            continue;
        }
        /* U+2019 and U+2018 are UTF-8 E2 80 99 / E2 80 98. */
        if (c == 0xE2 && (unsigned char)text[i + 1] == 0x80 &&
            ((unsigned char)text[i + 2] == 0x99 || (unsigned char)text[i + 2] == 0x98)) {
            i += 3;
            continue;
        }
        if (c >= 'A' && c <= 'Z') {
            tmp[w++] = (char)(c - 'A' + 'a');
        } else {
            tmp[w++] = (char)c;
        }
        i++;
    }
    tmp[w] = '\0';
    for (i = 0; i <= w; i++) {
        unsigned char c = (unsigned char)tmp[i];
        int alnum = (c >= 'a' && c <= 'z') || (c >= '0' && c <= '9');
        if (alnum && tn + 1 < sizeof tok) {
            tok[tn++] = (char)c;
            continue;
        }
        if (tn == 0) {
            continue;
        }
        tok[tn] = '\0';
        tn = 0;
        if (is_stop(tok)) {
            continue;
        }
        if (!fold_token(tok, folded, sizeof folded)) {
            continue;
        }
        if (folded[0] == '\0' || is_stop(folded)) {
            continue;
        }
        rc = push_term(&out, &n, &cap, folded);
        if (rc != SKWOOP_MEM_OK) {
            free(tmp);
            free_terms(out, n);
            return rc;
        }
    }
    free(tmp);
    *terms = out;
    *n_out = n;
    return SKWOOP_MEM_OK;
}

static int term_index(char **vocab, int nv, const char *term) {
    int i;
    for (i = 0; i < nv; i++) {
        if (strcmp(vocab[i], term) == 0) {
            return i;
        }
    }
    return -1;
}

typedef struct {
    double score;
    char date[SKWOOP_MEM_DATE_LEN];
    int index;
} rank_row;

static int rank_cmp(const void *a, const void *b) {
    const rank_row *ra = (const rank_row *)a;
    const rank_row *rb = (const rank_row *)b;
    int d;
    if (ra->score < rb->score) {
        return 1;
    }
    if (ra->score > rb->score) {
        return -1;
    }
    d = strcmp(rb->date, ra->date);
    if (d != 0) {
        return d;
    }
    return ra->index - rb->index;
}

static int dedup_query(char **terms, int n, char ***out, int *n_out) {
    char **u = NULL;
    int un = 0;
    int uc = 0;
    int i;
    int rc;
    for (i = 0; i < n; i++) {
        if (term_index(u, un, terms[i]) >= 0) {
            continue;
        }
        rc = push_term(&u, &un, &uc, terms[i]);
        if (rc != SKWOOP_MEM_OK) {
            free_terms(u, un);
            return rc;
        }
    }
    *out = u;
    *n_out = un;
    return SKWOOP_MEM_OK;
}

int skwoop_memory_search(skwoop_memory *mem, const char *query, int k,
                         skwoop_fact *hits, int hits_cap, int *n_out) {
    char **qterms = NULL;
    int nq = 0;
    char **quniq = NULL;
    int nuniq = 0;
    char ***docs = NULL;
    int *docn = NULL;
    int *df = NULL;
    char **vocab = NULL;
    int nv = 0;
    int vc = 0;
    int *lengths = NULL;
    rank_row *ranked = NULL;
    int nranked = 0;
    int i;
    int t;
    int rc;
    double avgdl = 0.0;
    int total;
    if (n_out != NULL) {
        *n_out = 0;
    }
    if (mem == NULL || n_out == NULL) {
        return SKWOOP_MEM_ERR_ARG;
    }
    if (k <= 0 || mem->nfact == 0 || query == NULL) {
        return SKWOOP_MEM_OK;
    }
    rc = tokenize(query, &qterms, &nq);
    if (rc != SKWOOP_MEM_OK) {
        return rc;
    }
    rc = dedup_query(qterms, nq, &quniq, &nuniq);
    free_terms(qterms, nq);
    if (rc != SKWOOP_MEM_OK) {
        return rc;
    }
    if (nuniq == 0) {
        free_terms(quniq, nuniq);
        return SKWOOP_MEM_OK;
    }
    total = mem->nfact;
    docs = (char ***)calloc((size_t)total, sizeof(char **));
    docn = (int *)calloc((size_t)total, sizeof(int));
    lengths = (int *)calloc((size_t)total, sizeof(int));
    if (docs == NULL || docn == NULL || lengths == NULL) {
        free(docs);
        free(docn);
        free(lengths);
        free_terms(quniq, nuniq);
        return SKWOOP_MEM_ERR_NOMEM;
    }
    for (i = 0; i < total; i++) {
        char *joined;
        size_t lt = strlen(mem->facts[i].text);
        size_t lg = strlen(mem->facts[i].tags);
        joined = (char *)malloc(lt + 1 + lg + 1);
        if (joined == NULL) {
            rc = SKWOOP_MEM_ERR_NOMEM;
            goto done;
        }
        memcpy(joined, mem->facts[i].text, lt);
        joined[lt] = ' ';
        memcpy(joined + lt + 1, mem->facts[i].tags, lg + 1);
        rc = tokenize(joined, &docs[i], &docn[i]);
        free(joined);
        if (rc != SKWOOP_MEM_OK) {
            goto done;
        }
        lengths[i] = docn[i];
        avgdl += (double)docn[i];
        for (t = 0; t < docn[i]; t++) {
            int seen = 0;
            int u;
            for (u = 0; u < t; u++) {
                if (strcmp(docs[i][u], docs[i][t]) == 0) {
                    seen = 1;
                    break;
                }
            }
            if (seen) {
                continue;
            }
            if (term_index(vocab, nv, docs[i][t]) >= 0) {
                df[term_index(vocab, nv, docs[i][t])]++;
                continue;
            }
            rc = push_term(&vocab, &nv, &vc, docs[i][t]);
            if (rc != SKWOOP_MEM_OK) {
                goto done;
            }
            {
                int *grown = (int *)realloc(df, (size_t)nv * sizeof(int));
                if (grown == NULL) {
                    rc = SKWOOP_MEM_ERR_NOMEM;
                    goto done;
                }
                df = grown;
                df[nv - 1] = 1;
            }
        }
    }
    avgdl = total > 0 ? avgdl / (double)total : 0.0;
    if (avgdl <= 0.0) {
        rc = SKWOOP_MEM_OK;
        goto done;
    }
    ranked = (rank_row *)calloc((size_t)total, sizeof(rank_row));
    if (ranked == NULL) {
        rc = SKWOOP_MEM_ERR_NOMEM;
        goto done;
    }
    for (i = 0; i < total; i++) {
        double score = 0.0;
        int qi;
        for (qi = 0; qi < nuniq; qi++) {
            int freq = 0;
            int vi;
            int seen;
            double idf;
            double denom;
            for (t = 0; t < docn[i]; t++) {
                if (strcmp(docs[i][t], quniq[qi]) == 0) {
                    freq++;
                }
            }
            if (freq == 0) {
                continue;
            }
            vi = term_index(vocab, nv, quniq[qi]);
            seen = vi >= 0 ? df[vi] : 0;
            idf = log(((double)total - (double)seen + 0.5) / ((double)seen + 0.5) + 1.0);
            denom = (double)freq + K1 * (1.0 - B + B * ((double)lengths[i] / avgdl));
            score += idf * ((double)freq * (K1 + 1.0)) / denom;
        }
        if (score > 0.0) {
            ranked[nranked].score = score;
            snprintf(ranked[nranked].date, sizeof ranked[nranked].date, "%s",
                     mem->facts[i].date);
            ranked[nranked].index = i;
            nranked++;
        }
    }
    qsort(ranked, (size_t)nranked, sizeof(rank_row), rank_cmp);
    if (k > nranked) {
        k = nranked;
    }
    if (hits != NULL && hits_cap >= 0 && k > hits_cap) {
        k = hits_cap;
    }
    if (hits == NULL) {
        k = 0;
    }
    for (i = 0; i < k; i++) {
        fill_fact(&mem->facts[ranked[i].index], &hits[i]);
    }
    *n_out = k;
    rc = SKWOOP_MEM_OK;
done:
    if (docs != NULL) {
        for (i = 0; i < total; i++) {
            free_terms(docs[i], docn != NULL ? docn[i] : 0);
        }
    }
    free(docs);
    free(docn);
    free(lengths);
    free(df);
    free_terms(vocab, nv);
    free_terms(quniq, nuniq);
    free(ranked);
    return rc;
}

int skwoop_memory_core_card(const skwoop_memory *mem, int limit_chars,
                            skwoop_fact *out, int out_cap, int *n_out) {
    int used = 0;
    int n = 0;
    int i;
    if (n_out != NULL) {
        *n_out = 0;
    }
    if (mem == NULL || n_out == NULL) {
        return SKWOOP_MEM_ERR_ARG;
    }
    if (limit_chars < 0) {
        limit_chars = 4000;
    }
    for (i = 0; i < mem->ncore; i++) {
        const fact_rec *fact = find_fact(mem, mem->core[i]);
        int len;
        if (fact == NULL) {
            continue;
        }
        len = (int)strlen(fact->text);
        if (used + len > limit_chars) {
            break;
        }
        if (out != NULL && n < out_cap) {
            fill_fact(fact, &out[n]);
        }
        n++;
        used += len;
    }
    *n_out = n;
    return SKWOOP_MEM_OK;
}

static int append_journal(skwoop_memory *mem, const char *line) {
    char path[1024];
    FILE *f;
    char *dir;
    int rc = snprintf(path, sizeof path, "%s/journal.log", mem->dir);
    if (rc < 0 || (size_t)rc >= sizeof path) {
        return SKWOOP_MEM_ERR_ARG;
    }
    dir = xstrdup(mem->dir);
    if (dir == NULL) {
        return SKWOOP_MEM_ERR_NOMEM;
    }
    rc = mkdir_p(dir);
    free(dir);
    if (rc != SKWOOP_MEM_OK) {
        return rc;
    }
    f = fopen(path, "ab");
    if (f == NULL) {
        return SKWOOP_MEM_ERR_IO;
    }
    if (fputs(line, f) < 0) {
        fclose(f);
        return SKWOOP_MEM_ERR_IO;
    }
    if (fclose(f) != 0) {
        return SKWOOP_MEM_ERR_IO;
    }
    return SKWOOP_MEM_OK;
}

static void today(char *buf, size_t cap) {
    time_t now = time(NULL);
    struct tm tm;
    localtime_r(&now, &tm);
    strftime(buf, cap, "%Y-%m-%d", &tm);
}

static int new_id(skwoop_memory *mem, char *buf, size_t cap) {
    struct timeval tv;
    long long millis;
    int guard = 0;
    if (gettimeofday(&tv, NULL) != 0) {
        return SKWOOP_MEM_ERR_IO;
    }
    millis = (long long)tv.tv_sec * 1000LL + (long long)tv.tv_usec / 1000LL;
    for (;;) {
        snprintf(buf, cap, "m%lld", millis);
        if (find_fact(mem, buf) == NULL) {
            return SKWOOP_MEM_OK;
        }
        millis++;
        if (++guard > 100000) {
            return SKWOOP_MEM_ERR;
        }
    }
}

int skwoop_memory_add(skwoop_memory *mem, const char *text, const char *tags_csv,
                      skwoop_fact *out) {
    char id[SKWOOP_MEM_ID_LEN];
    char date[SKWOOP_MEM_DATE_LEN];
    char *flat = NULL;
    char *tags = NULL;
    char *line = NULL;
    size_t need;
    int rc;
    const fact_rec *found;
    if (mem == NULL || text == NULL) {
        return SKWOOP_MEM_ERR_ARG;
    }
    rc = collapse_ws(text, &flat);
    if (rc != SKWOOP_MEM_OK) {
        return rc;
    }
    if (flat[0] == '\0') {
        free(flat);
        return SKWOOP_MEM_ERR_ARG;
    }
    rc = normalize_tags(tags_csv, &tags);
    if (rc != SKWOOP_MEM_OK) {
        free(flat);
        return rc;
    }
    rc = new_id(mem, id, sizeof id);
    if (rc != SKWOOP_MEM_OK) {
        free(flat);
        free(tags);
        return rc;
    }
    today(date, sizeof date);
    need = strlen(id) + strlen(date) + strlen(tags) + strlen(flat) + 16;
    line = (char *)malloc(need);
    if (line == NULL) {
        free(flat);
        free(tags);
        return SKWOOP_MEM_ERR_NOMEM;
    }
    snprintf(line, need, "add|%s|%s|%s|%s\n", id, date, tags, flat);
    rc = append_journal(mem, line);
    free(line);
    free(flat);
    free(tags);
    if (rc != SKWOOP_MEM_OK) {
        return rc;
    }
    rc = skwoop_memory_reload(mem);
    if (rc != SKWOOP_MEM_OK) {
        return rc;
    }
    if (out != NULL) {
        found = find_fact(mem, id);
        if (found == NULL) {
            return SKWOOP_MEM_ERR;
        }
        fill_fact(found, out);
    }
    return SKWOOP_MEM_OK;
}

int skwoop_memory_delete(skwoop_memory *mem, const char *id) {
    char date[SKWOOP_MEM_DATE_LEN];
    char line[SKWOOP_MEM_ID_LEN + 32];
    int rc;
    if (mem == NULL || id == NULL || id[0] == '\0') {
        return SKWOOP_MEM_ERR_ARG;
    }
    if (find_fact(mem, id) == NULL) {
        return SKWOOP_MEM_ERR_MISSING;
    }
    today(date, sizeof date);
    snprintf(line, sizeof line, "del|%s|%s\n", id, date);
    rc = append_journal(mem, line);
    if (rc != SKWOOP_MEM_OK) {
        return rc;
    }
    return skwoop_memory_reload(mem);
}

int skwoop_memory_reload(skwoop_memory *mem) {
    int rc;
    if (mem == NULL) {
        return SKWOOP_MEM_ERR_ARG;
    }
    free_facts(mem);
    if (!is_dir(mem->dir)) {
        return SKWOOP_MEM_OK;
    }
    rc = read_lines(mem, "facts.txt", 0);
    if (rc != SKWOOP_MEM_OK) {
        return rc;
    }
    rc = read_lines(mem, "journal.log", 1);
    if (rc != SKWOOP_MEM_OK) {
        return rc;
    }
    return read_core(mem);
}

int skwoop_memory_open(skwoop_memory **out, const char *dir) {
    skwoop_memory *mem;
    const char *chosen = dir;
    if (out == NULL) {
        return SKWOOP_MEM_ERR_ARG;
    }
    *out = NULL;
    if (chosen == NULL || chosen[0] == '\0') {
        chosen = getenv("SKWOOP_MEMORY_DIR");
        if (chosen == NULL || chosen[0] == '\0') {
            chosen = "/tmp/skwoop-memory-test";
        }
    }
    mem = (skwoop_memory *)calloc(1, sizeof(*mem));
    if (mem == NULL) {
        return SKWOOP_MEM_ERR_NOMEM;
    }
    mem->dir = xstrdup(chosen);
    if (mem->dir == NULL) {
        free(mem);
        return SKWOOP_MEM_ERR_NOMEM;
    }
    if (skwoop_memory_reload(mem) != SKWOOP_MEM_OK) {
        skwoop_memory_close(mem);
        return SKWOOP_MEM_ERR;
    }
    *out = mem;
    return SKWOOP_MEM_OK;
}

void skwoop_memory_close(skwoop_memory *mem) {
    if (mem == NULL) {
        return;
    }
    free_facts(mem);
    free(mem->dir);
    free(mem);
}

const char *skwoop_memory_dir(const skwoop_memory *mem) {
    return mem != NULL ? mem->dir : NULL;
}

int skwoop_memory_fact_count(const skwoop_memory *mem) {
    return mem != NULL ? mem->nfact : 0;
}
