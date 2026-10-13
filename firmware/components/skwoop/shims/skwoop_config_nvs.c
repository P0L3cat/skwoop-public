/* Device backend for skwoop_config: NVS (namespace "skwoop") replaces the
 * host files; AES-256-GCM via mbedTLS replaces OpenSSL EVP. The GCM key is a
 * random 32-byte blob kept in NVS; with CONFIG_NVS_ENCRYPTION + flash
 * encryption the NVS partition itself is encrypted (keys in nvs_keys partition,
 * protected by flash-encryption eFuse key). Blob layout matches host: SKS1|iv|tag|ct. */
#include "skwoop_config.h"
#include <string.h>
#include <stdint.h>
#include "nvs_flash.h"
#include "nvs.h"
#include "esp_random.h"
#include "esp_idf_version.h"
#if ESP_IDF_VERSION_MAJOR >= 6
/* IDF 6 ships mbedTLS 4 (PSA-only): legacy mbedtls/gcm.h is gone. */
#include "psa/crypto.h"
static int gcm(int enc, const uint8_t *key, const uint8_t *iv, const uint8_t *in, size_t n,
               uint8_t *out, uint8_t *tag) {
    psa_key_attributes_t a = PSA_KEY_ATTRIBUTES_INIT; psa_key_id_t id; size_t ol; int rc = -1;
    static uint8_t tmp[4096 + 16];
    if (psa_crypto_init() != PSA_SUCCESS || n > 4096) return -1;
    psa_set_key_type(&a, PSA_KEY_TYPE_AES); psa_set_key_bits(&a, 256);
    psa_set_key_usage_flags(&a, PSA_KEY_USAGE_ENCRYPT | PSA_KEY_USAGE_DECRYPT);
    psa_set_key_algorithm(&a, PSA_ALG_GCM);
    if (psa_import_key(&a, key, 32, &id) != PSA_SUCCESS) return -1;
    if (enc) {
        if (psa_aead_encrypt(id, PSA_ALG_GCM, iv, 12, NULL, 0, in, n, tmp, sizeof tmp, &ol) == PSA_SUCCESS && ol == n + 16) {
            memcpy(out, tmp, n); memcpy(tag, tmp + n, 16); rc = 0; }
    } else {
        memcpy(tmp, in, n); memcpy(tmp + n, tag, 16);
        if (psa_aead_decrypt(id, PSA_ALG_GCM, iv, 12, NULL, 0, tmp, n + 16, out, n, &ol) == PSA_SUCCESS) rc = 0;
    }
    memset(tmp, 0, sizeof tmp); psa_destroy_key(id); return rc;
}
#define SKW_ENC(key, iv, pt, n, ct, tag) gcm(1, key, iv, pt, n, ct, tag)
#define SKW_DEC(key, iv, ct, n, pt, tag) gcm(0, key, iv, ct, n, pt, (uint8_t *)(tag))
#else
#include "mbedtls/gcm.h"
static int gcm3(int enc, const uint8_t *key, const uint8_t *iv, const uint8_t *in, size_t n, uint8_t *out, uint8_t *tag) {
    mbedtls_gcm_context g; int rc = -1; mbedtls_gcm_init(&g);
    if (mbedtls_gcm_setkey(&g, MBEDTLS_CIPHER_ID_AES, key, 256) == 0)
        rc = enc ? mbedtls_gcm_crypt_and_tag(&g, MBEDTLS_GCM_ENCRYPT, n, iv, 12, NULL, 0, in, out, 16, tag)
                 : mbedtls_gcm_auth_decrypt(&g, n, iv, 12, NULL, 0, tag, 16, in, out);
    mbedtls_gcm_free(&g); return rc ? -1 : 0;
}
#define SKW_ENC(key, iv, pt, n, ct, tag) gcm3(1, key, iv, pt, n, ct, tag)
#define SKW_DEC(key, iv, ct, n, pt, tag) gcm3(0, key, iv, ct, n, pt, (uint8_t *)(tag))
#endif

#define IVL 12
#define TAGL 16
#define KEYL 32
#define HDR (4 + IVL + TAGL)
#define NS "skwoop"

static int open_ns(nvs_handle_t *h, nvs_open_mode_t m) {
    static int inited;
    if (!inited) {
        esp_err_t e = nvs_flash_init();
        if (e == ESP_ERR_NVS_NO_FREE_PAGES || e == ESP_ERR_NVS_NEW_VERSION_FOUND) {
            nvs_flash_erase(); e = nvs_flash_init();
        }
        if (e != ESP_OK) return -1;
        inited = 1;
    }
    return nvs_open(NS, m, h) == ESP_OK ? 0 : -1;
}
static int put_str(const char *k, const char *v) {
    nvs_handle_t h; if (open_ns(&h, NVS_READWRITE)) return -1;
    int rc = (nvs_set_str(h, k, v) == ESP_OK && nvs_commit(h) == ESP_OK) ? 0 : -1;
    nvs_close(h); return rc;
}
static int get_str(const char *k, char *v, size_t cap, int *present) {
    nvs_handle_t h; *present = 0; if (open_ns(&h, NVS_READONLY)) return 0;
    size_t n = cap; esp_err_t e = nvs_get_str(h, k, v, &n); nvs_close(h);
    if (e == ESP_ERR_NVS_NOT_FOUND) return 0;
    if (e != ESP_OK) return -1;
    *present = 1; return 0;
}
int skwoop_cfg_store_wifi(const char *ssid, const char *pass, int standalone) {
    nvs_handle_t h; if (open_ns(&h, NVS_READWRITE)) return -1;
    int rc = (nvs_set_str(h, "ssid", ssid) == ESP_OK && nvs_set_str(h, "pass", pass) == ESP_OK &&
              nvs_set_u8(h, "standalone", standalone ? 1 : 0) == ESP_OK && nvs_commit(h) == ESP_OK) ? 0 : -1;
    nvs_close(h); return rc;
}
int skwoop_cfg_load_wifi(char *ssid, size_t sc, char *pass, size_t pc, int *standalone, int *present) {
    nvs_handle_t h; uint8_t sa = 0; *present = 0;
    if (open_ns(&h, NVS_READONLY)) return 0;
    size_t a = sc, b = pc;
    esp_err_t e = nvs_get_str(h, "ssid", ssid, &a);
    if (e == ESP_OK) e = nvs_get_str(h, "pass", pass, &b);
    if (e == ESP_OK) nvs_get_u8(h, "standalone", &sa);
    nvs_close(h);
    if (e == ESP_ERR_NVS_NOT_FOUND) return 0;
    if (e != ESP_OK) return -1;
    *standalone = sa; *present = 1; return 0;
}
static int get_key(uint8_t key[KEYL], int create) {
    nvs_handle_t h; if (open_ns(&h, create ? NVS_READWRITE : NVS_READONLY)) return -1;
    size_t n = KEYL; esp_err_t e = nvs_get_blob(h, "gcmkey", key, &n);
    if (e == ESP_ERR_NVS_NOT_FOUND && create) {
        esp_fill_random(key, KEYL);
        e = nvs_set_blob(h, "gcmkey", key, KEYL); if (e == ESP_OK) e = nvs_commit(h);
    } else if (e == ESP_OK && n != KEYL) e = ESP_FAIL;
    nvs_close(h); return e == ESP_OK ? 0 : -1;
}
int skwoop_cfg_store_secrets(const uint8_t *pt, size_t len) {
    static uint8_t blob[HDR + 4096]; uint8_t key[KEYL]; int rc = -1;
    if (!pt || len > 4096 || get_key(key, 1)) return -1;
    memcpy(blob, "SKS1", 4); esp_fill_random(blob + 4, IVL);
    if (SKW_ENC(key, blob + 4, pt, len, blob + HDR, blob + 4 + IVL) == 0) {
        nvs_handle_t h;
        if (!open_ns(&h, NVS_READWRITE)) {
            rc = (nvs_set_blob(h, "secrets", blob, HDR + len) == ESP_OK && nvs_commit(h) == ESP_OK) ? 0 : -1;
            nvs_close(h);
        }
    }
    memset(key, 0, KEYL); memset(blob, 0, sizeof blob); return rc;
}
int skwoop_cfg_load_secrets(uint8_t *pt, size_t cap, size_t *plen, int *present) {
    static uint8_t blob[HDR + 4096]; uint8_t key[KEYL]; nvs_handle_t h; int rc = -1;
    *present = 0; if (open_ns(&h, NVS_READONLY)) return 0;
    size_t n = sizeof blob; esp_err_t e = nvs_get_blob(h, "secrets", blob, &n); nvs_close(h);
    if (e == ESP_ERR_NVS_NOT_FOUND) return 0;
    if (e != ESP_OK || n < HDR || memcmp(blob, "SKS1", 4) || n - HDR > cap) return -1;
    *present = 1; if (get_key(key, 0)) return -1;
    if (SKW_DEC(key, blob + 4, blob + HDR, n - HDR, pt, blob + 4 + IVL) == 0) { *plen = n - HDR; rc = 0; }
    memset(key, 0, KEYL); memset(blob, 0, sizeof blob);
    if (rc) { memset(pt, 0, cap); }
    return rc;
}
int skwoop_cfg_store_base(const char *u) { return put_str("prov_base", u); }
int skwoop_cfg_load_base(char *u, size_t c, int *p) { return get_str("prov_base", u, c, p); }
int skwoop_cfg_store_clip_base(const char *u) { return put_str("clip_base", u); }
int skwoop_cfg_load_clip_base(char *u, size_t c, int *p) { return get_str("clip_base", u, c, p); }
