#include "endpoint.h"

void endpoint_init(endpoint_t *ep, const endpoint_limits_t *limits)
{
    ep->limits = *limits;
    ep->state = ENDPOINT_WAITING;
    ep->elapsed_ms = 0;
    ep->silence_ms = 0;
}

endpoint_state_t endpoint_step(endpoint_t *ep, bool speech, uint32_t frame_ms)
{
    if (ep->state == ENDPOINT_DONE || ep->state == ENDPOINT_NO_SPEECH) {
        return ep->state;
    }

    ep->elapsed_ms += frame_ms;

    if (ep->state == ENDPOINT_WAITING) {
        if (speech) {
            ep->state = ENDPOINT_SPEAKING;
        } else if (ep->elapsed_ms >= ep->limits.start_timeout_ms) {
            ep->state = ENDPOINT_NO_SPEECH;
        }

        return ep->state;
    }

    ep->silence_ms = speech ? 0 : ep->silence_ms + frame_ms;

    if (ep->silence_ms >= ep->limits.trailing_silence_ms || ep->elapsed_ms >= ep->limits.max_ms) {
        ep->state = ENDPOINT_DONE;
    }

    return ep->state;
}
