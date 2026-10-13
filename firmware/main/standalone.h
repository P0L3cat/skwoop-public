#pragma once

/* Standalone pod path (CONFIG_SKWOOP_STANDALONE): the whole voice loop on the
 * device. Wake or button -> record (VAD end-pointed) -> cloud STT -> harness
 * route + BM25 recall -> streaming cloud LLM -> per-sentence cloud TTS -> play.
 * Keys come from encrypted NVS; the bridge host is not needed. */

#include <stdbool.h>

#include "face.h"
#include "mood.h"

typedef void (*standalone_mood_fn)(face_mood_t mood);

/* Speaker task, once per reply sentence: its on-device mood. */
typedef void (*standalone_reply_mood_fn)(mood_name_t mood);

/* false when memory or the cloud keys are missing; the caller then keeps the
 * bridge path. */
bool standalone_start(standalone_mood_fn on_mood, standalone_reply_mood_fn on_reply_mood);

/* Begin one turn (wake word or button). Ignored while a turn is running. */
void standalone_listen(void);

/* Text turn from any source (bridge STT, tests on device). */
void standalone_on_text(const char *utterance);

/* Drop the running turn: recording stops and queued speech is skipped (an
 * offline command took over). No-op when idle. */
void standalone_cancel(void);
