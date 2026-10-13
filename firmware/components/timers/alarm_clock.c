#include "alarm_clock.h"

#include <errno.h>
#include <stdbool.h>
#include <stdlib.h>
#include <sys/time.h>
#include <time.h>

#include "esp_log.h"
#include "esp_netif_sntp.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "nvs.h"

#define AC_NS "timers"
#define AC_KEY_TABLE "table"
#define AC_KEY_EPOCH "epoch"
#define AC_VALID_EPOCH 1735689600 /* 2025-01-01: anything earlier is the boot clock */
#define AC_EPOCH_SAVE_S 60
#define AC_COUNTDOWN_MAX_S 86400
#define AC_SHIFT_MIN_S 60

static const char *TAG = "alarm_clock";

/* Where the wall clock came from this boot. Timers added on the bare boot
 * clock are shifted when SNTP jumps; restored or synced clocks are not. */
typedef enum {
    AC_CLOCK_BOOT,
    AC_CLOCK_RESTORED,
    AC_CLOCK_SNTP,
} ac_clock_t;

static timers_t s_timers;

static SemaphoreHandle_t s_lock;

static alarm_clock_callbacks_t s_cb;

static ac_clock_t s_clock = AC_CLOCK_BOOT;

static int64_t s_tick_wall;

static int64_t s_tick_mono_us;

static int64_t now_s(void)
{
    return (int64_t)time(NULL);
}

static void save_table(void)
{
    uint8_t blob[TIMERS_BLOB_MAX];
    size_t n = timers_serialize(&s_timers, blob, sizeof blob);
    nvs_handle_t h;

    if (n == 0 || nvs_open(AC_NS, NVS_READWRITE, &h) != ESP_OK) {
        ESP_LOGE(TAG, "save failed");

        return;
    }

    if (nvs_set_blob(h, AC_KEY_TABLE, blob, n) != ESP_OK || nvs_commit(h) != ESP_OK) {
        ESP_LOGE(TAG, "save failed");
    }

    nvs_close(h);
}

static void save_epoch(int64_t epoch)
{
    nvs_handle_t h;

    if (nvs_open(AC_NS, NVS_READWRITE, &h) == ESP_OK) {
        (void)nvs_set_i64(h, AC_KEY_EPOCH, epoch);
        (void)nvs_commit(h);
        nvs_close(h);
    }
}

static void load(void)
{
    uint8_t blob[TIMERS_BLOB_MAX];
    size_t n = sizeof blob;
    int64_t epoch = 0;
    nvs_handle_t h;

    timers_init(&s_timers);

    if (nvs_open(AC_NS, NVS_READONLY, &h) != ESP_OK) {
        return;
    }

    if (nvs_get_blob(h, AC_KEY_TABLE, blob, &n) == ESP_OK && timers_parse(&s_timers, blob, n) != 0) {
        ESP_LOGW(TAG, "stored table corrupt, starting empty");
        timers_init(&s_timers);
    }

    if (now_s() < AC_VALID_EPOCH && nvs_get_i64(h, AC_KEY_EPOCH, &epoch) == ESP_OK && epoch >= AC_VALID_EPOCH) {
        struct timeval tv = { .tv_sec = (time_t)epoch, .tv_usec = 0 };

        settimeofday(&tv, NULL);
        s_clock = AC_CLOCK_RESTORED;
    }

    nvs_close(h);
}

static void on_sntp(struct timeval *tv)
{
    int64_t expected = s_tick_wall + (esp_timer_get_time() - s_tick_mono_us) / 1000000;
    int64_t jump = (int64_t)tv->tv_sec - expected;

    xSemaphoreTake(s_lock, portMAX_DELAY);

    if (s_clock == AC_CLOCK_BOOT && llabs(jump) >= AC_SHIFT_MIN_S) {
        for (uint32_t i = 0; i < s_timers.count; i++) {
            if (s_timers.items[i].kind != TIMER_KIND_ALARM) {
                s_timers.items[i].due_s += jump;
            }
        }

        save_table();
    }

    s_clock = AC_CLOCK_SNTP;
    xSemaphoreGive(s_lock);
    save_epoch(tv->tv_sec);
}

static void tick_task(void *arg)
{
    TickType_t wake = xTaskGetTickCount();
    int64_t last_epoch_save = 0;

    (void)arg;

    for (;;) {
        timer_item_t fired;
        const timer_item_t *next;
        char text[16];
        bool show = false;
        bool any_fired = false;
        int64_t now;

        vTaskDelayUntil(&wake, pdMS_TO_TICKS(1000));
        now = now_s();
        xSemaphoreTake(s_lock, portMAX_DELAY);
        s_tick_wall = now;
        s_tick_mono_us = esp_timer_get_time();

        while (timers_pop_due(&s_timers, now, &fired)) {
            any_fired = true;
            xSemaphoreGive(s_lock);
            s_cb.on_fire(&fired);
            xSemaphoreTake(s_lock, portMAX_DELAY);
        }

        if (any_fired) {
            save_table();
        }

        next = timers_next(&s_timers);

        if (next != NULL && next->due_s - now <= AC_COUNTDOWN_MAX_S) {
            timers_format_remaining(next->due_s - now, text, sizeof text);
            show = true;
        }

        xSemaphoreGive(s_lock);
        s_cb.on_countdown(show ? text : NULL);

        if (now >= AC_VALID_EPOCH && now - last_epoch_save >= AC_EPOCH_SAVE_S) {
            save_epoch(now);
            last_epoch_save = now;
        }
    }
}

esp_err_t alarm_clock_start(const alarm_clock_callbacks_t *callbacks)
{
    if (callbacks == NULL || callbacks->on_fire == NULL || callbacks->on_countdown == NULL) {
        return ESP_ERR_INVALID_ARG;
    }

    s_cb = *callbacks;
    setenv("TZ", CONFIG_SKWOOP_TZ, 1);
    tzset();
    s_lock = xSemaphoreCreateMutex();

    if (s_lock == NULL) {
        return ESP_ERR_NO_MEM;
    }

    load();
    s_tick_wall = now_s();
    s_tick_mono_us = esp_timer_get_time();

    return xTaskCreate(tick_task, "alarm_clock", 4096, NULL, 3, NULL) == pdPASS ? ESP_OK : ESP_ERR_NO_MEM;
}

void alarm_clock_sync_time(void)
{
    static bool started;
    esp_sntp_config_t cfg = ESP_NETIF_SNTP_DEFAULT_CONFIG("pool.ntp.org");

    if (started) {
        return;
    }

    cfg.sync_cb = on_sntp;
    started = esp_netif_sntp_init(&cfg) == ESP_OK;
}

static int add(timer_kind_t kind, int64_t due_s, const char *label)
{
    int id;

    xSemaphoreTake(s_lock, portMAX_DELAY);
    id = timers_add(&s_timers, kind, due_s, label);

    if (id > 0) {
        save_table();
    }

    xSemaphoreGive(s_lock);

    return id;
}

int alarm_clock_add_in(timer_kind_t kind, uint32_t seconds, const char *label)
{
    return seconds == 0 ? -EINVAL : add(kind, now_s() + seconds, label);
}

int alarm_clock_add_at(timer_kind_t kind, int hour, int minute, const char *label)
{
    int64_t due;

    if (now_s() < AC_VALID_EPOCH) {
        return -EAGAIN;
    }

    due = timers_next_clock(now_s(), hour, minute);

    return due < 0 ? -EINVAL : add(kind, due, label);
}

int alarm_clock_cancel_kind(timer_kind_t kind)
{
    int n;

    xSemaphoreTake(s_lock, portMAX_DELAY);
    n = timers_cancel_kind(&s_timers, kind);

    if (n > 0) {
        save_table();
    }

    xSemaphoreGive(s_lock);

    return n;
}
