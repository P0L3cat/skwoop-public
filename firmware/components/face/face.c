#include "face.h"

#include "esp_log.h"
#include "esp_lvgl_port.h"
#include "esp_random.h"

#include <stdbool.h>

#define FACE_BG 0x05070A
#define FACE_COUNTDOWN_RGB 0x7FD8FF
#define FACE_COUNTDOWN_INSET 70
#define FACE_MOOD_MS 200
#define FACE_BLINK_MIN_MS 3000
#define FACE_BLINK_SPAN_MS 3001
#define FACE_GAZE_HOLD_MIN_MS 1500
#define FACE_GAZE_HOLD_SPAN_MS 2500
#define FACE_GAZE_X 8
#define FACE_GAZE_Y 3
#define FACE_LIFE_MS 40
#define FACE_EYE_MIN_H 4
#define FACE_SHADOW_W 16
#define FACE_SHADOW_SPREAD 2
#define FACE_CORE_WHITE 56
#define FACE_LEVEL_MAX 100

static const char *TAG = "face";

static lv_obj_t *s_eye[2];
static lv_obj_t *s_countdown;
static lv_timer_t *s_life;
static bool s_ready;
static char s_anim_tag;

static face_mood_t s_mood = FACE_IDLE;
static face_mood_geom_t s_cur;
static face_mood_geom_t s_from;
static face_mood_geom_t s_to;
static uint8_t s_level;
static uint16_t s_blink = 256;
static uint8_t s_blink_i;
static uint32_t s_blink_at;
static int16_t s_gaze_x;
static int16_t s_gaze_y;
static int16_t s_gaze_tx;
static int16_t s_gaze_ty;
static uint32_t s_gaze_at;

/* Reference blink: 0.55, 0.12, 0.08, 0.35, 0.8 of the open height. */
static const uint16_t s_blink_seq[] = {141, 31, 20, 90, 205};

static bool face_lock(void)
{
    if (!lvgl_port_lock(0)) {
        ESP_LOGE(TAG, "lvgl lock failed");

        return false;
    }

    return true;
}

static int16_t face_lerp(int16_t from, int16_t to, int32_t t)
{
    return (int16_t)(from + ((int32_t)to - from) * t / 1000);
}

static uint32_t face_lerp_rgb(uint32_t from, uint32_t to, int32_t t)
{
    uint32_t rgb = 0;
    int shift;

    for (shift = 16; shift >= 0; shift -= 8) {
        int32_t a = (int32_t)((from >> shift) & 0xFF);
        int32_t b = (int32_t)((to >> shift) & 0xFF);

        rgb |= (uint32_t)(a + (b - a) * t / 1000) << shift;
    }

    return rgb;
}

static int16_t face_rand_span(int16_t span)
{
    uint32_t n = (uint32_t)(span * 2 + 1);

    return (int16_t)(esp_random() % n) - span;
}

static void face_plain(lv_obj_t *obj)
{
    lv_obj_remove_style_all(obj);
    lv_obj_remove_flag(obj, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_add_flag(obj, LV_OBJ_FLAG_EVENT_BUBBLE);
}

static void face_apply(void)
{
    int32_t h = (int32_t)s_cur.h * (int32_t)s_blink / 256;
    int32_t radius = (int32_t)s_cur.w / 2;
    int32_t x = (int32_t)s_cur.dx + s_gaze_x;
    int32_t y = (int32_t)FACE_EYE_Y + s_cur.dy + s_gaze_y;
    int32_t glow = s_cur.glow;
    int32_t shadow = FACE_SHADOW_W;
    lv_color_t hue = lv_color_hex(s_cur.color);
    lv_color_t core = lv_color_mix(lv_color_white(), hue, FACE_CORE_WHITE);
    int i;

    if (h < FACE_EYE_MIN_H) {
        h = FACE_EYE_MIN_H;
    }

    if (s_mood == FACE_SPEAKING) {
        glow += (255 - glow) * (int32_t)s_level / FACE_LEVEL_MAX;
        shadow += (int32_t)s_level / 8;
    }

    for (i = 0; i < 2; i++) {
        int side = (i == 0) ? -FACE_EYE_GAP : FACE_EYE_GAP;

        lv_obj_set_size(s_eye[i], s_cur.w, h);
        lv_obj_set_style_radius(s_eye[i], radius, 0);
        lv_obj_set_style_bg_color(s_eye[i], core, 0);
        lv_obj_set_style_shadow_color(s_eye[i], hue, 0);
        lv_obj_set_style_shadow_opa(s_eye[i], (lv_opa_t)glow, 0);
        lv_obj_set_style_shadow_width(s_eye[i], shadow, 0);
        lv_obj_align(s_eye[i], LV_ALIGN_CENTER, side + x, y);
    }
}

static void face_anim_exec(void *var, int32_t t)
{
    (void)var;

    if (t < 0) {
        t = 0;
    }

    if (t > 1000) {
        t = 1000;
    }

    s_cur.w = face_lerp(s_from.w, s_to.w, t);
    s_cur.h = face_lerp(s_from.h, s_to.h, t);
    s_cur.dx = face_lerp(s_from.dx, s_to.dx, t);
    s_cur.dy = face_lerp(s_from.dy, s_to.dy, t);
    s_cur.color = face_lerp_rgb(s_from.color, s_to.color, t);
    s_cur.glow = (uint8_t)face_lerp((int16_t)s_from.glow, (int16_t)s_to.glow, t);
    face_apply();
}

static void face_anim_done(lv_anim_t *anim)
{
    (void)anim;
    s_cur = s_to;
    face_apply();
}

/* s_blink_i is 0 when open, else the 1-based step currently shown.
 * Called from lv_timer_handler, which already holds lvgl_port_lock. */
static void face_blink(uint32_t now, bool *dirty)
{
    uint8_t steps = (uint8_t)(sizeof(s_blink_seq) / sizeof(s_blink_seq[0]));

    if (s_blink_i > 0) {
        s_blink_i++;

        if (s_blink_i > steps) {
            s_blink_i = 0;
            s_blink = 256;
            s_blink_at = now + FACE_BLINK_MIN_MS + (esp_random() % FACE_BLINK_SPAN_MS);
        } else {
            s_blink = s_blink_seq[s_blink_i - 1];
        }

        *dirty = true;

        return;
    }

    if ((int32_t)(now - s_blink_at) < 0) {
        return;
    }

    s_blink_i = 1;
    s_blink = s_blink_seq[0];
    *dirty = true;
}

static void face_gaze(uint32_t now, bool *dirty)
{
    if (s_blink_i != 0) {
        return;
    }

    if (s_gaze_x != s_gaze_tx || s_gaze_y != s_gaze_ty) {
        if (s_gaze_x < s_gaze_tx) {
            s_gaze_x++;
        } else if (s_gaze_x > s_gaze_tx) {
            s_gaze_x--;
        }

        if (s_gaze_y < s_gaze_ty) {
            s_gaze_y++;
        } else if (s_gaze_y > s_gaze_ty) {
            s_gaze_y--;
        }

        *dirty = true;

        if (s_gaze_x == s_gaze_tx && s_gaze_y == s_gaze_ty) {
            s_gaze_at = now + FACE_GAZE_HOLD_MIN_MS + (esp_random() % FACE_GAZE_HOLD_SPAN_MS);
        }

        return;
    }

    if ((int32_t)(now - s_gaze_at) < 0) {
        return;
    }

    s_gaze_tx = face_rand_span(FACE_GAZE_X);
    s_gaze_ty = face_rand_span(FACE_GAZE_Y);
}

static void face_life(lv_timer_t *timer)
{
    uint32_t now = lv_tick_get();
    bool dirty = false;

    (void)timer;
    face_blink(now, &dirty);
    face_gaze(now, &dirty);

    if (dirty) {
        face_apply();
    }
}

static void face_style_eye(lv_obj_t *eye)
{
    face_plain(eye);
    lv_obj_set_style_bg_opa(eye, LV_OPA_COVER, 0);
    lv_obj_set_style_border_width(eye, 0, 0);
    lv_obj_set_style_outline_width(eye, 0, 0);
    lv_obj_set_style_pad_all(eye, 0, 0);
    lv_obj_set_style_shadow_width(eye, FACE_SHADOW_W, 0);
    lv_obj_set_style_shadow_spread(eye, FACE_SHADOW_SPREAD, 0);
    lv_obj_set_style_shadow_offset_x(eye, 0, 0);
    lv_obj_set_style_shadow_offset_y(eye, 0, 0);
}

void face_init(lv_obj_t *parent)
{
    lv_obj_t *root;
    int32_t side;
    uint32_t now;
    int i;

    if (parent == NULL || s_ready) {
        return;
    }

    if (!face_lock()) {
        return;
    }

    side = lv_obj_get_width(parent);

    if (lv_obj_get_height(parent) < side) {
        side = lv_obj_get_height(parent);
    }

    if (side < 2) {
        side = FACE_PANEL;
    }

    lv_obj_set_style_bg_color(parent, lv_color_hex(FACE_BG), 0);
    lv_obj_set_style_bg_opa(parent, LV_OPA_COVER, 0);
    lv_obj_remove_flag(parent, LV_OBJ_FLAG_SCROLLABLE);

    root = lv_obj_create(parent);
    face_plain(root);
    lv_obj_set_size(root, side, side);
    lv_obj_center(root);
    lv_obj_set_style_radius(root, LV_RADIUS_CIRCLE, 0);
    lv_obj_set_style_clip_corner(root, true, 0);
    lv_obj_set_style_bg_color(root, lv_color_hex(FACE_BG), 0);
    lv_obj_set_style_bg_opa(root, LV_OPA_COVER, 0);
    lv_obj_set_style_border_width(root, 0, 0);

    for (i = 0; i < 2; i++) {
        s_eye[i] = lv_obj_create(root);
        face_style_eye(s_eye[i]);
    }

    s_countdown = lv_label_create(root);
    lv_obj_set_style_text_font(s_countdown, &lv_font_montserrat_40, 0);
    lv_obj_set_style_text_color(s_countdown, lv_color_hex(FACE_COUNTDOWN_RGB), 0);
    lv_obj_align(s_countdown, LV_ALIGN_BOTTOM_MID, 0, -FACE_COUNTDOWN_INSET);
    lv_obj_add_flag(s_countdown, LV_OBJ_FLAG_HIDDEN);

    s_mood = FACE_IDLE;
    s_cur = *face_mood_geom(FACE_IDLE);
    s_level = 0;
    s_blink = 256;
    s_blink_i = 0;
    s_gaze_x = 0;
    s_gaze_y = 0;
    s_gaze_tx = 0;
    s_gaze_ty = 0;
    now = lv_tick_get();
    s_blink_at = now + FACE_BLINK_MIN_MS + (esp_random() % FACE_BLINK_SPAN_MS);
    s_gaze_at = now + FACE_GAZE_HOLD_MIN_MS + (esp_random() % FACE_GAZE_HOLD_SPAN_MS);
    face_apply();

    s_life = lv_timer_create(face_life, FACE_LIFE_MS, NULL);

    if (s_life == NULL) {
        ESP_LOGE(TAG, "life timer failed");
    }

    s_ready = true;
    lvgl_port_unlock();
}

void face_set_mood(face_mood_t mood)
{
    lv_anim_t anim;

    if ((unsigned)mood >= FACE_MOOD_COUNT) {
        mood = FACE_IDLE;
    }

    if (!face_lock()) {
        return;
    }

    if (!s_ready || mood == s_mood) {
        lvgl_port_unlock();

        return;
    }

    s_from = s_cur;
    s_to = *face_mood_geom(mood);
    s_mood = mood;

    lv_anim_init(&anim);
    lv_anim_set_var(&anim, &s_anim_tag);
    lv_anim_set_exec_cb(&anim, face_anim_exec);
    lv_anim_set_values(&anim, 0, 1000);
    lv_anim_set_duration(&anim, FACE_MOOD_MS);
    lv_anim_set_path_cb(&anim, lv_anim_path_ease_in_out);
    lv_anim_set_completed_cb(&anim, face_anim_done);

    if (lv_anim_start(&anim) == NULL) {
        s_cur = s_to;
        face_apply();
    }

    lvgl_port_unlock();
}

void face_set_level(uint8_t level)
{
    if (level > FACE_LEVEL_MAX) {
        level = FACE_LEVEL_MAX;
    }

    if (!face_lock()) {
        return;
    }

    if (!s_ready || level == s_level) {
        lvgl_port_unlock();

        return;
    }

    s_level = level;

    if (s_mood == FACE_SPEAKING) {
        face_apply();
    }

    lvgl_port_unlock();
}

void face_set_countdown(const char *text)
{
    if (!face_lock()) {
        return;
    }

    if (s_ready && text != NULL) {
        lv_label_set_text(s_countdown, text);
        lv_obj_remove_flag(s_countdown, LV_OBJ_FLAG_HIDDEN);
    } else if (s_ready) {
        lv_obj_add_flag(s_countdown, LV_OBJ_FLAG_HIDDEN);
    }

    lvgl_port_unlock();
}
