#pragma once

#include "face_geom.h"

#include "lvgl.h"

/* Parent becomes the round near-black stage. Safe to call once after board_init. */
void face_init(lv_obj_t *parent);

/* Ease eye size, offset, and color to this mood (~200 ms). */
void face_set_mood(face_mood_t mood);

/* 0..100. While speaking, this opens the eye glow from the mood's rest to full. */
void face_set_level(uint8_t level);

/* Timer countdown under the eyes ("9:59"); NULL hides it. Any task. */
void face_set_countdown(const char *text);
