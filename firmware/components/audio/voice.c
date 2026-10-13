#include "voice.h"

#include <stdatomic.h>
#include <stddef.h>
#include <stdint.h>
#include <string.h>

#include "freertos/FreeRTOS.h"
#include "freertos/message_buffer.h"
#include "freertos/task.h"

#include "board.h"
#include "bridge.h"
#include "opus_io.h"
#include "resample.h"
#include "wake.h"

static atomic_bool s_listening;

static MessageBufferHandle_t s_down;

static _Atomic uint8_t s_level;

static _Atomic(voice_frame_fn) s_capture;

static uint32_t voice_isqrt(uint32_t x)
{
    uint32_t lo = 0;
    uint32_t hi = 65536;

    while (lo + 1u < hi) {
        uint32_t mid = (lo + hi) / 2u;
        if (mid * mid <= x) {
            lo = mid;
        } else {
            hi = mid;
        }
    }
    return lo;
}

static void voice_update_level(const int16_t *pcm, size_t samples)
{
    uint64_t sum_sq = 0;

    for (size_t i = 0; i < samples; i++) {
        int32_t sample = pcm[i];
        sum_sq += (uint64_t)((int64_t)sample * (int64_t)sample);
    }

    uint32_t mean = (uint32_t)(sum_sq / samples);
    uint32_t level = voice_isqrt(mean) / 328u;

    if (level > 100u) {
        level = 100u;
    }

    atomic_store(&s_level, (uint8_t)level);
}

static void voice_uplink_task(void *arg)
{
    static int16_t buf[2880];
    static int16_t mono24[1440];
    static int16_t mic16[OPUS_UP_FRAME_SAMPLES];
    static int16_t ref16[OPUS_UP_FRAME_SAMPLES];
    static int16_t interleaved[OPUS_UP_FRAME_SAMPLES * 2];
    static int16_t pcm[OPUS_UP_FRAME_SAMPLES];
    static resample_24to16_t mic_rs;
    static resample_24to16_t ref_rs;
    static size_t clean_n;

    (void)arg;
    resample_24to16_init(&mic_rs);
    resample_24to16_init(&ref_rs);

    for (;;) {
        size_t nmic;
        size_t nref;
        size_t frames;
        esp_err_t err = board_audio_read(buf, 2880);

        if (err != ESP_OK) {
            vTaskDelay(pdMS_TO_TICKS(10));
            continue;
        }

        for (size_t i = 0; i < 1440; i++) {
            mono24[i] = buf[i * 2];
        }

        nmic = resample_24to16(&mic_rs, mono24, 1440, mic16, OPUS_UP_FRAME_SAMPLES);

        for (size_t i = 0; i < 1440; i++) {
            mono24[i] = buf[i * 2 + 1];
        }

        nref = resample_24to16(&ref_rs, mono24, 1440, ref16, OPUS_UP_FRAME_SAMPLES);
        frames = nmic < nref ? nmic : nref;

        for (size_t i = 0; i < frames; i++) {
            interleaved[i * 2] = mic16[i];
            interleaved[i * 2 + 1] = ref16[i];
        }

        wake_feed(interleaved, frames);

        if (clean_n < OPUS_UP_FRAME_SAMPLES) {
            clean_n += wake_fetch_clean(pcm + clean_n, OPUS_UP_FRAME_SAMPLES - clean_n);
        }

        if (clean_n < OPUS_UP_FRAME_SAMPLES) {
            continue;
        }

        voice_frame_fn capture = atomic_load(&s_capture);

        if (atomic_load(&s_listening) && capture != NULL) {
            capture(pcm, OPUS_UP_FRAME_SAMPLES);
        } else if (atomic_load(&s_listening)) {
            uint8_t pkt[512];
            int n = opus_up_encode(pcm, pkt, sizeof pkt);

            if (n > 0) {
                (void)bridge_send_audio(pkt, (size_t)n);
            }
        }

        clean_n = 0;
    }
}

static void voice_downlink_task(void *arg)
{
    static uint8_t pkt[512];
    static int16_t pcm[OPUS_DOWN_FRAME_SAMPLES];

    (void)arg;

    for (;;) {
        size_t len = xMessageBufferReceive(s_down, pkt, sizeof(pkt), portMAX_DELAY);
        if (len == 0 || len > sizeof(pkt)) {
            continue;
        }

        int n = opus_down_decode(pkt, len, pcm, OPUS_DOWN_FRAME_SAMPLES);
        if (n <= 0) {
            continue;
        }

        size_t samples = (size_t)n;
        if (samples > OPUS_DOWN_FRAME_SAMPLES) {
            samples = OPUS_DOWN_FRAME_SAMPLES;
        }

        (void)board_audio_write(pcm, samples);
        voice_update_level(pcm, samples);
    }
}

esp_err_t voice_start(void)
{
    if (!opus_up_open() || !opus_down_open()) {
        return ESP_FAIL;
    }

    s_down = xMessageBufferCreate(8192);
    if (s_down == NULL) {
        return ESP_ERR_NO_MEM;
    }

    if (xTaskCreate(voice_uplink_task, "voice_up", 6144, NULL, 5, NULL) != pdPASS) {
        return ESP_ERR_NO_MEM;
    }
    if (xTaskCreate(voice_downlink_task, "voice_dn", 6144, NULL, 5, NULL) != pdPASS) {
        return ESP_ERR_NO_MEM;
    }
    return ESP_OK;
}

void voice_set_listening(bool on)
{
    atomic_store(&s_listening, on);
}

void voice_feed_opus(const uint8_t *pkt, size_t len)
{
    if (pkt == NULL || len == 0 || len > 512 || s_down == NULL) {
        return;
    }
    (void)xMessageBufferSend(s_down, pkt, len, 0);
}

uint8_t voice_level(void)
{
    return atomic_load(&s_level);
}

void voice_set_capture(voice_frame_fn fn)
{
    atomic_store(&s_capture, fn);
}

void voice_play_pcm(const int16_t *pcm, size_t samples)
{
    static int16_t chunk[OPUS_DOWN_FRAME_SAMPLES];

    while (pcm != NULL && samples > 0) {
        size_t n = samples < OPUS_DOWN_FRAME_SAMPLES ? samples : OPUS_DOWN_FRAME_SAMPLES;

        memcpy(chunk, pcm, n * sizeof(int16_t));
        (void)board_audio_write(chunk, n);
        voice_update_level(chunk, n);
        pcm += n;
        samples -= n;
    }
}
