#ifndef SKWOOP_HARNESS_H
#define SKWOOP_HARNESS_H

/*
 * Lean on-pod harness loop, host-simulated.
 * route (instant/fast/deep subset) -> toolset pick -> optional recall
 * prefill -> model callback -> sentence list.
 * Music, home control, and Matter are out of this step.
 *
 * standalone=1 (skwoop_config): skwoop_net + local memory + clip_base_url.
 * No bridge host URL is required.
 * standalone=0: bridge websocket mode. This host build does not link the
 * bridge client. skwoop_harness_turn returns SKWOOP_HARNESS_BRIDGE_UNLINKED.
 */

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define SKWOOP_HARNESS_UTTER_MAX 512
#define SKWOOP_HARNESS_SENT_MAX 8
#define SKWOOP_HARNESS_SENT_LEN 256
#define SKWOOP_HARNESS_PREFILL_MAX 1024
#define SKWOOP_HARNESS_TOOL_NAME_MAX 32

/* standalone=0 on this host build. Not a bridge-host call. */
#define SKWOOP_HARNESS_BRIDGE_UNLINKED (-2)
#define SKWOOP_HARNESS_BRIDGE_UNLINKED_MSG "bridge mode not linked in host build"

typedef enum skwoop_route {
    SKWOOP_ROUTE_INSTANT = 0,
    SKWOOP_ROUTE_FAST = 1,
    SKWOOP_ROUTE_DEEP = 2
} skwoop_route_t;

/* Bits the router may attach to a route. Host tests read these. */
enum {
    SKWOOP_TOOL_RECALL = 1u << 0,
    SKWOOP_TOOL_REMEMBER = 1u << 1,
    SKWOOP_TOOL_FORGET_LAST = 1u << 2,
    SKWOOP_TOOL_TIMER = 1u << 3
};

typedef struct skwoop_sentence_list {
    char text[SKWOOP_HARNESS_SENT_MAX][SKWOOP_HARNESS_SENT_LEN];
    int count;
} skwoop_sentence_list_t;

/*
 * Model stand-in. prefill is the recall block (may be empty).
 * Write one or more sentences into out. Return 0 on success.
 * A clarifying ask is a single sentence in out.
 */
typedef int (*skwoop_model_fn)(const char *utterance,
                               skwoop_route_t route,
                               const char *prefill,
                               skwoop_sentence_list_t *out,
                               void *userdata);

typedef struct skwoop_harness skwoop_harness_t;

/* Owns an s3 fact store. facts_path is the memory directory (NULL → s3 default). */
skwoop_harness_t *skwoop_harness_create(const char *facts_path);
void skwoop_harness_destroy(skwoop_harness_t *h);

/* Keyword route. Unknown utterances land on FAST with recall. */
skwoop_route_t skwoop_harness_route(const char *utterance);

/* Tool bits selected for this utterance (recall/remember/forget/timer). */
unsigned skwoop_harness_toolset(const char *utterance);

/*
 * If the toolset includes recall, fill prefill with matching facts.
 * prefill_cap includes the trailing NUL. Returns bytes written (no NUL),
 * or -1 on bad args.
 */
int skwoop_harness_recall_prefill(skwoop_harness_t *h,
                                  const char *utterance,
                                  char *prefill,
                                  size_t prefill_cap);

/*
 * Full turn. Uses model when non-NULL. When model is NULL, a tiny host
 * fake answers from the prefill (wife facts) or emits one clarifying
 * ask for a timer with no time.
 * Returns 0 on success, -1 on bad args.
 * Returns SKWOOP_HARNESS_BRIDGE_UNLINKED when standalone is off. The host
 * build does not open a bridge websocket and does not call the bridge host.
 * skwoop_harness_strerror of that code is SKWOOP_HARNESS_BRIDGE_UNLINKED_MSG.
 */
int skwoop_harness_turn(skwoop_harness_t *h,
                        const char *utterance,
                        skwoop_model_fn model,
                        void *userdata,
                        skwoop_sentence_list_t *out);

/* Stable text for a turn status. Empty string when rc is unknown. */
const char *skwoop_harness_strerror(int rc);

/* Timer tool: called with the parsed seconds before the harness confirms
 * "Timer set". Return 0 when the timer exists; nonzero makes the harness say
 * it could not set it. Without a callback the reply is spoken only. */
typedef int (*skwoop_timer_fn)(int seconds, void *ctx);

void skwoop_harness_on_timer(skwoop_harness_t *h, skwoop_timer_fn fn, void *ctx);

/* Seed / mutate facts through the s3 store the harness owns. */
int skwoop_harness_remember(skwoop_harness_t *h, const char *fact);
int skwoop_harness_forget_last(skwoop_harness_t *h);

#ifdef __cplusplus
}
#endif

#endif /* SKWOOP_HARNESS_H */
