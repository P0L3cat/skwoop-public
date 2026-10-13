#include "standalone.h"

#include "alarm_clock.h"
#include "clipcache.h"
#include "cloud.h"
#include "endpoint.h"
#include "mood_builtin.h"
#include "skwoop_clips.h"
#include "skwoop_config.h"
#include "skwoop_harness.h"
#include "skwoop_net.h"
#include "voice.h"
#include "wake.h"
#include "wav.h"

#include <errno.h>
#include <stdatomic.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>

#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/task.h"

#define SA_MEMORY_DIR "/storage/memory"
#define SA_CLIPS_DIR "/storage/clips"
#define SA_INDEX_PATH SA_CLIPS_DIR "/clips.idx"
#define SA_INDEX_MAX (256 * 1024)
#define SA_CACHE_DIR "/storage/cache"
#define SA_WAV_HEAD 256
#define SA_PLAY_RATE 24000
#define SA_PLAY_CHUNK 1440
#define SA_RATE 16000
#define SA_FRAME_MS 60
#define SA_RECORD_MS 12000
#define SA_RECORD_SAMPLES (SA_RATE / 1000 * SA_RECORD_MS)
#define SA_STT_TEXT_MAX SKWOOP_HARNESS_UTTER_MAX
#define SA_SYSTEM_MAX (SKWOOP_HARNESS_PREFILL_MAX + 256)
#define SA_SPEAK_DEPTH 4
#define SA_NOTIFY_RECORDED 1u
#define SA_NOTIFY_TEXT 2u
#define SA_NOTIFY_NO_SPEECH 3u

static const char *TAG = "standalone";

static const char *SA_PERSONA =
    "You are Skwoop, a small desk robot. Reply in one to three short spoken sentences. "
    "No lists, no markdown.";

static const endpoint_limits_t SA_LIMITS = {
    .start_timeout_ms = 5000,
    .trailing_silence_ms = 700,
    .max_ms = SA_RECORD_MS,
};

/* One sentence for the speaker task; an empty sentence ends the turn. */
typedef struct {
    char text[CLOUD_SENTENCE_MAX];
} sa_sentence_t;

typedef struct {
    int64_t start_us;
    int64_t stt_us;
    int64_t first_token_us;
    int64_t first_sentence_us;
    int64_t first_audio_us;
} sa_timing_t;

static skwoop_harness_t *s_harness;

static skwoop_clip_index *s_clips;

static cloud_t s_cloud;

static clipcache_t *s_cache;

static standalone_mood_fn s_on_mood;

static standalone_reply_mood_fn s_on_reply_mood;

static mood_model_t s_mood_model;

static mood_scratch_t *s_mood_scratch;

static TaskHandle_t s_turn_task;

static QueueHandle_t s_speak;

static int16_t *s_record;

static size_t s_record_len;

static endpoint_t s_endpoint;

static atomic_bool s_busy;

static atomic_bool s_cancel;

static char s_text[SA_STT_TEXT_MAX];

static sa_timing_t s_timing;

static cloud_splitter_t s_splitter;

/* Set when the harness handed the turn to the LLM (which speaks as it
 * streams); local answers (remember, timer, clock) are spoken after. */
static bool s_model_spoke;

static int64_t since_start_ms(int64_t at_us)
{
    return at_us > 0 ? (at_us - s_timing.start_us) / 1000 : -1;
}

static void ensure_dir(const char *path)
{
    if (mkdir(path, 0755) != 0 && errno != EEXIST) {
        ESP_LOGW(TAG, "mkdir %s: %d", path, errno);
    }
}

static int fetch_index(void)
{
    char base[SKWOOP_BASE_URL_MAX + 1];
    char url[SKWOOP_BASE_URL_MAX + 16];
    size_t got = 0;

    if (skwoop_config_get_clip_base(base, sizeof base) != 0 || base[0] == '\0') {
        return -ENOENT;
    }

    if (snprintf(url, sizeof url, "%s/clips.idx", base) >= (int)sizeof url) {
        return -ENAMETOOLONG;
    }

    char *buf = heap_caps_malloc(SA_INDEX_MAX, MALLOC_CAP_SPIRAM);

    if (buf == NULL) {
        return -ENOMEM;
    }

    int rc = skwoop_http_get(url, buf, SA_INDEX_MAX, &got);
    FILE *f = rc == 200 ? fopen(SA_INDEX_PATH, "wb") : NULL;

    if (f != NULL) {
        rc = fwrite(buf, 1, got, f) == got ? 0 : -EIO;
        fclose(f);
    } else {
        rc = rc == 200 ? -EIO : -ENOENT;
    }

    heap_caps_free(buf);

    return rc;
}

static void load_clips(void)
{
    struct stat st;

    if (stat(SA_INDEX_PATH, &st) != 0 && fetch_index() != 0) {
        ESP_LOGW(TAG, "no clip index");

        return;
    }

    if (skwoop_clips_load_file(SA_INDEX_PATH, &s_clips) != 0) {
        ESP_LOGW(TAG, "clip index load failed");
        s_clips = NULL;
    }
}

/* Uplink task: append AEC audio and end-point on the AFE VAD. */
static void on_frame(const int16_t *pcm, size_t samples)
{
    size_t room = SA_RECORD_SAMPLES - s_record_len;
    size_t take = samples < room ? samples : room;
    endpoint_state_t state;

    if (atomic_load(&s_cancel)) {
        voice_set_listening(false);
        voice_set_capture(NULL);
        xTaskNotify(s_turn_task, SA_NOTIFY_NO_SPEECH, eSetValueWithOverwrite);

        return;
    }

    memcpy(s_record + s_record_len, pcm, take * sizeof(int16_t));
    s_record_len += take;
    state = endpoint_step(&s_endpoint, wake_vad_speech(), SA_FRAME_MS);

    if (state != ENDPOINT_DONE && state != ENDPOINT_NO_SPEECH && take == samples) {
        return;
    }

    voice_set_listening(false);
    voice_set_capture(NULL);
    xTaskNotify(s_turn_task, state == ENDPOINT_NO_SPEECH ? SA_NOTIFY_NO_SPEECH : SA_NOTIFY_RECORDED, eSetValueWithOverwrite);
}

static void speak(const char *sentence)
{
    sa_sentence_t item;

    strlcpy(item.text, sentence, sizeof item.text);
    (void)xQueueSend(s_speak, &item, portMAX_DELAY);
}

typedef struct {
    skwoop_sentence_list_t *out;
} sa_reply_t;

static void on_sentence(const char *sentence, void *ctx)
{
    /* SAFETY: cloud_model passes its sa_reply_t through the splitter. */
    sa_reply_t *reply = (sa_reply_t *)ctx;

    if (s_timing.first_sentence_us == 0) {
        s_timing.first_sentence_us = esp_timer_get_time();
    }

    if (reply->out->count < SKWOOP_HARNESS_SENT_MAX) {
        strlcpy(reply->out->text[reply->out->count++], sentence, SKWOOP_HARNESS_SENT_LEN);
    }

    speak(sentence);
}

static void on_token(const char *token, size_t len, void *ctx)
{
    if (s_timing.first_token_us == 0) {
        s_timing.first_token_us = esp_timer_get_time();
    }

    cloud_split_feed(&s_splitter, token, len, on_sentence, ctx);
}

/* skwoop_model_fn: the harness hands over the routed utterance and the
 * recall block; sentences are spoken as soon as they complete. */
static int cloud_model(const char *utterance, skwoop_route_t route, const char *prefill,
                       skwoop_sentence_list_t *out, void *userdata)
{
    char *system = malloc(SA_SYSTEM_MAX);
    sa_reply_t reply = { .out = out };
    int rc;

    (void)route;
    (void)userdata;

    if (system == NULL) {
        return -ENOMEM;
    }

    snprintf(system, SA_SYSTEM_MAX, "%s%s%s", SA_PERSONA, prefill[0] != '\0' ? "\nKnown facts:\n" : "", prefill);
    out->count = 0;
    s_model_spoke = true;
    cloud_split_init(&s_splitter);
    rc = cloud_chat(&s_cloud, system, utterance, on_token, &reply);
    cloud_split_flush(&s_splitter, on_sentence, &reply);
    free(system);

    return rc == 0 ? 0 : -1;
}

static void on_pcm(const int16_t *pcm, size_t samples, void *ctx)
{
    (void)ctx;

    if (s_timing.first_audio_us == 0) {
        s_timing.first_audio_us = esp_timer_get_time();
        s_on_mood(FACE_SPEAKING);
    }

    voice_play_pcm(pcm, samples);
}

/* Play a cached 24 kHz mono s16 WAV from flash. */
static bool play_wav(const char *path)
{
    static int16_t pcm[SA_PLAY_CHUNK];
    uint8_t head[SA_WAV_HEAD];
    wav_info_t wav;
    FILE *f = fopen(path, "rb");
    size_t n = f != NULL ? fread(head, 1, sizeof head, f) : 0;
    uint32_t left;

    if (f == NULL || wav_parse(head, n, &wav) != 0 || wav.rate != SA_PLAY_RATE || wav.channels != 1 || wav.bits != 16
        || fseek(f, (long)wav.data_offset, SEEK_SET) != 0) {
        ESP_LOGW(TAG, "clip %s: not 24 kHz mono s16 WAV", path);

        if (f != NULL) {
            fclose(f);
        }

        return false;
    }

    left = wav.data_bytes / 2u;

    while (left > 0 && (n = fread(pcm, sizeof(int16_t), left < SA_PLAY_CHUNK ? left : SA_PLAY_CHUNK, f)) > 0) {
        on_pcm(pcm, n, NULL);
        left -= (uint32_t)n;
    }

    fclose(f);

    return true;
}

/* A sentence that matches a clip plays from the cache; a miss is fetched from
 * the clip host (bridge host or dock) over Wi-Fi and stored first. false: use TTS. */
static bool speak_clip(const char *sentence)
{
    skwoop_clip clip;
    char path[CLIPCACHE_PATH_MAX];
    int rc;

    if (s_clips == NULL || s_cache == NULL || skwoop_clips_resolve(s_clips, sentence, &clip) != 0) {
        return false;
    }

    rc = clipcache_lookup(s_cache, clip.id, path, sizeof path);

    if (rc == -ENOENT) {
        rc = clipcache_temp_path(s_cache, clip.id, path, sizeof path);
        rc = rc == 0 ? skwoop_clips_fetch(clip.id, path) : rc;
        rc = rc == 0 ? clipcache_commit(s_cache, clip.id) : rc;
        rc = rc == 0 ? clipcache_lookup(s_cache, clip.id, path, sizeof path) : rc;
    }

    if (rc != 0) {
        ESP_LOGW(TAG, "clip %s: %d", clip.id, rc);

        return false;
    }

    return play_wav(path);
}

/* On-device mood of the sentence about to be spoken (skwoop-mood v1.1). */
static void reply_mood(const char *sentence)
{
    mood_pad_t pad;

    if (s_mood_scratch == NULL || mood_predict(&s_mood_model, sentence, s_mood_scratch, &pad) != 0) {
        return;
    }

    s_on_reply_mood(mood_nearest(&s_mood_model, pad));
}

static void speaker_task(void *arg)
{
    static sa_sentence_t item;

    (void)arg;

    for (;;) {
        (void)xQueueReceive(s_speak, &item, portMAX_DELAY);

        if (item.text[0] == '\0') {
            ESP_LOGI(TAG, "turn ms: stt %lld first-token %lld first-sentence %lld first-audio %lld end %lld",
                     since_start_ms(s_timing.stt_us), since_start_ms(s_timing.first_token_us),
                     since_start_ms(s_timing.first_sentence_us), since_start_ms(s_timing.first_audio_us),
                     since_start_ms(esp_timer_get_time()));
            s_on_mood(FACE_IDLE);
            atomic_store(&s_busy, false);
            continue;
        }

        if (atomic_load(&s_cancel)) {
            continue;
        }

        reply_mood(item.text);

        if (speak_clip(item.text)) {
            continue;
        }

        int rc = cloud_tts(&s_cloud, item.text, on_pcm, NULL);

        if (rc != 0) {
            ESP_LOGW(TAG, "tts %d", rc);
        }
    }
}

static void run_text_turn(const char *utterance)
{
    static skwoop_sentence_list_t reply;
    int rc;

    s_on_mood(FACE_THINKING);
    s_model_spoke = false;
    rc = skwoop_harness_turn(s_harness, utterance, cloud_model, NULL, &reply);

    if (rc != 0) {
        ESP_LOGW(TAG, "turn %d %s", rc, skwoop_harness_strerror(rc));
        speak("Sorry, I could not reach the cloud.");
    } else if (!s_model_spoke) {
        for (int i = 0; i < reply.count; i++) {
            speak(reply.text[i]);
        }
    }

    speak("");
}

static void turn_task(void *arg)
{
    (void)arg;

    for (;;) {
        uint32_t what = ulTaskNotifyTake(pdTRUE, portMAX_DELAY);

        if (what == SA_NOTIFY_TEXT) {
            run_text_turn(s_text);
            continue;
        }

        if (what != SA_NOTIFY_RECORDED) {
            s_on_mood(FACE_IDLE);
            atomic_store(&s_busy, false);
            continue;
        }

        s_on_mood(FACE_THINKING);
        int rc = cloud_stt(&s_cloud, s_record, s_record_len, s_text, sizeof s_text);

        s_timing.stt_us = esp_timer_get_time();

        if (rc != 0 || s_text[0] == '\0' || atomic_load(&s_cancel)) {
            ESP_LOGW(TAG, "stt %d", rc);
            s_on_mood(FACE_IDLE);
            atomic_store(&s_busy, false);
            continue;
        }

        run_text_turn(s_text);
    }
}

/* Harness timer tool -> a real local timer. */
static int harness_timer(int seconds, void *ctx)
{
    (void)ctx;

    return alarm_clock_add_in(TIMER_KIND_TIMER, (uint32_t)seconds, NULL) > 0 ? 0 : -1;
}

bool standalone_start(standalone_mood_fn on_mood, standalone_reply_mood_fn on_reply_mood)
{
    int rc;

    s_on_mood = on_mood;
    s_on_reply_mood = on_reply_mood;
    ensure_dir(SA_MEMORY_DIR);
    ensure_dir(SA_CLIPS_DIR);
    s_harness = skwoop_harness_create(SA_MEMORY_DIR);
    rc = cloud_load(&s_cloud);

    if (s_harness == NULL || rc != 0) {
        ESP_LOGE(TAG, "standalone off: memory %s, cloud keys %d", s_harness != NULL ? "ok" : "missing", rc);

        return false;
    }

    skwoop_harness_on_timer(s_harness, harness_timer, NULL);
    s_mood_scratch = heap_caps_malloc(sizeof *s_mood_scratch, MALLOC_CAP_SPIRAM);

    if (s_mood_scratch != NULL && mood_builtin(&s_mood_model) != 0) {
        ESP_LOGE(TAG, "mood table rejected");
        heap_caps_free(s_mood_scratch);
        s_mood_scratch = NULL;
    }

    s_record = heap_caps_malloc(SA_RECORD_SAMPLES * sizeof(int16_t), MALLOC_CAP_SPIRAM);
    s_speak = xQueueCreate(SA_SPEAK_DEPTH, sizeof(sa_sentence_t));

    if (s_record == NULL || s_speak == NULL
        || xTaskCreate(turn_task, "sa_turn", 8192, NULL, 4, &s_turn_task) != pdPASS
        || xTaskCreate(speaker_task, "sa_speak", 8192, NULL, 5, NULL) != pdPASS) {
        ESP_LOGE(TAG, "standalone tasks: no memory");

        return false;
    }

    s_cache = heap_caps_malloc(sizeof *s_cache, MALLOC_CAP_SPIRAM);

    if (s_cache == NULL || clipcache_open(s_cache, SA_CACHE_DIR, CONFIG_SKWOOP_CLIP_CACHE_KB * 1024u) != 0) {
        ESP_LOGW(TAG, "clip cache off");
        heap_caps_free(s_cache);
        s_cache = NULL;
    }

    load_clips();

    return true;
}

void standalone_listen(void)
{
    bool idle = false;

    if (s_turn_task == NULL || !atomic_compare_exchange_strong(&s_busy, &idle, true)) {
        return;
    }

    memset(&s_timing, 0, sizeof s_timing);
    s_timing.start_us = esp_timer_get_time();
    atomic_store(&s_cancel, false);
    s_record_len = 0;
    endpoint_init(&s_endpoint, &SA_LIMITS);
    s_on_mood(FACE_LISTENING);
    voice_set_capture(on_frame);
    voice_set_listening(true);
}

void standalone_on_text(const char *utterance)
{
    bool idle = false;

    if (s_turn_task == NULL || utterance == NULL || utterance[0] == '\0'
        || !atomic_compare_exchange_strong(&s_busy, &idle, true)) {
        return;
    }

    memset(&s_timing, 0, sizeof s_timing);
    s_timing.start_us = esp_timer_get_time();
    atomic_store(&s_cancel, false);
    strlcpy(s_text, utterance, sizeof s_text);
    xTaskNotify(s_turn_task, SA_NOTIFY_TEXT, eSetValueWithOverwrite);
}

void standalone_cancel(void)
{
    if (atomic_load(&s_busy)) {
        atomic_store(&s_cancel, true);
    }
}
