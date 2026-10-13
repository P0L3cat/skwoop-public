#include "wake.h"

#include <stdatomic.h>
#include <stdlib.h>
#include <string.h>

#include "freertos/FreeRTOS.h"
#include "freertos/stream_buffer.h"
#include "freertos/task.h"

#include "esp_afe_sr_models.h"
#include "esp_err.h"
#include "esp_log.h"
#include "esp_mn_iface.h"
#include "esp_mn_models.h"
#include "esp_mn_speech_commands.h"
#include "esp_wn_models.h"

#include "cmds.h"

#include "wake_pick.h"

#define WAKE_CLEAN_BYTES 8192
#define WAKE_FALLBACK_KEYWORD "hiesp"
#define WAKE_CMD_WINDOW_MS 5000

static const char *TAG = "wake";

static esp_afe_sr_iface_t *s_afe;
static esp_afe_sr_data_t *s_afe_data;
static void (*s_on_wake)(const char *word);
static char s_word[48] = "Hi ESP";
static int16_t *s_pending;
static size_t s_pending_frames;
static int s_chunk;
static StreamBufferHandle_t s_clean;
static atomic_bool s_speech;

static esp_mn_iface_t *s_mn;

static model_iface_data_t *s_mn_data;

static void (*s_on_command)(int id);

static bool s_cmd_listen;

/* After a wake word, MultiNet listens for one offline command. */
static void detect_command(int16_t *chunk)
{
    esp_mn_state_t state = s_mn->detect(s_mn_data, chunk);

    if (state == ESP_MN_STATE_DETECTING) {
        return;
    }

    s_cmd_listen = false;

    if (state != ESP_MN_STATE_DETECTED) {
        return;
    }

    esp_mn_results_t *r = s_mn->get_results(s_mn_data);

    if (r != NULL && r->num > 0 && s_on_command != NULL) {
        s_on_command(r->command_id[0]);
    }
}

static void wake_fetch_task(void *arg)
{
    (void)arg;

    for (;;) {
        afe_fetch_result_t *res = s_afe->fetch(s_afe_data);
        size_t nbytes;

        if (res == NULL || res->ret_value == ESP_FAIL) {
            continue;
        }

        atomic_store(&s_speech, res->vad_state == VAD_SPEECH);

        if (res->wakeup_state == WAKENET_DETECTED) {
            if (s_mn_data != NULL) {
                s_mn->clean(s_mn_data);
                s_cmd_listen = true;
            }

            if (s_on_wake != NULL) {
                s_on_wake(s_word);
            }
        } else if (s_cmd_listen && res->data != NULL) {
            detect_command(res->data);
        }

        nbytes = (size_t)res->data_size;

        if (res->data == NULL || nbytes < sizeof(int16_t) || (nbytes % sizeof(int16_t)) != 0
            || xStreamBufferSpacesAvailable(s_clean) < nbytes) {
            continue;
        }

        (void)xStreamBufferSend(s_clean, res->data, nbytes, 0);
    }
}

/* MultiNet English with the cmds table. Missing model or a chunk-size
 * mismatch with the AFE leaves offline commands off; wake still works. */
static void load_commands(srmodel_list_t *models, int afe_chunk)
{
    char *name = esp_srmodel_filter(models, ESP_MN_PREFIX, ESP_MN_ENGLISH);
    const cmd_entry_t *table;
    esp_mn_error_t *err;
    size_t n;

    if (name == NULL) {
        ESP_LOGW(TAG, "no MultiNet in model partition: offline commands off");

        return;
    }

    s_mn = esp_mn_handle_from_name(name);
    s_mn_data = s_mn != NULL ? s_mn->create(name, WAKE_CMD_WINDOW_MS) : NULL;

    if (s_mn_data == NULL || s_mn->get_samp_chunksize(s_mn_data) != afe_chunk) {
        ESP_LOGE(TAG, "MultiNet %s unusable (chunk %d vs afe %d)", name,
                 s_mn_data != NULL ? s_mn->get_samp_chunksize(s_mn_data) : -1, afe_chunk);

        if (s_mn_data != NULL) {
            s_mn->destroy(s_mn_data);
            s_mn_data = NULL;
        }

        return;
    }

    esp_mn_commands_alloc(s_mn, s_mn_data);
    table = cmds_table(&n);

    for (size_t i = 0; i < n; i++) {
        if (esp_mn_commands_add(table[i].id, table[i].phrase) != ESP_OK) {
            ESP_LOGE(TAG, "command rejected: %s", table[i].phrase);
        }
    }

    err = esp_mn_commands_update();

    if (err != NULL && err->num > 0) {
        ESP_LOGE(TAG, "%d commands failed to tokenize", err->num);
    }
}

esp_err_t wake_start(void (*on_wake)(const char *word), void (*on_command)(int id))
{
    srmodel_list_t *models;
    char *model_name;
    char *words;
    afe_config_t *cfg;
    int channels;

    int pick;

    s_on_wake = on_wake;
    s_on_command = on_command;
    models = esp_srmodel_init("model");

    if (models == NULL) {
        ESP_LOGE(TAG, "model partition empty");

        return ESP_FAIL;
    }

    /* SAFETY: model_name holds num NUL-terminated names owned by models; the
     * picker only reads them. */
    pick = wake_pick((const char *const *)models->model_name, (size_t)models->num, ESP_WN_PREFIX,
                     CONFIG_SKWOOP_WAKE_KEYWORD, WAKE_FALLBACK_KEYWORD);

    if (pick < 0) {
        ESP_LOGE(TAG, "no wake model (%s or %s)", CONFIG_SKWOOP_WAKE_KEYWORD, WAKE_FALLBACK_KEYWORD);

        return ESP_FAIL;
    }

    model_name = models->model_name[pick];

    words = esp_srmodel_get_wake_words(models, model_name);

    if (words != NULL) {
        size_t n = 0;

        while (words[n] != '\0' && words[n] != ';' && n + 1 < sizeof s_word) {
            s_word[n] = (words[n] == ',') ? ' ' : words[n];
            n++;
        }

        if (n > 0) {
            s_word[n] = '\0';
        }

        free(words);
    }

    cfg = afe_config_init("MR", models, AFE_TYPE_SR, AFE_MODE_HIGH_PERF);

    if (cfg == NULL) {
        return ESP_FAIL;
    }

    cfg->aec_init = true;
    cfg->vad_init = true;
    cfg->wakenet_init = true;
    cfg->wakenet_model_name = model_name;
    cfg->memory_alloc_mode = AFE_MEMORY_ALLOC_MORE_PSRAM;
    s_afe = esp_afe_handle_from_config(cfg);

    if (s_afe != NULL) {
        s_afe_data = s_afe->create_from_config(cfg);
    }

    afe_config_free(cfg);

    if (s_afe_data == NULL) {
        return ESP_FAIL;
    }

    s_chunk = s_afe->get_feed_chunksize(s_afe_data);
    channels = s_afe->get_feed_channel_num(s_afe_data);

    if (s_chunk <= 0 || channels != 2) {
        ESP_LOGE(TAG, "afe chunk %d ch %d", s_chunk, channels);

        return ESP_FAIL;
    }

    load_commands(models, s_afe->get_fetch_chunksize(s_afe_data));
    s_pending = malloc((size_t)s_chunk * 2u * sizeof(int16_t));
    s_clean = xStreamBufferCreate(WAKE_CLEAN_BYTES, 1);

    if (s_pending == NULL || s_clean == NULL
        || xTaskCreate(wake_fetch_task, "wake_fetch", 8192, NULL, 5, NULL) != pdPASS) {
        return ESP_ERR_NO_MEM;
    }

    ESP_LOGI(TAG, "wn %s word %s chunk %d", model_name, s_word, s_chunk);

    return ESP_OK;
}

void wake_feed(const int16_t *interleaved, size_t frames)
{
    size_t off = 0;

    if (s_afe_data == NULL || s_pending == NULL || interleaved == NULL || frames == 0) {
        return;
    }

    while (off < frames) {
        size_t room = (size_t)s_chunk - s_pending_frames;
        size_t take = frames - off;

        if (take > room) {
            take = room;
        }

        memcpy(s_pending + s_pending_frames * 2u, interleaved + off * 2u, take * 2u * sizeof(int16_t));
        s_pending_frames += take;
        off += take;

        if (s_pending_frames == (size_t)s_chunk) {
            s_afe->feed(s_afe_data, s_pending);
            s_pending_frames = 0;
        }
    }
}

bool wake_vad_speech(void)
{
    return atomic_load(&s_speech);
}

size_t wake_fetch_clean(int16_t *out, size_t n)
{
    size_t bytes;

    if (out == NULL || n == 0 || s_clean == NULL) {
        return 0;
    }

    bytes = xStreamBufferReceive(s_clean, out, n * sizeof(int16_t), 0);

    return bytes / sizeof(int16_t);
}
