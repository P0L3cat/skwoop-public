#pragma once

/* Device side of timers.h: wall clock (SNTP when online, last known time
 * from NVS after a reboot; the 1.75C has no battery RTC), the persisted
 * table, a 1 s tick that fires due items, and the face countdown text. */

#include <stdint.h>

#include "esp_err.h"
#include "timers.h"

typedef struct {
    /* Tick task. Keep short; ringing is the caller's job. */
    void (*on_fire)(const timer_item_t *item);
    /* Tick task, once a second: "m:ss" of the soonest item within a day,
     * or NULL to hide the countdown. */
    void (*on_countdown)(const char *text);
} alarm_clock_callbacks_t;

esp_err_t alarm_clock_start(const alarm_clock_callbacks_t *callbacks);

/* Start SNTP. Call once Wi-Fi has an IP. */
void alarm_clock_sync_time(void);

/* Due in seconds from now. Returns the id (>0) or a negative errno. */
int alarm_clock_add_in(timer_kind_t kind, uint32_t seconds, const char *label);

/* Next local hour:minute. -EAGAIN until the clock has been set once. */
int alarm_clock_add_at(timer_kind_t kind, int hour, int minute, const char *label);

int alarm_clock_cancel_kind(timer_kind_t kind);
