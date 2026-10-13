#include "mood_builtin.h"

#include <stddef.h>

#include "esp_log.h"

#if defined(MOOD_WEIGHTS_EMBEDDED)

/* EMBED_FILES mood_v11.bin from tools/export_mood.py. */
extern const uint8_t mood_v11_bin_start[] asm("_binary_mood_v11_bin_start");
extern const uint8_t mood_v11_bin_end[] asm("_binary_mood_v11_bin_end");

#endif

int mood_builtin(mood_model_t *m)
{
#if defined(MOOD_WEIGHTS_EMBEDDED)
    /* SAFETY: the embed places _binary_mood_v11_bin_end after _start, so the
     * difference is non-negative and the span fits in size_t. */
    return mood_load(m, mood_v11_bin_start, (size_t)(mood_v11_bin_end - mood_v11_bin_start));
#else
    static int logged;

    (void)m;

    if (!logged) {
        ESP_LOGE("mood", "mood weights missing; mood disabled. Generate components/mood/mood_v11.bin with tools/export_mood.py");
        logged = 1;
    }

    return -1;
#endif
}
