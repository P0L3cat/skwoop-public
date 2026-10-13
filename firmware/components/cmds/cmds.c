#include "cmds.h"

/* MultiNet7 English takes plain lower-case words. Several phrasings share an
 * action; ids stay unique because MultiNet reports the id. */
static const cmd_entry_t CMDS[] = {
    { 1, "set a timer for one minute", CMD_TIMER_SET, 60 },
    { 2, "set a timer for two minutes", CMD_TIMER_SET, 120 },
    { 3, "set a timer for five minutes", CMD_TIMER_SET, 300 },
    { 4, "set a timer for ten minutes", CMD_TIMER_SET, 600 },
    { 5, "set a timer for fifteen minutes", CMD_TIMER_SET, 900 },
    { 6, "set a timer for twenty minutes", CMD_TIMER_SET, 1200 },
    { 7, "set a timer for thirty minutes", CMD_TIMER_SET, 1800 },
    { 8, "set a timer for one hour", CMD_TIMER_SET, 3600 },
    { 9, "cancel the timer", CMD_TIMER_CANCEL, 0 },
    { 10, "stop the timer", CMD_TIMER_CANCEL, 0 },
    { 11, "volume up", CMD_VOLUME_UP, 0 },
    { 12, "turn it up", CMD_VOLUME_UP, 0 },
    { 13, "volume down", CMD_VOLUME_DOWN, 0 },
    { 14, "turn it down", CMD_VOLUME_DOWN, 0 },
    { 15, "mute", CMD_MUTE, 0 },
    { 16, "unmute", CMD_UNMUTE, 0 },
    { 17, "turn on the lights", CMD_LIGHTS_ON, 0 },
    { 18, "lights on", CMD_LIGHTS_ON, 0 },
    { 19, "turn off the lights", CMD_LIGHTS_OFF, 0 },
    { 20, "lights off", CMD_LIGHTS_OFF, 0 },
    { 21, "stop", CMD_STOP, 0 },
    { 22, "never mind", CMD_STOP, 0 },
};

const cmd_entry_t *cmds_table(size_t *count)
{
    *count = sizeof CMDS / sizeof CMDS[0];

    return CMDS;
}

static const cmd_entry_t *find(int id)
{
    for (size_t i = 0; i < sizeof CMDS / sizeof CMDS[0]; i++) {
        if (CMDS[i].id == id) {
            return &CMDS[i];
        }
    }

    return NULL;
}

bool cmds_dispatch(int id, const cmd_handlers_t *h)
{
    const cmd_entry_t *e = find(id);

    if (e == NULL || h == NULL) {
        return false;
    }

    switch (e->action) {
    case CMD_TIMER_SET:
        h->timer_set(e->seconds);
        break;
    case CMD_TIMER_CANCEL:
        h->timer_cancel();
        break;
    case CMD_VOLUME_UP:
        h->volume_step(CMDS_VOLUME_STEP);
        break;
    case CMD_VOLUME_DOWN:
        h->volume_step(-CMDS_VOLUME_STEP);
        break;
    case CMD_MUTE:
        h->mute(true);
        break;
    case CMD_UNMUTE:
        h->mute(false);
        break;
    case CMD_LIGHTS_ON:
        h->lights(true);
        break;
    case CMD_LIGHTS_OFF:
        h->lights(false);
        break;
    case CMD_STOP:
        h->stop();
        break;
    }

    return true;
}
