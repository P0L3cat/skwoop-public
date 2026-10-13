#include "mqtt_lite.h"

#include <stdio.h>
#include <string.h>

#include "esp_crt_bundle.h"
#include "esp_log.h"
#include "esp_tls.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/task.h"

#include "mqtt_codec.h"
#include "mqtt_url.h"

#define ML_TOPIC_MAX 96
#define ML_PAYLOAD_MAX 64
#define ML_ID_MAX 32
#define ML_CRED_MAX 128
#define ML_QUEUE_DEPTH 8
#define ML_KEEPALIVE_S 60
#define ML_IO_TIMEOUT_MS 5000
#define ML_BACKOFF_MIN_MS 1000
#define ML_BACKOFF_MAX_MS 60000
#define ML_PACKET_MAX (ML_TOPIC_MAX + ML_PAYLOAD_MAX + 16)
#define ML_CONNECT_MAX (16 + ML_ID_MAX + 2 * ML_CRED_MAX)

static const char *TAG = "mqtt_lite";

typedef struct {
    char topic[ML_TOPIC_MAX];
    char payload[ML_PAYLOAD_MAX];
    bool retain;
} ml_message_t;

static mqtt_url_t s_url;

static char s_client_id[ML_ID_MAX];

static char s_username[ML_CRED_MAX];

static char s_password[ML_CRED_MAX];

static QueueHandle_t s_queue;

static bool write_all(esp_tls_t *tls, const uint8_t *buf, int len)
{
    int done = 0;

    while (done < len) {
        int n = esp_tls_conn_write(tls, buf + done, (size_t)(len - done));

        if (n <= 0) {
            return false;
        }

        done += n;
    }

    return true;
}

static bool read_exact(esp_tls_t *tls, uint8_t *buf, size_t len)
{
    size_t done = 0;

    while (done < len) {
        int n = esp_tls_conn_read(tls, buf + done, len - done);

        if (n <= 0) {
            return false;
        }

        done += (size_t)n;
    }

    return true;
}

/* One whole packet into buf. */
static bool read_packet(esp_tls_t *tls, uint8_t *buf, size_t cap, mqtt_packet_t *out)
{
    size_t have = 0;
    uint8_t type;
    uint8_t flags;
    uint32_t remaining;
    int hl = 0;

    while (hl == 0 && have < 5) {
        if (!read_exact(tls, buf + have, 1)) {
            return false;
        }

        have++;
        hl = mqtt_decode_header(buf, have, &type, &flags, &remaining);
    }

    if (hl <= 0 || (size_t)hl + remaining > cap || !read_exact(tls, buf + hl, remaining)) {
        return false;
    }

    return mqtt_decode_packet(buf, (size_t)hl + remaining, out) == 0;
}

static esp_tls_t *open_session(void)
{
    esp_tls_cfg_t cfg = {
        .timeout_ms = ML_IO_TIMEOUT_MS,
        .is_plain_tcp = !s_url.tls,
        .crt_bundle_attach = s_url.tls ? esp_crt_bundle_attach : NULL,
    };
    uint8_t buf[ML_CONNECT_MAX];
    const mqtt_connect_t connect = {
        .client_id = s_client_id,
        .username = s_username[0] != '\0' ? s_username : NULL,
        .password = s_password[0] != '\0' ? s_password : NULL,
        .keepalive_s = ML_KEEPALIVE_S,
        .clean_session = true,
    };
    esp_tls_t *tls = esp_tls_init();
    mqtt_packet_t ack;
    int n;

    if (tls == NULL) {
        return NULL;
    }

    if (esp_tls_conn_new_sync(s_url.host, (int)strlen(s_url.host), s_url.port, &cfg, tls) != 1) {
        esp_tls_conn_destroy(tls);

        return NULL;
    }

    n = mqtt_encode_connect(buf, sizeof buf, &connect);

    if (n < 0 || !write_all(tls, buf, n) || !read_packet(tls, buf, sizeof buf, &ack) || ack.type != MQTT_CONNACK
        || ack.return_code != 0) {
        ESP_LOGE(TAG, "broker refused (%d)", n < 0 ? -1 : (int)ack.return_code);
        esp_tls_conn_destroy(tls);

        return NULL;
    }

    return tls;
}

/* Publish queued messages; ping at half the keepalive. Returns on I/O error. */
static void run_session(esp_tls_t *tls)
{
    uint8_t buf[ML_PACKET_MAX];
    ml_message_t msg;

    for (;;) {
        int n;

        if (xQueueReceive(s_queue, &msg, pdMS_TO_TICKS(ML_KEEPALIVE_S * 1000 / 2)) == pdTRUE) {
            n = mqtt_encode_publish(buf, sizeof buf, msg.topic, (const uint8_t *)msg.payload, strlen(msg.payload), 0,
                                    msg.retain, 0);

            if (n > 0 && !write_all(tls, buf, n)) {
                return;
            }

            continue;
        }

        mqtt_packet_t pong;

        n = mqtt_encode_pingreq(buf, sizeof buf);

        if (!write_all(tls, buf, n) || !read_packet(tls, buf, sizeof buf, &pong) || pong.type != MQTT_PINGRESP) {
            return;
        }
    }
}

static void mqtt_task(void *arg)
{
    uint32_t backoff = ML_BACKOFF_MIN_MS;

    (void)arg;

    for (;;) {
        esp_tls_t *tls = open_session();

        if (tls != NULL) {
            ESP_LOGI(TAG, "connected %s:%u", s_url.host, s_url.port);
            backoff = ML_BACKOFF_MIN_MS;
            run_session(tls);
            esp_tls_conn_destroy(tls);
            ESP_LOGW(TAG, "session ended");
        }

        vTaskDelay(pdMS_TO_TICKS(backoff));
        backoff = backoff * 2 > ML_BACKOFF_MAX_MS ? ML_BACKOFF_MAX_MS : backoff * 2;
    }
}

esp_err_t mqtt_lite_start(const mqtt_lite_config_t *cfg)
{
    if (s_queue != NULL) {
        return ESP_OK;
    }

    if (cfg == NULL || mqtt_url_parse(cfg->url, &s_url) != 0 || cfg->client_id == NULL
        || strlen(cfg->client_id) >= sizeof s_client_id
        || (cfg->username != NULL && strlen(cfg->username) >= sizeof s_username)
        || (cfg->password != NULL && strlen(cfg->password) >= sizeof s_password)) {
        return ESP_ERR_INVALID_ARG;
    }

    strcpy(s_client_id, cfg->client_id);
    strcpy(s_username, cfg->username != NULL ? cfg->username : "");
    strcpy(s_password, cfg->password != NULL ? cfg->password : "");
    s_queue = xQueueCreate(ML_QUEUE_DEPTH, sizeof(ml_message_t));

    if (s_queue == NULL || xTaskCreate(mqtt_task, "mqtt_lite", 6144, NULL, 3, NULL) != pdPASS) {
        return ESP_ERR_NO_MEM;
    }

    return ESP_OK;
}

bool mqtt_lite_publish(const char *topic, const char *payload, bool retain)
{
    ml_message_t msg = { .retain = retain };

    if (s_queue == NULL || topic == NULL || payload == NULL || strlen(topic) >= sizeof msg.topic
        || strlen(payload) >= sizeof msg.payload) {
        return false;
    }

    strcpy(msg.topic, topic);
    strcpy(msg.payload, payload);

    return xQueueSend(s_queue, &msg, 0) == pdTRUE;
}
