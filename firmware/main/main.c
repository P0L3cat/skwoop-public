#include "alarm_clock.h"
#include "board.h"
#include "bridge.h"
#include "cmds.h"
#include "face.h"
#include "mqtt_lite.h"
#include "skwoop_config.h"
#include "storage.h"
#include "voice.h"
#include "wake.h"
#include "wifi.h"

#if CONFIG_SKWOOP_STANDALONE
#include "standalone.h"
#endif

#include <math.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "esp_log.h"
#include "esp_mac.h"
#include "esp_lvgl_port.h"

#define MAIN_HAPPY_MS 1000
#define MAIN_LEVEL_MS 100
#define MAIN_AUDIO_LOG_EVERY 50
#define MAIN_LISTEN_JSON 128
#define MAIN_VOLUME_DEFAULT 70
#define MAIN_RING_RATE 24000
#define MAIN_RING_HZ 880.0f
#define MAIN_RING_BEEP_MS 200
#define MAIN_RING_BEEPS 3
#define MAIN_RING_AMP 9000.0f

static const char *TAG = "main";

/* Touched only while lvgl_port_lock is held. set_mood takes it (button task,
 * wifi callback, and the bridge task). Touch and the happy timer already run
 * inside the LVGL task's lock. */
static face_mood_t s_mood = FACE_IDLE;
static lv_timer_t *s_happy_timer;
static bool s_bridge_started;
static bool s_listening;
static unsigned s_audio_packets;
#if CONFIG_SKWOOP_STANDALONE
static bool s_standalone;
#endif
static size_t s_audio_bytes;
static int s_volume = MAIN_VOLUME_DEFAULT;

static const char *face_mood_name(face_mood_t mood)
{
    static const char *names[FACE_MOOD_COUNT] = {
        "idle", "listening", "thinking", "speaking", "happy", "sleepy", "calm", "sad", "alert",
    };

    if ((unsigned)mood >= FACE_MOOD_COUNT) {
        return "idle";
    }

    return names[mood];
}

static void cancel_happy(void)
{
    if (s_happy_timer != NULL) {
        lv_timer_delete(s_happy_timer);
        s_happy_timer = NULL;
    }
}

static void on_happy_done(lv_timer_t *timer)
{
    (void)timer;
    s_happy_timer = NULL;
    face_set_mood(s_mood);
    ESP_LOGI(TAG, "flash done -> %s", face_mood_name(s_mood));
}

static void set_mood(face_mood_t mood)
{
    if (!lvgl_port_lock(0)) {
        ESP_LOGE(TAG, "lvgl lock failed");

        return;
    }

    cancel_happy();
    s_mood = mood;
    face_set_mood(s_mood);
    ESP_LOGI(TAG, "mood %s", face_mood_name(s_mood));
    lvgl_port_unlock();
}

/* Caller holds the LVGL lock. Show face for MAIN_HAPPY_MS, then back to s_mood. */
static void start_flash(face_mood_t face)
{
    if (s_happy_timer == NULL) {
        s_happy_timer = lv_timer_create(on_happy_done, MAIN_HAPPY_MS, NULL);

        if (s_happy_timer == NULL) {
            ESP_LOGE(TAG, "happy timer failed");

            return;
        }

        lv_timer_set_repeat_count(s_happy_timer, 1);
    } else {
        lv_timer_reset(s_happy_timer);
    }

    face_set_mood(face);
}

/* Caller holds the LVGL lock. */
static void start_happy(void)
{
    start_flash(FACE_HAPPY);
}

static void on_tap(lv_event_t *event)
{
    (void)event;
    start_happy();
    ESP_LOGI(TAG, "tap -> happy");
}

static void on_bridge_msg(const bridge_msg_t *msg)
{
    if (msg == NULL) {
        return;
    }

    switch (msg->kind) {
    case BRIDGE_LLM:
        if (strcmp(msg->llm.emotion, "happy") == 0) {
            set_mood(FACE_HAPPY);
        } else if (strcmp(msg->llm.emotion, "thinking") == 0) {
            set_mood(FACE_THINKING);
        }

        break;
    case BRIDGE_TTS:
        if (msg->tts.state == BRIDGE_TTS_START) {
            s_listening = false;
            voice_set_listening(false);
            set_mood(FACE_SPEAKING);
        } else if (msg->tts.state == BRIDGE_TTS_STOP) {
            set_mood(FACE_IDLE);
        }

        break;
    case BRIDGE_STT:
        ESP_LOGI(TAG, "stt %s", msg->stt.text);
        break;
    case BRIDGE_LISTEN_STOP:
        s_listening = false;
        voice_set_listening(false);
        set_mood(FACE_IDLE);
        break;
    default:
        break;
    }
}

static void on_bridge_audio(const uint8_t *data, size_t len)
{
    voice_feed_opus(data, len);
    s_audio_bytes += len;
    s_audio_packets += 1;

    if (s_audio_packets % MAIN_AUDIO_LOG_EVERY != 0) {
        return;
    }

    ESP_LOGI(TAG, "audio %u packets, %u bytes", s_audio_packets, (unsigned)s_audio_bytes);
}

static void on_wake(const char *word)
{
    char json[MAIN_LISTEN_JSON];
    int wrote;
    size_t n;

#if CONFIG_SKWOOP_STANDALONE
    if (s_standalone) {
        standalone_listen();

        return;
    }
#endif

    if (word == NULL || word[0] == '\0') {
        word = "Hi ESP";
    }

    set_mood(FACE_LISTENING);
    wrote = snprintf(json, sizeof json,
                     "{\"type\":\"listen\",\"state\":\"detect\",\"mode\":\"auto\",\"wake_word\":\"%s\"}",
                     word);

    if (wrote > 0 && (size_t)wrote < sizeof json) {
        (void)bridge_send_text(json);
    }

    n = bridge_build_listen(json, sizeof json, "start", "auto");

    if (n > 0) {
        (void)bridge_send_text(json);
    }

    s_listening = true;
    voice_set_listening(true);
}

static void stop_listening(void)
{
    char json[MAIN_LISTEN_JSON];

#if CONFIG_SKWOOP_STANDALONE
    if (s_standalone) {
        standalone_cancel();

        return;
    }
#endif

    if (s_listening && bridge_build_listen(json, sizeof json, "stop", "auto") > 0) {
        (void)bridge_send_text(json);
    }

    s_listening = false;
    voice_set_listening(false);
}

static void cmd_timer_set(uint32_t seconds)
{
    int id = alarm_clock_add_in(TIMER_KIND_TIMER, seconds, NULL);

    if (id < 0) {
        ESP_LOGW(TAG, "timer %u s not set: %d", (unsigned)seconds, id);
    }
}

static void cmd_timer_cancel(void)
{
    (void)alarm_clock_cancel_kind(TIMER_KIND_TIMER);
}

static void cmd_volume_step(int delta)
{
    s_volume = s_volume + delta < 0 ? 0 : (s_volume + delta > 100 ? 100 : s_volume + delta);
    (void)board_set_volume(s_volume);
}

static void cmd_mute(bool on)
{
    (void)board_set_mute(on);
}

static void cmd_lights(bool on)
{
    if (!mqtt_lite_publish(CONFIG_SKWOOP_LIGHTS_TOPIC, on ? "ON" : "OFF", false)) {
        ESP_LOGW(TAG, "lights %s: MQTT not configured or busy", on ? "on" : "off");
    }
}

static void cmd_stop(void)
{
    ESP_LOGI(TAG, "cmd stop");
}

static const cmd_handlers_t s_cmd_handlers = {
    .timer_set = cmd_timer_set,
    .timer_cancel = cmd_timer_cancel,
    .volume_step = cmd_volume_step,
    .mute = cmd_mute,
    .lights = cmd_lights,
    .stop = cmd_stop,
};

/* Wake task: MultiNet matched an offline command. The cloud/bridge turn that
 * the wake started is dropped and the local handler runs. */
static void on_command(int id)
{
    stop_listening();

    set_mood(FACE_IDLE);

    if (!cmds_dispatch(id, &s_cmd_handlers)) {
        ESP_LOGW(TAG, "unknown command %d", id);

        return;
    }

    if (lvgl_port_lock(0)) {
        start_happy();
        lvgl_port_unlock();
    }
}

/* Alarm-clock tick task: three short beeps and a happy flash. */
static void on_timer_fire(const timer_item_t *item)
{
    size_t n = MAIN_RING_RATE / 1000 * MAIN_RING_BEEP_MS;
    int16_t *tone = calloc(n, sizeof(int16_t));

    ESP_LOGI(TAG, "timer %u fired (%s)", (unsigned)item->id, item->label);

    if (lvgl_port_lock(0)) {
        start_happy();
        lvgl_port_unlock();
    }

    if (tone == NULL) {
        return;
    }

    /* First half tone, second half silence: one beep per buffer. */
    for (size_t i = 0; i < n / 2; i++) {
        tone[i] = (int16_t)(MAIN_RING_AMP * sinf(2.0f * (float)M_PI * MAIN_RING_HZ * (float)i / MAIN_RING_RATE));
    }

    for (int k = 0; k < MAIN_RING_BEEPS; k++) {
        voice_play_pcm(tone, n);
    }

    free(tone);
}

static void on_button_press(void)
{
    char json[MAIN_LISTEN_JSON];
    const char *state = s_listening ? "stop" : "start";
    size_t n;

#if CONFIG_SKWOOP_STANDALONE
    if (s_standalone) {
        standalone_listen();

        return;
    }
#endif

    n = bridge_build_listen(json, sizeof json, state, "manual");

    if (n == 0 || !bridge_send_text(json)) {
        ESP_LOGW(TAG, "listen %s not sent", state);

        return;
    }

    s_listening = !s_listening;
    voice_set_listening(s_listening);
    set_mood(s_listening ? FACE_LISTENING : FACE_IDLE);
}

static void on_button_long_press(void)
{
    ESP_LOGI(TAG, "button long-press");
}

static void on_level(lv_timer_t *timer)
{
    (void)timer;

    if (s_mood != FACE_SPEAKING) {
        return;
    }

    face_set_level(voice_level());
}

static void show_face(void)
{
    lv_obj_t *screen;

    if (!lvgl_port_lock(0)) {
        ESP_LOGE(TAG, "lvgl lock failed");

        return;
    }

    screen = lv_screen_active();
    lv_obj_add_flag(screen, LV_OBJ_FLAG_CLICKABLE);
    lv_obj_add_event_cb(screen, on_tap, LV_EVENT_CLICKED, NULL);
    face_init(screen);

    if (lv_timer_create(on_level, MAIN_LEVEL_MS, NULL) == NULL) {
        ESP_LOGE(TAG, "level timer failed");
    }

    lvgl_port_unlock();
}

/* Smart home over our MQTT client when a broker is configured. */
static void start_mqtt(void)
{
    char password[SKWOOP_API_KEY_MAX + 1] = "";
    char client_id[24];
    uint8_t mac[6] = { 0 };
    mqtt_lite_config_t cfg = {
        .url = CONFIG_SKWOOP_MQTT_URL,
        .client_id = client_id,
        .username = CONFIG_SKWOOP_MQTT_USER,
        .password = password,
    };

    if (CONFIG_SKWOOP_MQTT_URL[0] == '\0') {
        return;
    }

    (void)esp_read_mac(mac, ESP_MAC_WIFI_STA);
    snprintf(client_id, sizeof client_id, "skwoop-%02x%02x%02x", mac[3], mac[4], mac[5]);

    if (skwoop_config_get_api_key("mqtt", password, sizeof password) != 0) {
        password[0] = '\0';
    }

    if (mqtt_lite_start(&cfg) != ESP_OK) {
        ESP_LOGE(TAG, "mqtt config rejected");
    }
}

static void start_bridge_once(void)
{
    bridge_callbacks_t callbacks = {
        .on_msg = on_bridge_msg,
        .on_audio = on_bridge_audio,
    };

    if (s_bridge_started) {
        return;
    }

    if (bridge_start(&callbacks) != ESP_OK) {
        ESP_LOGE(TAG, "bridge start failed");

        return;
    }

    s_bridge_started = true;
    ESP_LOGI(TAG, "bridge started");
}

#if CONFIG_SKWOOP_STANDALONE
/* Every reply mood shows its own face for MAIN_HAPPY_MS, then the face goes
 * back to s_mood (speaking). */
static void on_reply_mood(mood_name_t mood)
{
    face_mood_t face;

    ESP_LOGI(TAG, "reply mood %s", mood_name(mood));

    if (!face_mood_for_reply(mood_name(mood), &face)) {
        return;
    }

    if (lvgl_port_lock(0)) {
        start_flash(face);
        lvgl_port_unlock();
    }
}
#endif

static void on_wifi(wifi_state_t state)
{
    face_mood_t mood;

    switch (state) {
    case WIFI_CONNECTING:
        mood = FACE_THINKING;
        break;
    case WIFI_GOT_IP:
        mood = FACE_HAPPY;
        break;
    case WIFI_AP_MODE:
        mood = FACE_SLEEPY;
        break;
    case WIFI_FAILED:
        return;
    default:
        return;
    }

    set_mood(mood);

    if (state != WIFI_GOT_IP) {
        return;
    }

    alarm_clock_sync_time();
    start_mqtt();

#if CONFIG_SKWOOP_STANDALONE
    if (!s_standalone && skwoop_config_get_standalone()) {
        s_standalone = standalone_start(set_mood, on_reply_mood);
    }

    if (s_standalone) {
        return;
    }
#endif

    start_bridge_once();
}

void app_main(void)
{
    const alarm_clock_callbacks_t clock_callbacks = {
        .on_fire = on_timer_fire,
        .on_countdown = face_set_countdown,
    };

    ESP_ERROR_CHECK(storage_init());
    ESP_ERROR_CHECK(board_init(on_button_press, on_button_long_press));
    show_face();
    ESP_ERROR_CHECK(alarm_clock_start(&clock_callbacks));
    ESP_ERROR_CHECK(wake_start(on_wake, on_command));
    ESP_ERROR_CHECK(voice_start());
    ESP_ERROR_CHECK(wifi_start(on_wifi));
}
