#pragma once

/* Offline voice commands: the MultiNet phrase table and the dispatch from a
 * recognized command id to local handlers. Works with no internet. Pure C. */

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

typedef enum {
    CMD_TIMER_SET,
    CMD_TIMER_CANCEL,
    CMD_VOLUME_UP,
    CMD_VOLUME_DOWN,
    CMD_MUTE,
    CMD_UNMUTE,
    CMD_LIGHTS_ON,
    CMD_LIGHTS_OFF,
    CMD_STOP,
} cmd_action_t;

typedef struct {
    int id;
    const char *phrase;
    cmd_action_t action;
    uint32_t seconds; /* CMD_TIMER_SET only */
} cmd_entry_t;

/* The table MultiNet is loaded with; ids are the MultiNet command ids. */
const cmd_entry_t *cmds_table(size_t *count);

/* Local handlers; main wires these to the board, timers, and smart home. */
typedef struct {
    void (*timer_set)(uint32_t seconds);
    void (*timer_cancel)(void);
    void (*volume_step)(int delta);
    void (*mute)(bool on);
    void (*lights)(bool on);
    void (*stop)(void);
} cmd_handlers_t;

#define CMDS_VOLUME_STEP 10

/* Run the handler for a recognized id. Returns false for an unknown id. */
bool cmds_dispatch(int id, const cmd_handlers_t *handlers);
