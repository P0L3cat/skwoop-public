#include "wifi.h"

#include <stdio.h>
#include <string.h>

#include "esp_event.h"
#include "esp_http_server.h"
#include "esp_log.h"
#include "esp_netif.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "esp_wifi.h"
#include "sdkconfig.h"
#include "prov_form.h"
#include "skwoop_config.h"

/* First join is WIFI_EVENT_STA_START. Each disconnect may join again, up to 5 times. */
#define WIFI_STA_RETRIES 5

/* Provisioning POST body cap. Matches prov_form_parse. */
#define PROV_BODY_MAX 256

static const char *TAG = "wifi";

static wifi_state_cb s_on_state;
static int s_retries;
static int s_fail_sent;
static int s_started;
static int s_ap;

static void wipe_chars(volatile char *p, size_t n)
{
    while (n > 0) {
        *p = 0;
        p += 1;
        n -= 1;
    }
}

static void notify(wifi_state_t state)
{
    if (s_on_state != NULL) {
        s_on_state(state);
    }
}

static void ap_fail(esp_err_t err)
{
    ESP_LOGE(TAG, "ap setup failed: %s", esp_err_to_name(err));

    if (!s_fail_sent) {
        s_fail_sent = 1;
        notify(WIFI_FAILED);
    }
}

/* ESP-IDF timer callback. arg is the timer's void* boundary. */
static void on_reboot(void *arg)
{
    (void)arg;
    esp_restart();
}

static esp_err_t schedule_reboot(void)
{
    const esp_timer_create_args_t args = {
        .callback = on_reboot,
        .name = "prov_reboot",
    };
    esp_timer_handle_t timer = NULL;
    esp_err_t err = esp_timer_create(&args, &timer);

    if (err != ESP_OK) {
        return err;
    }

    err = esp_timer_start_once(timer, 1000000);

    if (err != ESP_OK) {
        esp_timer_delete(timer);
    }

    return err;
}

static esp_err_t on_prov_get(httpd_req_t *req)
{
    static const char form[] =
        "<form method=\"POST\" action=\"/save\">"
        "<input name=\"ssid\">"
        "<input name=\"pass\" type=\"password\">"
        "</form>";
    esp_err_t err = httpd_resp_set_type(req, "text/html");

    if (err != ESP_OK) {
        return err;
    }

    return httpd_resp_send(req, form, sizeof form - 1);
}

static esp_err_t on_prov_save(httpd_req_t *req)
{
    char buf[PROV_BODY_MAX + 1];
    prov_creds_t creds;
    size_t got = 0;
    int timeouts = 0;

    /* httpd_err_code_t has no 413. 400 is the client error we can send. */
    if (req->content_len > PROV_BODY_MAX) {
        httpd_resp_send_err(req, HTTPD_400_BAD_REQUEST, "body too large");

        return ESP_FAIL;
    }

    memset(buf, 0, sizeof buf);

    while (got < req->content_len) {
        int n = httpd_req_recv(req, buf + got, req->content_len - got);

        if (n == HTTPD_SOCK_ERR_TIMEOUT) {
            timeouts += 1;

            if (timeouts > 3) {
                wipe_chars(buf, sizeof buf);

                return ESP_FAIL;
            }

            continue;
        }

        if (n <= 0) {
            wipe_chars(buf, sizeof buf);

            return ESP_FAIL;
        }

        got += (size_t)n;
    }

    if (prov_form_parse(buf, got, &creds) != 0) {
        wipe_chars(buf, sizeof buf);

        return httpd_resp_send_err(req, HTTPD_400_BAD_REQUEST, "bad form");
    }

    if (skwoop_config_set_wifi(creds.ssid, creds.pass) != 0 || skwoop_config_save() != 0) {
        wipe_chars(buf, sizeof buf);
        wipe_chars(creds.ssid, sizeof creds.ssid);
        wipe_chars(creds.pass, sizeof creds.pass);

        return httpd_resp_send_err(req, HTTPD_500_INTERNAL_SERVER_ERROR, "save failed");
    }

    wipe_chars(buf, sizeof buf);
    wipe_chars(creds.ssid, sizeof creds.ssid);
    wipe_chars(creds.pass, sizeof creds.pass);

    if (httpd_resp_send(req, "saved, rebooting", HTTPD_RESP_USE_STRLEN) != ESP_OK ||
        schedule_reboot() != ESP_OK) {
        esp_restart();
    }

    return ESP_OK;
}

static const httpd_uri_t s_prov_root = {
    .uri = "/",
    .method = HTTP_GET,
    .handler = on_prov_get,
};

static const httpd_uri_t s_prov_save = {
    .uri = "/save",
    .method = HTTP_POST,
    .handler = on_prov_save,
};

static esp_err_t ap_stack(void)
{
    wifi_init_config_t init = WIFI_INIT_CONFIG_DEFAULT();
    esp_err_t err = esp_netif_init();

    if (err != ESP_OK && err != ESP_ERR_INVALID_STATE) {
        return err;
    }

    err = esp_event_loop_create_default();

    if (err != ESP_OK && err != ESP_ERR_INVALID_STATE) {
        return err;
    }

    if (s_started) {
        return ESP_OK;
    }

    err = esp_wifi_init(&init);

    if (err == ESP_ERR_INVALID_STATE) {
        return ESP_OK;
    }

    return err;
}

static void start_ap(void)
{
    uint8_t mac[6];
    wifi_config_t ap;
    char ssid[33];
    int ssid_n;
    httpd_handle_t server = NULL;
    httpd_config_t httpd_cfg = HTTPD_DEFAULT_CONFIG();
    esp_err_t err;

    if (s_ap) {
        return;
    }

    err = ap_stack();

    if (err != ESP_OK) {
        ap_fail(err);

        return;
    }

    if (esp_netif_create_default_wifi_ap() == NULL) {
        ap_fail(ESP_FAIL);

        return;
    }

    memset(mac, 0, sizeof mac);
    err = esp_wifi_get_mac(WIFI_IF_AP, mac);

    if (err != ESP_OK) {
        ap_fail(err);

        return;
    }

    ssid_n = snprintf(ssid, sizeof ssid, "%s-%02x%02x", CONFIG_SKWOOP_AP_SSID_PREFIX, (unsigned)mac[4],
                      (unsigned)mac[5]);

    if (ssid_n <= 0 || ssid_n >= (int)sizeof ssid || ssid_n > (int)sizeof ap.ap.ssid) {
        ap_fail(ESP_ERR_INVALID_SIZE);

        return;
    }

    memset(&ap, 0, sizeof ap);
    memcpy(ap.ap.ssid, ssid, (size_t)ssid_n);
    ap.ap.ssid_len = (uint8_t)ssid_n;
    ap.ap.authmode = WIFI_AUTH_OPEN;
    ap.ap.channel = 1;
    ap.ap.max_connection = 2;

    err = esp_wifi_set_mode(s_started ? WIFI_MODE_APSTA : WIFI_MODE_AP);

    if (err == ESP_OK) {
        err = esp_wifi_set_config(WIFI_IF_AP, &ap);
    }

    /* Already started: APSTA switches the running stack. Start only the cold path. */
    if (err == ESP_OK && !s_started) {
        err = esp_wifi_start();
    }

    if (err != ESP_OK) {
        ap_fail(err);

        return;
    }

    s_started = 1;

    err = httpd_start(&server, &httpd_cfg);

    if (err == ESP_OK) {
        err = httpd_register_uri_handler(server, &s_prov_root);
    }

    if (err == ESP_OK) {
        err = httpd_register_uri_handler(server, &s_prov_save);
    }

    if (err != ESP_OK) {
        if (server != NULL) {
            httpd_stop(server);
        }

        ap_fail(err);

        return;
    }

    s_ap = 1;
    ESP_LOGI(TAG, "ap %s", ssid);
    notify(WIFI_AP_MODE);
}

/* ESP-IDF event-loop callback. arg and data are the loop's void* boundary. */
static void on_sta_event(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    (void)arg;
    (void)data;

    if (base == WIFI_EVENT && id == WIFI_EVENT_STA_START) {
        esp_wifi_connect();

        return;
    }

    if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
        if (s_retries < WIFI_STA_RETRIES) {
            s_retries += 1;
            ESP_LOGI(TAG, "sta retry %d/%d", s_retries, WIFI_STA_RETRIES);
            esp_wifi_connect();

            return;
        }

        if (!s_fail_sent) {
            s_fail_sent = 1;
            ESP_LOGW(TAG, "sta failed");
            notify(WIFI_FAILED);
            start_ap();
        }

        return;
    }

    if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        s_retries = 0;
        s_fail_sent = 0;
        notify(WIFI_GOT_IP);
    }
}

static esp_err_t sta_stack(void)
{
    esp_err_t err = esp_netif_init();

    if (err != ESP_OK && err != ESP_ERR_INVALID_STATE) {
        return err;
    }

    err = esp_event_loop_create_default();

    if (err != ESP_OK && err != ESP_ERR_INVALID_STATE) {
        return err;
    }

    if (esp_netif_create_default_wifi_sta() == NULL) {
        return ESP_FAIL;
    }

    return ESP_OK;
}

static esp_err_t sta_apply(const char *ssid, const char *pass)
{
    wifi_config_t cfg;
    size_t ssid_len = strlen(ssid);
    size_t pass_len = strlen(pass);
    esp_err_t err;

    if (ssid_len == 0 || ssid_len > sizeof cfg.sta.ssid || pass_len > sizeof cfg.sta.password) {
        return ESP_ERR_INVALID_SIZE;
    }

    memset(&cfg, 0, sizeof cfg);
    memcpy(cfg.sta.ssid, ssid, ssid_len);
    memcpy(cfg.sta.password, pass, pass_len);
    cfg.sta.threshold.authmode = (pass_len == 0) ? WIFI_AUTH_OPEN : WIFI_AUTH_WPA2_PSK;

    err = esp_wifi_set_mode(WIFI_MODE_STA);

    if (err == ESP_OK) {
        err = esp_wifi_set_config(WIFI_IF_STA, &cfg);
    }

    /* SAFETY: wifi_config_t holds the passphrase; wipe the object as bytes. */
    wipe_chars((volatile char *)&cfg, sizeof cfg);

    return err;
}

esp_err_t wifi_start(wifi_state_cb cb)
{
    char ssid[SKWOOP_SSID_MAX + 1];
    char pass[SKWOOP_PASS_MAX + 1];
    wifi_init_config_t init = WIFI_INIT_CONFIG_DEFAULT();
    esp_err_t err;

    s_on_state = cb;

    if (s_started) {
        return ESP_ERR_INVALID_STATE;
    }

    memset(ssid, 0, sizeof ssid);
    memset(pass, 0, sizeof pass);

    if (skwoop_config_load() != 0 ||
        skwoop_config_get_wifi(ssid, sizeof ssid, pass, sizeof pass) != 0 ||
        ssid[0] == '\0') {
        wipe_chars(ssid, sizeof ssid);
        wipe_chars(pass, sizeof pass);
        s_fail_sent = 1;
        notify(WIFI_FAILED);
        start_ap();

        return ESP_OK;
    }

    err = sta_stack();

    if (err == ESP_OK) {
        err = esp_wifi_init(&init);
    }

    if (err == ESP_OK) {
        err = esp_event_handler_register(WIFI_EVENT, ESP_EVENT_ANY_ID, on_sta_event, NULL);
    }

    if (err == ESP_OK) {
        err = esp_event_handler_register(IP_EVENT, IP_EVENT_STA_GOT_IP, on_sta_event, NULL);
    }

    if (err == ESP_OK) {
        err = sta_apply(ssid, pass);
    }

    wipe_chars(ssid, sizeof ssid);
    wipe_chars(pass, sizeof pass);

    if (err != ESP_OK) {
        return err;
    }

    s_retries = 0;
    s_fail_sent = 0;
    notify(WIFI_CONNECTING);

    err = esp_wifi_start();

    if (err != ESP_OK) {
        notify(WIFI_FAILED);

        return err;
    }

    s_started = 1;

    return ESP_OK;
}
