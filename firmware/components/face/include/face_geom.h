#pragma once

#include <stdbool.h>
#include <stdint.h>

/* Offsets are from screen center. dx is added to both eyes (a shared look);
 * each eye then sits ±FACE_EYE_GAP on x. Numbers are the robot visor in ref/face. */
#define FACE_EYE_GAP 62
#define FACE_EYE_Y (-8)
#define FACE_PANEL 466

typedef enum {
    FACE_IDLE = 0,
    FACE_LISTENING,
    FACE_THINKING,
    FACE_SPEAKING,
    FACE_HAPPY,
    FACE_SLEEPY,
    FACE_CALM,
    FACE_SAD,
    FACE_ALERT,
    FACE_MOOD_COUNT
} face_mood_t;

typedef struct {
    int16_t w;
    int16_t h;
    int16_t dx;
    int16_t dy;
    uint32_t color;
    uint8_t glow;
} face_mood_geom_t;

/* Out-of-range moods return idle. */
const face_mood_geom_t *face_mood_geom(face_mood_t mood);

/* Reply-mood name from the mood model (idle, plain, happy, calm, sad, alert)
 * to the face that shows it. False (and *out untouched) for any other name. */
bool face_mood_for_reply(const char *name, face_mood_t *out);
