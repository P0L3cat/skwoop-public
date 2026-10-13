/* Host check for the offline command table and dispatch. No ESP-IDF. */

#include "cmds.h"

#include <assert.h>
#include <ctype.h>
#include <string.h>

typedef struct {
    int timer_set;
    uint32_t seconds;
    int timer_cancel;
    int volume;
    int mute_on;
    int mute_off;
    int lights_on;
    int lights_off;
    int stop;
} calls_t;

static calls_t g;

static void on_timer_set(uint32_t s)
{
    g.timer_set++;
    g.seconds = s;
}

static void on_timer_cancel(void)
{
    g.timer_cancel++;
}

static void on_volume(int d)
{
    g.volume += d;
}

static void on_mute(bool on)
{
    on ? g.mute_on++ : g.mute_off++;
}

static void on_lights(bool on)
{
    on ? g.lights_on++ : g.lights_off++;
}

static void on_stop(void)
{
    g.stop++;
}

static const cmd_handlers_t H = {
    .timer_set = on_timer_set,
    .timer_cancel = on_timer_cancel,
    .volume_step = on_volume,
    .mute = on_mute,
    .lights = on_lights,
    .stop = on_stop,
};

static void test_table_shape(void)
{
    size_t n;
    const cmd_entry_t *t = cmds_table(&n);
    int seen_action[CMD_STOP + 1] = { 0 };

    assert(n > 0);

    for (size_t i = 0; i < n; i++) {
        assert(t[i].id > 0);
        assert(strlen(t[i].phrase) > 0 && strlen(t[i].phrase) < 64);
        assert((t[i].action == CMD_TIMER_SET) == (t[i].seconds > 0));
        seen_action[t[i].action] = 1;

        for (const char *p = t[i].phrase; *p != '\0'; p++) {
            assert(islower((unsigned char)*p) || *p == ' ');
        }

        for (size_t j = i + 1; j < n; j++) {
            assert(t[i].id != t[j].id);
            assert(strcmp(t[i].phrase, t[j].phrase) != 0);
        }
    }

    for (int a = 0; a <= CMD_STOP; a++) {
        assert(seen_action[a]);
    }
}

static int id_of(const char *phrase)
{
    size_t n;
    const cmd_entry_t *t = cmds_table(&n);

    for (size_t i = 0; i < n; i++) {
        if (strcmp(t[i].phrase, phrase) == 0) {
            return t[i].id;
        }
    }

    return -1;
}

static void test_dispatch(void)
{
    assert(cmds_dispatch(id_of("set a timer for ten minutes"), &H) && g.timer_set == 1 && g.seconds == 600);
    assert(cmds_dispatch(id_of("cancel the timer"), &H) && g.timer_cancel == 1);
    assert(cmds_dispatch(id_of("volume up"), &H) && g.volume == CMDS_VOLUME_STEP);
    assert(cmds_dispatch(id_of("turn it down"), &H) && g.volume == 0);
    assert(cmds_dispatch(id_of("mute"), &H) && cmds_dispatch(id_of("unmute"), &H) && g.mute_on == 1 && g.mute_off == 1);
    assert(cmds_dispatch(id_of("turn on the lights"), &H) && g.lights_on == 1);
    assert(cmds_dispatch(id_of("lights off"), &H) && g.lights_off == 1);
    assert(cmds_dispatch(id_of("stop"), &H) && g.stop == 1);
    assert(!cmds_dispatch(999, &H));
    assert(!cmds_dispatch(id_of("stop"), NULL));
}

int main(void)
{
    test_table_shape();
    test_dispatch();

    return 0;
}
