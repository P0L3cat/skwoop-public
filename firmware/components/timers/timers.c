#include "timers.h"

#include <errno.h>
#include <stdio.h>
#include <string.h>

#define TIMERS_MAGIC 0x31524d54u /* "TMR1" */

void timers_init(timers_t *t)
{
    memset(t, 0, sizeof *t);
    t->next_id = 1;
}

int timers_add(timers_t *t, timer_kind_t kind, int64_t due_s, const char *label)
{
    timer_item_t *item;

    if (t == NULL || (unsigned)kind > TIMER_KIND_REMINDER || due_s <= 0) {
        return -EINVAL;
    }

    if (t->count >= TIMERS_MAX) {
        return -ENOSPC;
    }

    item = &t->items[t->count++];
    memset(item, 0, sizeof *item);
    item->id = t->next_id++;
    item->kind = (uint32_t)kind;
    item->due_s = due_s;

    if (label != NULL) {
        snprintf(item->label, sizeof item->label, "%s", label);
    }

    return (int)item->id;
}

static void remove_at(timers_t *t, uint32_t i)
{
    t->items[i] = t->items[--t->count];
}

bool timers_cancel(timers_t *t, uint32_t id)
{
    for (uint32_t i = 0; i < t->count; i++) {
        if (t->items[i].id == id) {
            remove_at(t, i);

            return true;
        }
    }

    return false;
}

int timers_cancel_kind(timers_t *t, timer_kind_t kind)
{
    int removed = 0;

    for (uint32_t i = 0; i < t->count;) {
        if (t->items[i].kind == (uint32_t)kind) {
            remove_at(t, i);
            removed++;
        } else {
            i++;
        }
    }

    return removed;
}

static int soonest(const timers_t *t)
{
    int best = -1;

    for (uint32_t i = 0; i < t->count; i++) {
        if (best < 0 || t->items[i].due_s < t->items[best].due_s) {
            best = (int)i;
        }
    }

    return best;
}

const timer_item_t *timers_next(const timers_t *t)
{
    int i = soonest(t);

    return i < 0 ? NULL : &t->items[i];
}

bool timers_pop_due(timers_t *t, int64_t now_s, timer_item_t *out)
{
    int i = soonest(t);

    if (i < 0 || t->items[i].due_s > now_s) {
        return false;
    }

    *out = t->items[i];
    remove_at(t, (uint32_t)i);

    return true;
}

static uint32_t crc32(const uint8_t *p, size_t len)
{
    uint32_t crc = 0xFFFFFFFFu;

    while (len-- > 0) {
        crc ^= *p++;

        for (int k = 0; k < 8; k++) {
            crc = (crc >> 1) ^ (0xEDB88320u & (0u - (crc & 1u)));
        }
    }

    return ~crc;
}

size_t timers_serialize(const timers_t *t, uint8_t *buf, size_t cap)
{
    uint32_t head[3] = { TIMERS_MAGIC, t->count, t->next_id };
    size_t body = t->count * sizeof(timer_item_t);
    size_t total = sizeof head + body + sizeof(uint32_t);
    uint32_t crc;

    if (cap < total) {
        return 0;
    }

    memcpy(buf, head, sizeof head);
    memcpy(buf + sizeof head, t->items, body);
    crc = crc32(buf, sizeof head + body);
    memcpy(buf + sizeof head + body, &crc, sizeof crc);

    return total;
}

int timers_parse(timers_t *t, const uint8_t *buf, size_t len)
{
    timers_t parsed;
    uint32_t head[3];
    uint32_t crc;
    size_t body;

    if (t == NULL || buf == NULL || len < sizeof head + sizeof crc) {
        return -EINVAL;
    }

    memcpy(head, buf, sizeof head);

    if (head[0] != TIMERS_MAGIC || head[1] > TIMERS_MAX) {
        return -EBADMSG;
    }

    body = head[1] * sizeof(timer_item_t);

    if (len != sizeof head + body + sizeof crc) {
        return -EBADMSG;
    }

    memcpy(&crc, buf + sizeof head + body, sizeof crc);

    if (crc != crc32(buf, sizeof head + body)) {
        return -EBADMSG;
    }

    timers_init(&parsed);
    parsed.count = head[1];
    parsed.next_id = head[2];
    memcpy(parsed.items, buf + sizeof head, body);

    for (uint32_t i = 0; i < parsed.count; i++) {
        const timer_item_t *it = &parsed.items[i];

        if (it->kind > TIMER_KIND_REMINDER || it->due_s <= 0 || it->id == 0 || it->id >= parsed.next_id
            || memchr(it->label, '\0', sizeof it->label) == NULL) {
            return -EBADMSG;
        }
    }

    *t = parsed;

    return 0;
}

int64_t timers_next_clock(int64_t now_s, int hour, int minute)
{
    time_t now = (time_t)now_s;
    struct tm tm;
    time_t at;

    if (hour < 0 || hour > 23 || minute < 0 || minute > 59 || localtime_r(&now, &tm) == NULL) {
        return -1;
    }

    tm.tm_hour = hour;
    tm.tm_min = minute;
    tm.tm_sec = 0;
    tm.tm_isdst = -1;
    at = mktime(&tm);

    if (at <= now) {
        tm.tm_mday += 1;
        tm.tm_hour = hour;
        tm.tm_min = minute;
        tm.tm_isdst = -1;
        at = mktime(&tm);
    }

    return (int64_t)at;
}

void timers_format_remaining(int64_t seconds, char *out, size_t cap)
{
    long s = seconds < 0 ? 0 : (long)seconds;

    if (s < 3600) {
        snprintf(out, cap, "%ld:%02ld", s / 60, s % 60);
    } else {
        snprintf(out, cap, "%ld:%02ld:%02ld", s / 3600, (s / 60) % 60, s % 60);
    }
}
