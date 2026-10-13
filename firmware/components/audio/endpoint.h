#pragma once

/* Utterance end-pointing from per-frame VAD decisions: wait for speech, then
 * end after a run of trailing silence or at the length cap. Pure C. */

#include <stdbool.h>
#include <stdint.h>

typedef enum {
    ENDPOINT_WAITING,
    ENDPOINT_SPEAKING,
    ENDPOINT_DONE,
    ENDPOINT_NO_SPEECH,
} endpoint_state_t;

typedef struct {
    uint32_t start_timeout_ms;
    uint32_t trailing_silence_ms;
    uint32_t max_ms;
} endpoint_limits_t;

typedef struct {
    endpoint_limits_t limits;
    endpoint_state_t state;
    uint32_t elapsed_ms;
    uint32_t silence_ms;
} endpoint_t;

void endpoint_init(endpoint_t *ep, const endpoint_limits_t *limits);

/* Advance by one frame. DONE and NO_SPEECH are final until endpoint_init. */
endpoint_state_t endpoint_step(endpoint_t *ep, bool speech, uint32_t frame_ms);
