#include "skwoop_harness.h"

#include "skwoop_config.h"
#include "skwoop_memory.h"
#include "skwoop_net.h"

#include <ctype.h>
#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#define NORM_MAX SKWOOP_HARNESS_UTTER_MAX

struct skwoop_harness {
    skwoop_memory *mem;
    char last_id[SKWOOP_MEM_ID_LEN];
    int has_last;
    skwoop_timer_fn on_timer;
    void *on_timer_ctx;
};

static const char *const k_people[] = {
    "wife",     "husband", "partner", "spouse", "mom",    "mother",
    "dad",      "father",  "son",     "daughter", "kid",  "kids",
    "child",    "children", "brother", "sister", "dog",   "cat",
    "pet",      "pets",
};

static const char *const k_num_word[] = {
    "one", "two", "three", "four", "five", "six", "seven", "eight", "nine",
    "ten", "fifteen", "twenty", "thirty",
};
static const int k_num_val[] = {
    1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 15, 20, 30,
};

static int word_at(const char *s, const char *w) {
    size_t n = strlen(w);
    const char *p = s;
    while ((p = strstr(p, w)) != NULL) {
        int before_ok = (p == s) || !isalnum((unsigned char)p[-1]);
        int after_ok = !isalnum((unsigned char)p[n]);
        if (before_ok && after_ok) {
            return 1;
        }
        p += n;
    }
    return 0;
}

static void normalize(char *dst, size_t cap, const char *src) {
    size_t i = 0;
    int sp = 1;
    if (cap == 0) {
        return;
    }
    if (src == NULL) {
        dst[0] = '\0';
        return;
    }
    for (; *src && i + 1 < cap; src++) {
        unsigned char c = (unsigned char)*src;
        if (c == 0xE2 && (unsigned char)src[1] == 0x80 &&
            ((unsigned char)src[2] == 0x99 || (unsigned char)src[2] == 0x98)) {
            c = '\'';
            src += 2;
        }
        if (c == '\'' ) {
            continue;
        }
        if (c == '.' || c == '!' || c == '?' || c == ',') {
            c = ' ';
        }
        if (isspace(c)) {
            if (sp) {
                continue;
            }
            dst[i++] = ' ';
            sp = 1;
            continue;
        }
        dst[i++] = (char)tolower(c);
        sp = 0;
    }
    if (i > 0 && dst[i - 1] == ' ') {
        i--;
    }
    dst[i] = '\0';
}

static int is_memory_fact(const char *t) {
    size_t i;
    int person = 0;
    if (!word_at(t, "who") && !word_at(t, "what")) {
        return 0;
    }
    if (!word_at(t, "my") && !word_at(t, "our")) {
        return 0;
    }
    for (i = 0; i < sizeof k_people / sizeof k_people[0]; i++) {
        if (word_at(t, k_people[i])) {
            person = 1;
            break;
        }
    }
    return person;
}

static int is_remember_write(const char *t) {
    return strstr(t, "remember that") != NULL || strstr(t, "remember this") != NULL;
}

static int is_forget(const char *t) {
    return strstr(t, "forget that") != NULL || strstr(t, "forget about") != NULL ||
           strstr(t, "forget the last") != NULL || strstr(t, "forget last") != NULL;
}

static int is_deep_other(const char *t) {
    return strstr(t, "notes") != NULL || strstr(t, "documents") != NULL ||
           strstr(t, "document") != NULL || word_at(t, "docs") ||
           strstr(t, "research") != NULL || strstr(t, "look into") != NULL ||
           strstr(t, "summarize") != NULL || strstr(t, "summarise") != NULL ||
           strstr(t, "deep dive") != NULL;
}

static int is_timer_ask(const char *t) {
    if (word_at(t, "timer") || word_at(t, "reminder")) {
        return 1;
    }
    return strncmp(t, "remind me", 9) == 0;
}

static int is_clock_ask(const char *t) {
    return strstr(t, "what time") != NULL || strstr(t, "the time") != NULL ||
           strstr(t, "what day") != NULL || strstr(t, "the date") != NULL ||
           strstr(t, "what date") != NULL || strstr(t, "todays date") != NULL;
}

static int parse_number_token(const char *tok, int *out) {
    size_t i;
    char *end = NULL;
    long v;
    if (tok[0] >= '0' && tok[0] <= '9') {
        v = strtol(tok, &end, 10);
        if (end == tok || v <= 0 || v > 86400) {
            return 0;
        }
        *out = (int)v;
        return 1;
    }
    if (strcmp(tok, "a") == 0 || strcmp(tok, "an") == 0) {
        *out = 1;
        return 1;
    }
    for (i = 0; i < sizeof k_num_word / sizeof k_num_word[0]; i++) {
        if (strcmp(tok, k_num_word[i]) == 0) {
            *out = k_num_val[i];
            return 1;
        }
    }
    return 0;
}

/* Seconds, or 0 when the utterance names a timer but no duration. */
static int timer_seconds(const char *t) {
    char buf[NORM_MAX];
    char *words[48];
    int n = 0;
    int i;
    char *save = NULL;
    char *tok;
    snprintf(buf, sizeof buf, "%s", t);
    for (tok = strtok_r(buf, " ", &save); tok != NULL && n < 48;
         tok = strtok_r(NULL, " ", &save)) {
        words[n++] = tok;
    }
    for (i = 0; i + 1 < n; i++) {
        int num = 0;
        int mul = 0;
        if (!parse_number_token(words[i], &num)) {
            continue;
        }
        if (strncmp(words[i + 1], "hour", 4) == 0) {
            mul = 3600;
        } else if (strncmp(words[i + 1], "minute", 6) == 0 ||
                   strncmp(words[i + 1], "min", 3) == 0) {
            mul = 60;
        } else if (strncmp(words[i + 1], "second", 6) == 0 ||
                   strncmp(words[i + 1], "sec", 3) == 0) {
            mul = 1;
        }
        if (mul != 0) {
            return num * mul;
        }
    }
    return 0;
}

skwoop_route_t skwoop_harness_route(const char *utterance) {
    char t[NORM_MAX];
    normalize(t, sizeof t, utterance);
    if (t[0] == '\0') {
        return SKWOOP_ROUTE_FAST;
    }
    if (is_remember_write(t) || is_forget(t) || is_deep_other(t)) {
        return SKWOOP_ROUTE_DEEP;
    }
    if (is_memory_fact(t)) {
        return SKWOOP_ROUTE_FAST;
    }
    if (is_timer_ask(t) || is_clock_ask(t)) {
        return SKWOOP_ROUTE_INSTANT;
    }
    return SKWOOP_ROUTE_FAST;
}

unsigned skwoop_harness_toolset(const char *utterance) {
    char t[NORM_MAX];
    normalize(t, sizeof t, utterance);
    if (t[0] == '\0') {
        return 0;
    }
    if (is_remember_write(t)) {
        return SKWOOP_TOOL_REMEMBER;
    }
    if (is_forget(t)) {
        return SKWOOP_TOOL_FORGET_LAST;
    }
    if (is_deep_other(t)) {
        return 0;
    }
    if (is_memory_fact(t)) {
        return SKWOOP_TOOL_RECALL;
    }
    if (is_timer_ask(t)) {
        return SKWOOP_TOOL_TIMER;
    }
    if (is_clock_ask(t)) {
        return 0;
    }
    return SKWOOP_TOOL_RECALL;
}

static void one_sentence(skwoop_sentence_list_t *out, const char *s) {
    snprintf(out->text[0], SKWOOP_HARNESS_SENT_LEN, "%s", s);
    out->count = 1;
}

static const char *ci_find(const char *hay, const char *needle) {
    size_t n = strlen(needle);
    const char *p;
    for (p = hay; *p; p++) {
        size_t i;
        for (i = 0; i < n; i++) {
            unsigned char a = (unsigned char)p[i];
            unsigned char b = (unsigned char)needle[i];
            if (a == '\0' || tolower(a) != tolower(b)) {
                break;
            }
        }
        if (i == n) {
            return p;
        }
    }
    return NULL;
}

/* Fact text keeps the caller's casing. `norm` is only used to know the phrase. */
static const char *remember_body(const char *original) {
    const char *p = ci_find(original, "remember that");
    if (p != NULL) {
        p += strlen("remember that");
    } else {
        p = ci_find(original, "remember this");
        if (p == NULL) {
            return "";
        }
        p += strlen("remember this");
    }
    while (*p == ' ') {
        p++;
    }
    return p;
}

skwoop_harness_t *skwoop_harness_create(const char *facts_path) {
    skwoop_harness_t *h = (skwoop_harness_t *)calloc(1, sizeof(*h));
    if (h == NULL) {
        return NULL;
    }
    if (skwoop_memory_open(&h->mem, facts_path) != SKWOOP_MEM_OK) {
        free(h);
        return NULL;
    }
    return h;
}

void skwoop_harness_destroy(skwoop_harness_t *h) {
    if (h == NULL) {
        return;
    }
    skwoop_memory_close(h->mem);
    free(h);
}

int skwoop_harness_recall_prefill(skwoop_harness_t *h, const char *utterance,
                                  char *prefill, size_t prefill_cap) {
    skwoop_fact hits[4];
    int n = 0;
    int i;
    size_t used = 0;
    if (h == NULL || utterance == NULL || prefill == NULL || prefill_cap == 0) {
        return -1;
    }
    prefill[0] = '\0';
    if (skwoop_memory_search(h->mem, utterance, 4, hits, 4, &n) != SKWOOP_MEM_OK) {
        return -1;
    }
    for (i = 0; i < n; i++) {
        size_t len = strlen(hits[i].text);
        int need_nl = (i + 1 < n);
        if (used + len + (size_t)need_nl + 1 > prefill_cap) {
            break;
        }
        memcpy(prefill + used, hits[i].text, len);
        used += len;
        if (need_nl) {
            prefill[used++] = '\n';
        }
        prefill[used] = '\0';
    }
    return (int)used;
}

int skwoop_harness_remember(skwoop_harness_t *h, const char *fact) {
    skwoop_fact kept;
    if (h == NULL || fact == NULL || fact[0] == '\0') {
        return -1;
    }
    if (skwoop_memory_add(h->mem, fact, "note", &kept) != SKWOOP_MEM_OK) {
        return -1;
    }
    snprintf(h->last_id, sizeof h->last_id, "%s", kept.id);
    h->has_last = 1;
    return 0;
}

int skwoop_harness_forget_last(skwoop_harness_t *h) {
    int rc;
    if (h == NULL || !h->has_last) {
        return -1;
    }
    rc = skwoop_memory_delete(h->mem, h->last_id);
    h->has_last = 0;
    h->last_id[0] = '\0';
    return rc == SKWOOP_MEM_OK ? 0 : -1;
}

static void prefill_sentences(const char *prefill, skwoop_sentence_list_t *out) {
    const char *p = prefill;
    out->count = 0;
    if (p == NULL || p[0] == '\0') {
        one_sentence(out, "Okay.");
        return;
    }
    while (*p && out->count < SKWOOP_HARNESS_SENT_MAX) {
        const char *nl = strchr(p, '\n');
        size_t len = nl != NULL ? (size_t)(nl - p) : strlen(p);
        if (len >= SKWOOP_HARNESS_SENT_LEN) {
            len = SKWOOP_HARNESS_SENT_LEN - 1;
        }
        if (len > 0) {
            memcpy(out->text[out->count], p, len);
            out->text[out->count][len] = '\0';
            out->count++;
        }
        if (nl == NULL) {
            break;
        }
        p = nl + 1;
    }
    if (out->count == 0) {
        one_sentence(out, "Okay.");
    }
}

static void timer_sentence(skwoop_harness_t *h, const char *norm, skwoop_sentence_list_t *out) {
    int sec = timer_seconds(norm);
    if (sec <= 0) {
        one_sentence(out, "When should I remind you?");
        return;
    }
    if (h->on_timer != NULL && h->on_timer(sec, h->on_timer_ctx) != 0) {
        one_sentence(out, "I couldn't set that timer.");
        return;
    }
    if (sec % 3600 == 0) {
        snprintf(out->text[0], SKWOOP_HARNESS_SENT_LEN, "Okay. Timer set for %d hours.",
                 sec / 3600);
    } else if (sec % 60 == 0) {
        snprintf(out->text[0], SKWOOP_HARNESS_SENT_LEN, "Okay. Timer set for %d minutes.",
                 sec / 60);
    } else {
        snprintf(out->text[0], SKWOOP_HARNESS_SENT_LEN, "Okay. Timer set for %d seconds.", sec);
    }
    out->count = 1;
}

const char *skwoop_harness_strerror(int rc) {
    if (rc == SKWOOP_HARNESS_BRIDGE_UNLINKED) {
        return SKWOOP_HARNESS_BRIDGE_UNLINKED_MSG;
    }
    if (rc == -1) {
        return "bad args";
    }
    if (rc == 0) {
        return "";
    }
    return "";
}

/* Standalone path touches skwoop_net and clip_base_url. Neither has to be set.
 * A missing provider base or clip host is not a bridge URL. */
static int standalone_transport(void) {
    char url[SKWOOP_BASE_URL_MAX + 1];
    int nrc;
    if (!skwoop_config_get_standalone()) {
        /*
         * Bridge websocket mode. Documented stub: the device would open
         * the bridge host URL with esp_websocket_client. This host build
         * does not link that client and does not call it.
         */
        return SKWOOP_HARNESS_BRIDGE_UNLINKED;
    }
    nrc = skwoop_net_provider_base(url, sizeof url);
    if (nrc != 0 && nrc != -ENOENT) {
        return -1;
    }
    (void)skwoop_config_get_clip_base_url(url, sizeof url);
    return 0;
}

static void clock_sentence(const char *norm, skwoop_sentence_list_t *out) {
    time_t now = time(NULL);
    struct tm tm;
    localtime_r(&now, &tm);
    if (strstr(norm, "date") != NULL || strstr(norm, "day") != NULL) {
        strftime(out->text[0], SKWOOP_HARNESS_SENT_LEN, "Today is %A, %B %d.", &tm);
    } else {
        strftime(out->text[0], SKWOOP_HARNESS_SENT_LEN, "It is %I:%M %p.", &tm);
    }
    out->count = 1;
}

int skwoop_harness_turn(skwoop_harness_t *h, const char *utterance,
                        skwoop_model_fn model, void *userdata,
                        skwoop_sentence_list_t *out) {
    char norm[NORM_MAX];
    char prefill[SKWOOP_HARNESS_PREFILL_MAX];
    unsigned tools;
    skwoop_route_t route;
    int gate;
    if (h == NULL || utterance == NULL || out == NULL) {
        return -1;
    }
    gate = standalone_transport();
    if (gate != 0) {
        return gate;
    }
    memset(out, 0, sizeof(*out));
    normalize(norm, sizeof norm, utterance);
    route = skwoop_harness_route(utterance);
    tools = skwoop_harness_toolset(utterance);
    prefill[0] = '\0';

    if (tools & SKWOOP_TOOL_REMEMBER) {
        const char *body = remember_body(utterance);
        if (body[0] == '\0' || skwoop_harness_remember(h, body) != 0) {
            one_sentence(out, "What should I remember?");
        } else {
            one_sentence(out, "Okay. I'll remember that.");
        }
        return 0;
    }
    if (tools & SKWOOP_TOOL_FORGET_LAST) {
        if (skwoop_harness_forget_last(h) != 0) {
            one_sentence(out, "Nothing to forget.");
        } else {
            one_sentence(out, "Okay. Forgot the last thing.");
        }
        return 0;
    }
    if (tools & SKWOOP_TOOL_TIMER) {
        timer_sentence(h, norm, out);
        return 0;
    }
    if (route == SKWOOP_ROUTE_INSTANT) {
        clock_sentence(norm, out);
        return 0;
    }
    if (tools & SKWOOP_TOOL_RECALL) {
        if (skwoop_harness_recall_prefill(h, utterance, prefill, sizeof prefill) < 0) {
            return -1;
        }
    }
    if (model != NULL) {
        return model(utterance, route, prefill, out, userdata);
    }
    if (route == SKWOOP_ROUTE_DEEP && prefill[0] == '\0') {
        one_sentence(out, "I can't take that one from here.");
        return 0;
    }
    prefill_sentences(prefill, out);
    return 0;
}

void skwoop_harness_on_timer(skwoop_harness_t *h, skwoop_timer_fn fn, void *ctx) {
    if (h != NULL) {
        h->on_timer = fn;
        h->on_timer_ctx = ctx;
    }
}
