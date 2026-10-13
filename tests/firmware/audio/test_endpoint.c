/* Host check for utterance end-pointing. No ESP-IDF. */

#include "endpoint.h"

#include <assert.h>

static const endpoint_limits_t LIMITS = { .start_timeout_ms = 3000, .trailing_silence_ms = 600, .max_ms = 6000 };

static endpoint_state_t run(endpoint_t *ep, bool speech, int frames)
{
    endpoint_state_t s = ep->state;

    for (int i = 0; i < frames; i++) {
        s = endpoint_step(ep, speech, 60);
    }

    return s;
}

int main(void)
{
    endpoint_t ep;

    endpoint_init(&ep, &LIMITS);
    assert(run(&ep, false, 49) == ENDPOINT_WAITING);
    assert(run(&ep, false, 1) == ENDPOINT_NO_SPEECH);
    assert(run(&ep, true, 5) == ENDPOINT_NO_SPEECH);

    endpoint_init(&ep, &LIMITS);
    assert(run(&ep, false, 10) == ENDPOINT_WAITING);
    assert(run(&ep, true, 20) == ENDPOINT_SPEAKING);
    assert(run(&ep, false, 9) == ENDPOINT_SPEAKING);
    assert(run(&ep, true, 1) == ENDPOINT_SPEAKING);
    assert(run(&ep, false, 9) == ENDPOINT_SPEAKING);
    assert(run(&ep, false, 1) == ENDPOINT_DONE);

    endpoint_init(&ep, &LIMITS);
    assert(run(&ep, true, 99) == ENDPOINT_SPEAKING);
    assert(run(&ep, true, 1) == ENDPOINT_DONE);

    return 0;
}
