#include "face_geom.h"

#include <stddef.h>
#include <string.h>

/* Idle cyan is the flat fill sampled from ref/face/va-grid-robot.png (#3BD5FF).
 * Listening / thinking / speaking use the FaceStyle hues kIce, kViolet, kMint.
 * The patch names those constants but does not list their hex, so these are the
 * same bright families. Sleepy is the fixed sleep overlay (56×6, dropped, dim).
 * glow is shadow opacity 50 + 150×pose glow (neutral 0.6 -> 140, happy 1.0 -> 200).
 * Calm / sad / alert are the web pod's mood poses 2..4 (face-data.json poses,
 * the same table expressions.json builds on): eye_w, eye_h, eye_dy, glow. They
 * are reply moods, so they keep the speaking hue the web pod shows them in. */
static const face_mood_geom_t s_geom[FACE_MOOD_COUNT] = {
    {58, 78, 0, 0, 0x3BD5FF, 140},
    {66, 90, 0, 0, 0x9AF4FF, 168},
    {62, 36, 12, -14, 0xC9A6FF, 150},
    {60, 66, 0, 0, 0x5EEAD4, 155},
    {66, 86, 0, -4, 0x3BD5FF, 200},
    {56, 6, 0, 10, 0x8AA0B3, 48},
    {62, 56, 0, 4, 0x5EEAD4, 110},
    {52, 60, 0, 8, 0x5EEAD4, 95},
    {54, 92, 0, -6, 0x5EEAD4, 185},
};

const face_mood_geom_t *face_mood_geom(face_mood_t mood)
{
    if ((unsigned)mood >= FACE_MOOD_COUNT) {
        mood = FACE_IDLE;
    }

    return &s_geom[mood];
}

typedef struct {
    const char *name;
    face_mood_t face;
} face_reply_map_t;

/* plain is the neutral speaking face; idle is the rest pose. */
static const face_reply_map_t s_reply[] = {
    {"idle", FACE_IDLE},
    {"plain", FACE_SPEAKING},
    {"happy", FACE_HAPPY},
    {"calm", FACE_CALM},
    {"sad", FACE_SAD},
    {"alert", FACE_ALERT},
};

bool face_mood_for_reply(const char *name, face_mood_t *out)
{
    if (name == NULL || out == NULL) {
        return false;
    }

    for (size_t i = 0; i < sizeof s_reply / sizeof s_reply[0]; i++) {
        if (strcmp(name, s_reply[i].name) == 0) {
            *out = s_reply[i].face;

            return true;
        }
    }

    return false;
}
