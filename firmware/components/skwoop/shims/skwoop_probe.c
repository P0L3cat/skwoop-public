/* Link anchor so the linker keeps every Skwoop entry point in size builds. */
#include "skwoop_config.h"
#include "skwoop_net.h"
#include "skwoop_harness.h"
#include "skwoop_clips.h"
#include <stdio.h>
void skwoop_probe(void) {
    static char body[4096]; size_t n; char base[256];
    skwoop_config_load();
    if (skwoop_net_provider_base(base, sizeof base) == 0) skwoop_http_get(base, body, sizeof body, &n);
    skwoop_wss_connect("wss://example.invalid/ws");
    skwoop_clips_fetch("hello", "/s/clip.bin");
    skwoop_harness_t *h = skwoop_harness_create("/s/facts");
    skwoop_harness_remember(h, "probe fact");
    static skwoop_sentence_list_t out; skwoop_harness_turn(h, "what is my wife's name", NULL, NULL, &out);
    char pre[512]; skwoop_harness_recall_prefill(h, "wife", pre, sizeof pre); skwoop_harness_forget_last(h);
    printf("%d %u %d %s\n", (int)skwoop_harness_route("x"), skwoop_harness_toolset("x"), skwoop_config_get_standalone(), skwoop_harness_strerror(0));
    skwoop_clip_index *ci = NULL; if (skwoop_clips_load_file("/s/clips.idx", &ci) == 0) skwoop_clips_free(ci);
    skwoop_harness_destroy(h);
}
