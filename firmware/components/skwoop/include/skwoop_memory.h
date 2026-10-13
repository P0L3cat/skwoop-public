#ifndef SKWOOP_MEMORY_H
#define SKWOOP_MEMORY_H

/*
 * On-pod fact store.
 *
 * Directory: argument, else $SKWOOP_MEMORY_DIR, else /tmp/skwoop-memory-test.
 *   facts.txt   — id|date|tags|text   (comma-separated tags)
 *   journal.log — add|id|date|tags|text  or  del|id|date
 *   core.txt    — one pinned fact id per line
 * A missing directory or file is an empty store. Search is brute-force BM25
 * (k1=1.2, b=0.75) with plural folding and stopwords. No third-party deps.
 *
 * Returned fact text pointers are owned by the store and stay valid until the
 * next add, delete, reload, or close.
 */

#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

#define SKWOOP_MEM_K1 1.2
#define SKWOOP_MEM_B  0.75

#define SKWOOP_MEM_OK           0
#define SKWOOP_MEM_ERR         -1
#define SKWOOP_MEM_ERR_ARG     -2
#define SKWOOP_MEM_ERR_IO      -3
#define SKWOOP_MEM_ERR_NOMEM   -4
#define SKWOOP_MEM_ERR_MISSING -5

#define SKWOOP_MEM_ID_LEN   64
#define SKWOOP_MEM_DATE_LEN 16
#define SKWOOP_MEM_TAGS_LEN 256

typedef struct skwoop_memory skwoop_memory;

typedef struct skwoop_fact {
    char id[SKWOOP_MEM_ID_LEN];
    char date[SKWOOP_MEM_DATE_LEN];
    char tags[SKWOOP_MEM_TAGS_LEN]; /* comma-joined, no spaces */
    const char *text;
} skwoop_fact;

/* dir NULL selects $SKWOOP_MEMORY_DIR or /tmp/skwoop-memory-test. */
int skwoop_memory_open(skwoop_memory **out, const char *dir);
void skwoop_memory_close(skwoop_memory *mem);
const char *skwoop_memory_dir(const skwoop_memory *mem);

/* Re-read facts.txt, journal.log, and core.txt. */
int skwoop_memory_reload(skwoop_memory *mem);

int skwoop_memory_fact_count(const skwoop_memory *mem);

/*
 * Journal an add line. tags_csv is comma-separated (NULL is none).
 * Whitespace in text is collapsed. Id is "m" plus unix milliseconds.
 * out may be NULL. Returns SKWOOP_MEM_OK or a negative code.
 */
int skwoop_memory_add(skwoop_memory *mem, const char *text, const char *tags_csv,
                      skwoop_fact *out);

/* Journal a del line. SKWOOP_MEM_ERR_MISSING when the id is not loaded. */
int skwoop_memory_delete(skwoop_memory *mem, const char *id);

/*
 * BM25 over text and tags. k <= 0 yields no hits. Equal scores keep the
 * newer date, then earlier file order. Returns SKWOOP_MEM_OK.
 */
int skwoop_memory_search(skwoop_memory *mem, const char *query, int k,
                         skwoop_fact *hits, int hits_cap, int *n_out);

/*
 * Pinned facts in core.txt order. Text length is the character budget.
 * A fact that would exceed limit_chars stops the card (later facts are not
 * pulled forward). limit_chars < 0 uses 4000. Returns SKWOOP_MEM_OK.
 */
int skwoop_memory_core_card(const skwoop_memory *mem, int limit_chars,
                            skwoop_fact *out, int out_cap, int *n_out);

#ifdef __cplusplus
}
#endif

#endif /* SKWOOP_MEMORY_H */
