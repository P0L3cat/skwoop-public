#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "esp_err.h"

/* Load the model partition, start WakeNet (CONFIG_SKWOOP_WAKE_KEYWORD, else
 * hiesp) with AEC and VAD, and spawn the fetch task. on_wake runs on that
 * task with the wake word. When MultiNet is packed, the few seconds after a
 * wake are also matched against the offline command table (cmds) and
 * on_command gets the command id. Both callbacks must return quickly. */
esp_err_t wake_start(void (*on_wake)(const char *word), void (*on_command)(int id));

/* Interleaved mic then reference, 16 kHz. frames is the per-channel count.
 * The caller is the feed side: samples are chunked into the AFE. */
void wake_feed(const int16_t *interleaved, size_t frames);

bool wake_vad_speech(void);

/* Copy up to n mono AEC samples. Returns how many were copied. Non-blocking. */
size_t wake_fetch_clean(int16_t *out, size_t n);
