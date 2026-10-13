/* Host check for the mood table. No ESP-IDF and no LVGL.
 * gcc -Wall -Wextra -Werror -I../../../firmware/components/face/include test_face_mood.c ../../../firmware/components/face/face_geom.c
 */

#include "face_geom.h"

#include <assert.h>
#include <stdlib.h>

static int face_reach_x(const face_mood_geom_t *geom)
{
    int dx = geom->dx;

    if (dx < 0) {
        dx = -dx;
    }

    return FACE_EYE_GAP + dx + geom->w / 2;
}

static int face_reach_y(const face_mood_geom_t *geom)
{
    int y = FACE_EYE_Y + geom->dy;

    if (y < 0) {
        y = -y;
    }

    return y + geom->h / 2;
}

int main(void)
{
    const face_mood_geom_t *idle = face_mood_geom(FACE_IDLE);
    const face_mood_geom_t *listen = face_mood_geom(FACE_LISTENING);
    const face_mood_geom_t *think = face_mood_geom(FACE_THINKING);
    const face_mood_geom_t *speak = face_mood_geom(FACE_SPEAKING);
    const face_mood_geom_t *happy = face_mood_geom(FACE_HAPPY);
    const face_mood_geom_t *sleepy = face_mood_geom(FACE_SLEEPY);
    face_mood_t mood;

    assert(idle->w == 58 && idle->h == 78 && idle->dy == 0);
    assert(idle->color == 0x3BD5FF);
    assert(listen->w == 66 && listen->h == 90);
    assert(think->w == 62 && think->h == 36 && think->dx == 12 && think->dy == -14);
    assert(speak->w == 60 && speak->h == 66);
    assert(happy->w == 66 && happy->h == 86 && happy->dy == -4);
    assert(sleepy->w == 56 && sleepy->h == 6 && sleepy->dy == 10);

    assert(listen->h > idle->h);
    assert(think->h < idle->h && think->dy < idle->dy && think->dx > 0);
    assert(happy->w > idle->w && happy->dy < 0 && happy->glow > idle->glow);
    assert(sleepy->h < think->h && sleepy->dy > 0 && sleepy->glow < idle->glow);
    assert(listen->color != idle->color);
    assert(think->color != idle->color);
    assert(speak->color != listen->color);
    assert(happy->color == idle->color);

    for (mood = FACE_IDLE; mood < FACE_MOOD_COUNT; mood = (face_mood_t)(mood + 1)) {
        const face_mood_geom_t *geom = face_mood_geom(mood);

        assert(geom->w > 0 && (geom->w % 2) == 0);
        assert(geom->h >= 4);
        assert(geom->w / 2 > 0);
        assert(face_reach_x(geom) < FACE_PANEL / 2);
        assert(face_reach_y(geom) < FACE_PANEL / 2);
    }

    assert(face_mood_geom((face_mood_t)255) == idle);

    {
        static const char *const names[] = { "idle", "plain", "happy", "calm", "sad", "alert" };
        static const face_mood_t want[] = { FACE_IDLE, FACE_SPEAKING, FACE_HAPPY, FACE_CALM, FACE_SAD, FACE_ALERT };
        const face_mood_geom_t *calm = face_mood_geom(FACE_CALM);
        const face_mood_geom_t *sad = face_mood_geom(FACE_SAD);
        const face_mood_geom_t *alert = face_mood_geom(FACE_ALERT);
        face_mood_t got = FACE_SLEEPY;

        for (size_t i = 0; i < sizeof names / sizeof names[0]; i++) {
            assert(face_mood_for_reply(names[i], &got) && got == want[i]);
        }

        got = FACE_SLEEPY;
        assert(!face_mood_for_reply("angry", &got) && got == FACE_SLEEPY);
        assert(!face_mood_for_reply(NULL, &got));

        /* Web pod poses 2..4: calm low and soft, sad droopy and dim, alert tall and bright. */
        assert(calm->w == 62 && calm->h == 56 && calm->dy == 4 && calm->glow == 110);
        assert(sad->w == 52 && sad->h == 60 && sad->dy == 8 && sad->glow == 95);
        assert(alert->w == 54 && alert->h == 92 && alert->dy == -6 && alert->glow == 185);
        assert(sad->glow < calm->glow && calm->glow < idle->glow && alert->glow > idle->glow);
        assert(alert->h > idle->h && sad->dy > 0 && alert->dy < 0);
    }

    return 0;
}
