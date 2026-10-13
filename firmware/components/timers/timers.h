#pragma once

/* Local timers, alarms, and reminders: a small fixed table of absolute due
 * times (Unix seconds) with a CRC'd blob for NVS. Pure C; the device glue in
 * alarm_clock.c owns the clock, persistence, and firing. */

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <time.h>

#define TIMERS_MAX 8
#define TIMERS_LABEL_MAX 48
#define TIMERS_BLOB_MAX (16 + TIMERS_MAX * 64)

typedef enum {
    TIMER_KIND_TIMER = 0,
    TIMER_KIND_ALARM = 1,
    TIMER_KIND_REMINDER = 2,
} timer_kind_t;

typedef struct {
    uint32_t id;
    uint32_t kind; /* timer_kind_t, fixed width for the blob */
    int64_t due_s;
    char label[TIMERS_LABEL_MAX];
} timer_item_t;

typedef struct {
    timer_item_t items[TIMERS_MAX];
    uint32_t count;
    uint32_t next_id;
} timers_t;

void timers_init(timers_t *t);

/* Returns the new id (>0), or -ENOSPC / -EINVAL. label may be NULL. */
int timers_add(timers_t *t, timer_kind_t kind, int64_t due_s, const char *label);

bool timers_cancel(timers_t *t, uint32_t id);

/* Cancel every item of kind. Returns how many were removed. */
int timers_cancel_kind(timers_t *t, timer_kind_t kind);

/* Soonest item, or NULL when empty. */
const timer_item_t *timers_next(const timers_t *t);

/* Remove the soonest item due at or before now_s into *out. Returns true
 * when one was popped; call again until false. */
bool timers_pop_due(timers_t *t, int64_t now_s, timer_item_t *out);

/* Blob: magic, count, next_id, items, crc32. Returns bytes, or 0 if cap is short. */
size_t timers_serialize(const timers_t *t, uint8_t *buf, size_t cap);

/* Rejects bad magic, count, CRC, kind, or an unterminated label; *t is
 * untouched on failure. */
int timers_parse(timers_t *t, const uint8_t *buf, size_t len);

/* Next local wall-clock time hour:minute strictly after now_s, in the
 * process TZ. -1 on bad input. */
int64_t timers_next_clock(int64_t now_s, int hour, int minute);

/* "m:ss" under an hour, else "h:mm:ss". Negative counts as 0. */
void timers_format_remaining(int64_t seconds, char *out, size_t cap);
