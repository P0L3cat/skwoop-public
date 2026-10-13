/* Host checks for the timer table, its NVS blob, and clock math. */

#include "timers.h"

#include <assert.h>
#include <errno.h>
#include <stdlib.h>
#include <string.h>

static void test_table(void)
{
    timers_t t;
    timer_item_t out;
    int a;
    int b;

    timers_init(&t);
    assert(timers_next(&t) == NULL);
    a = timers_add(&t, TIMER_KIND_TIMER, 1000, NULL);
    b = timers_add(&t, TIMER_KIND_REMINDER, 500, "call mom");
    assert(a == 1 && b == 2);
    assert(timers_add(&t, TIMER_KIND_TIMER, 0, NULL) == -EINVAL);
    assert(timers_add(&t, (timer_kind_t)7, 10, NULL) == -EINVAL);
    assert(timers_next(&t)->id == (uint32_t)b);

    assert(!timers_pop_due(&t, 499, &out));
    assert(timers_pop_due(&t, 500, &out) && out.id == (uint32_t)b && strcmp(out.label, "call mom") == 0);
    assert(!timers_pop_due(&t, 999, &out));
    assert(timers_pop_due(&t, 5000, &out) && out.id == (uint32_t)a);
    assert(!timers_pop_due(&t, 5000, &out));

    for (int i = 0; i < TIMERS_MAX; i++) {
        assert(timers_add(&t, i % 2 ? TIMER_KIND_ALARM : TIMER_KIND_TIMER, 100 + i, NULL) > 0);
    }

    assert(timers_add(&t, TIMER_KIND_TIMER, 1, NULL) == -ENOSPC);
    assert(timers_cancel_kind(&t, TIMER_KIND_TIMER) == TIMERS_MAX / 2);
    assert(t.count == TIMERS_MAX / 2);
    assert(timers_cancel(&t, timers_next(&t)->id));
    assert(!timers_cancel(&t, 9999));
}

static void test_blob(void)
{
    timers_t t;
    timers_t back;
    uint8_t blob[TIMERS_BLOB_MAX];
    size_t n;

    assert(sizeof blob >= 16 + TIMERS_MAX * sizeof(timer_item_t));
    timers_init(&t);
    timers_add(&t, TIMER_KIND_ALARM, 1800000000, "wake up");
    timers_add(&t, TIMER_KIND_TIMER, 1800000300, NULL);
    n = timers_serialize(&t, blob, sizeof blob);
    assert(n > 0);
    assert(timers_serialize(&t, blob, 8) == 0);

    assert(timers_parse(&back, blob, n) == 0);
    assert(back.count == 2 && back.next_id == 3 && strcmp(back.items[0].label, "wake up") == 0);

    timers_init(&back);
    blob[20] ^= 1;
    assert(timers_parse(&back, blob, n) == -EBADMSG && back.count == 0);
    blob[20] ^= 1;
    assert(timers_parse(&back, blob, n - 1) == -EBADMSG);
    assert(timers_parse(&back, blob, 4) == -EINVAL);
}

static void test_clock(void)
{
    char s[16];
    /* 2026-10-07 12:00:00 CDT = 17:00:00 UTC */
    int64_t noon = 1791392400;

    setenv("TZ", "CST6CDT,M3.2.0,M11.1.0", 1);
    tzset();
    assert(timers_next_clock(noon, 13, 30) == noon + 5400);
    assert(timers_next_clock(noon, 7, 0) == noon + 19 * 3600);
    assert(timers_next_clock(noon, 12, 0) == noon + 24 * 3600);
    assert(timers_next_clock(noon, 24, 0) == -1);

    timers_format_remaining(599, s, sizeof s);
    assert(strcmp(s, "9:59") == 0);
    timers_format_remaining(3723, s, sizeof s);
    assert(strcmp(s, "1:02:03") == 0);
    timers_format_remaining(-5, s, sizeof s);
    assert(strcmp(s, "0:00") == 0);
}

int main(void)
{
    test_table();
    test_blob();
    test_clock();

    return 0;
}
