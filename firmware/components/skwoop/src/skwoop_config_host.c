/*
 * Host file backend. Device firmware will replace this with NVS under
 * flash encryption; the AES key will come from eFuse, not host.key.
 * host.key is a random 32-byte test key created on first save.
 */

#include "skwoop_config.h"

#include <errno.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

#include <openssl/evp.h>
#include <openssl/rand.h>

#define SKWOOP_DEFAULT_DIR "/tmp/skwoop-config-test"
#define SKWOOP_GCM_IV 12
#define SKWOOP_GCM_TAG 16
#define SKWOOP_KEY_LEN 32
/* magic "SKS1" || iv(12) || tag(16) || ciphertext */
#define SKWOOP_SEC_HDR (4 + SKWOOP_GCM_IV + SKWOOP_GCM_TAG)

int skwoop_cfg_store_wifi(const char *ssid, const char *pass, int standalone);
int skwoop_cfg_load_wifi(char *ssid, size_t ssid_cap, char *pass, size_t pass_cap,
                         int *standalone, int *present);
int skwoop_cfg_store_secrets(const uint8_t *pt, size_t pt_len);
int skwoop_cfg_load_secrets(uint8_t *pt, size_t pt_cap, size_t *pt_len, int *present);
int skwoop_cfg_store_base(const char *url);
int skwoop_cfg_load_base(char *url, size_t url_cap, int *present);
int skwoop_cfg_store_clip_base(const char *url);
int skwoop_cfg_load_clip_base(char *url, size_t url_cap, int *present);

static int config_dir(char *out, size_t cap) {
    const char *env = getenv("SKWOOP_CONFIG_DIR");
    const char *dir = (env != NULL && env[0] != '\0') ? env : SKWOOP_DEFAULT_DIR;
    int n = snprintf(out, cap, "%s", dir);
    if (n < 0 || (size_t)n >= cap) {
        return -1;
    }
    return 0;
}

static int ensure_dir(const char *dir) {
    struct stat st;
    if (stat(dir, &st) == 0) {
        if (!S_ISDIR(st.st_mode)) {
            return -1;
        }
        return 0;
    }
    if (mkdir(dir, 0700) != 0 && errno != EEXIST) {
        return -1;
    }
    return 0;
}

static int join_path(char *out, size_t cap, const char *dir, const char *name) {
    int n = snprintf(out, cap, "%s/%s", dir, name);
    if (n < 0 || (size_t)n >= cap) {
        return -1;
    }
    return 0;
}

static void wipe(void *p, size_t n) {
    volatile unsigned char *v = (volatile unsigned char *)p;
    while (n > 0) {
        *v++ = 0;
        n--;
    }
}

static int write_atomic(const char *path, const void *data, size_t len, mode_t mode) {
    char tmp[512];
    FILE *f;
    int n = snprintf(tmp, sizeof tmp, "%s.tmp", path);
    if (n < 0 || (size_t)n >= sizeof tmp) {
        return -1;
    }
    f = fopen(tmp, "wb");
    if (f == NULL) {
        return -1;
    }
    if (len > 0 && fwrite(data, 1, len, f) != len) {
        fclose(f);
        remove(tmp);
        return -1;
    }
    if (fflush(f) != 0 || fsync(fileno(f)) != 0) {
        fclose(f);
        remove(tmp);
        return -1;
    }
    if (fchmod(fileno(f), mode) != 0) {
        fclose(f);
        remove(tmp);
        return -1;
    }
    if (fclose(f) != 0) {
        remove(tmp);
        return -1;
    }
    if (rename(tmp, path) != 0) {
        remove(tmp);
        return -1;
    }
    return 0;
}

static int read_whole(const char *path, unsigned char *buf, size_t cap, size_t *out,
                      int *present) {
    FILE *f = fopen(path, "rb");
    size_t n;
    if (f == NULL) {
        if (errno == ENOENT) {
            *present = 0;
            *out = 0;
            return 0;
        }
        return -1;
    }
    *present = 1;
    n = fread(buf, 1, cap, f);
    if (ferror(f)) {
        fclose(f);
        return -1;
    }
    if (n == cap) {
        unsigned char extra;
        if (fread(&extra, 1, 1, f) == 1) {
            fclose(f);
            return -1;
        }
    }
    fclose(f);
    *out = n;
    return 0;
}

static int append_json_str(FILE *f, const char *s) {
    const unsigned char *p;
    if (fputc('"', f) == EOF) {
        return -1;
    }
    for (p = (const unsigned char *)s; *p != 0; p++) {
        if (*p == '"' || *p == '\\') {
            if (fprintf(f, "\\%c", *p) < 0) {
                return -1;
            }
        } else if (*p < 0x20) {
            if (fprintf(f, "\\u%04x", *p) < 0) {
                return -1;
            }
        } else {
            if (fputc(*p, f) == EOF) {
                return -1;
            }
        }
    }
    if (fputc('"', f) == EOF) {
        return -1;
    }
    return 0;
}

static int write_wifi_file(const char *path, const char *ssid, const char *pass,
                           int standalone) {
    char tmp[512];
    FILE *f;
    int n = snprintf(tmp, sizeof tmp, "%s.tmp", path);
    if (n < 0 || (size_t)n >= sizeof tmp) {
        return -1;
    }
    f = fopen(tmp, "wb");
    if (f == NULL) {
        return -1;
    }
    if (fprintf(f, "{\"ssid\":") < 0 || append_json_str(f, ssid) != 0 ||
        fprintf(f, ",\"pass\":") < 0 || append_json_str(f, pass) != 0 ||
        fprintf(f, ",\"standalone\":%d}\n", standalone ? 1 : 0) < 0) {
        fclose(f);
        remove(tmp);
        return -1;
    }
    if (fflush(f) != 0 || fsync(fileno(f)) != 0 || fchmod(fileno(f), 0644) != 0 ||
        fclose(f) != 0) {
        remove(tmp);
        return -1;
    }
    if (rename(tmp, path) != 0) {
        remove(tmp);
        return -1;
    }
    return 0;
}

static const char *skip_ws(const char *p, const char *end) {
    while (p < end && (*p == ' ' || *p == '\t' || *p == '\n' || *p == '\r')) {
        p++;
    }
    return p;
}

static int hex_nibble(char c) {
    if (c >= '0' && c <= '9') {
        return c - '0';
    }
    if (c >= 'a' && c <= 'f') {
        return c - 'a' + 10;
    }
    if (c >= 'A' && c <= 'F') {
        return c - 'A' + 10;
    }
    return -1;
}

static int parse_json_string(const char **pp, const char *end, char *dst, size_t cap) {
    const char *p = *pp;
    size_t n = 0;
    if (p >= end || *p != '"') {
        return -1;
    }
    p++;
    while (p < end) {
        unsigned char c;
        if (*p == '"') {
            if (n >= cap) {
                return -1;
            }
            dst[n] = '\0';
            *pp = p + 1;
            return 0;
        }
        if (*p == '\\') {
            if (p + 1 >= end) {
                return -1;
            }
            p++;
            if (*p == '"' || *p == '\\' || *p == '/') {
                c = (unsigned char)*p;
                p++;
            } else if (*p == 'n') {
                c = '\n';
                p++;
            } else if (*p == 'r') {
                c = '\r';
                p++;
            } else if (*p == 't') {
                c = '\t';
                p++;
            } else if (*p == 'u') {
                int v = 0;
                int i;
                if (p + 5 > end) {
                    return -1;
                }
                for (i = 1; i <= 4; i++) {
                    int h = hex_nibble(p[i]);
                    if (h < 0) {
                        return -1;
                    }
                    v = (v << 4) | h;
                }
                if (v > 0xff) {
                    return -1;
                }
                c = (unsigned char)v;
                p += 5;
            } else {
                return -1;
            }
        } else {
            c = (unsigned char)*p;
            if (c < 0x20) {
                return -1;
            }
            p++;
        }
        if (n + 1 >= cap) {
            return -1;
        }
        dst[n++] = (char)c;
    }
    return -1;
}

static int expect_lit(const char **pp, const char *end, const char *lit) {
    size_t n = strlen(lit);
    const char *p = skip_ws(*pp, end);
    if ((size_t)(end - p) < n || memcmp(p, lit, n) != 0) {
        return -1;
    }
    *pp = p + n;
    return 0;
}

int skwoop_cfg_load_wifi(char *ssid, size_t ssid_cap, char *pass, size_t pass_cap,
                         int *standalone, int *present) {
    char dir[400];
    char path[512];
    unsigned char raw[2048];
    size_t len = 0;
    const char *p;
    const char *end;
    int stand = 0;

    if (config_dir(dir, sizeof dir) != 0 || join_path(path, sizeof path, dir, "wifi.json") != 0) {
        return -1;
    }
    if (read_whole(path, raw, sizeof raw - 1, &len, present) != 0) {
        return -1;
    }
    if (!*present) {
        return 0;
    }
    raw[len] = 0;
    p = (const char *)raw;
    end = p + len;
    if (expect_lit(&p, end, "{") != 0 || expect_lit(&p, end, "\"ssid\"") != 0 ||
        expect_lit(&p, end, ":") != 0 || parse_json_string(&p, end, ssid, ssid_cap) != 0 ||
        expect_lit(&p, end, ",") != 0 || expect_lit(&p, end, "\"pass\"") != 0 ||
        expect_lit(&p, end, ":") != 0 || parse_json_string(&p, end, pass, pass_cap) != 0 ||
        expect_lit(&p, end, ",") != 0 || expect_lit(&p, end, "\"standalone\"") != 0 ||
        expect_lit(&p, end, ":") != 0) {
        return -1;
    }
    p = skip_ws(p, end);
    if (p >= end || (*p != '0' && *p != '1')) {
        return -1;
    }
    stand = *p - '0';
    p++;
    if (expect_lit(&p, end, "}") != 0) {
        return -1;
    }
    p = skip_ws(p, end);
    if (p != end) {
        return -1;
    }
    *standalone = stand;
    return 0;
}

int skwoop_cfg_store_wifi(const char *ssid, const char *pass, int standalone) {
    char dir[400];
    char path[512];
    if (ssid == NULL || pass == NULL) {
        return -1;
    }
    if (config_dir(dir, sizeof dir) != 0 || ensure_dir(dir) != 0 ||
        join_path(path, sizeof path, dir, "wifi.json") != 0) {
        return -1;
    }
    return write_wifi_file(path, ssid, pass, standalone);
}

/* 0 ok, 1 missing, -1 error (does not create or replace). */
static int read_key_file(const char *path, unsigned char key[SKWOOP_KEY_LEN]) {
    FILE *f = fopen(path, "rb");
    size_t n;
    unsigned char extra;
    if (f == NULL) {
        return (errno == ENOENT) ? 1 : -1;
    }
    n = fread(key, 1, SKWOOP_KEY_LEN, f);
    if (ferror(f) || n != SKWOOP_KEY_LEN || fread(&extra, 1, 1, f) == 1) {
        fclose(f);
        wipe(key, SKWOOP_KEY_LEN);
        return -1;
    }
    fclose(f);
    return 0;
}

static int load_key(const char *dir, unsigned char key[SKWOOP_KEY_LEN]) {
    char path[512];
    int rc;
    if (join_path(path, sizeof path, dir, "host.key") != 0) {
        return -1;
    }
    rc = read_key_file(path, key);
    return rc == 0 ? 0 : -1;
}

static int load_or_create_key(const char *dir, unsigned char key[SKWOOP_KEY_LEN]) {
    char path[512];
    int rc;
    if (join_path(path, sizeof path, dir, "host.key") != 0) {
        return -1;
    }
    rc = read_key_file(path, key);
    if (rc == 0) {
        return 0;
    }
    if (rc < 0) {
        return -1;
    }
    if (RAND_bytes(key, SKWOOP_KEY_LEN) != 1) {
        return -1;
    }
    if (write_atomic(path, key, SKWOOP_KEY_LEN, 0600) != 0) {
        wipe(key, SKWOOP_KEY_LEN);
        return -1;
    }
    return 0;
}

static int aes_gcm_encrypt(const unsigned char *key, const uint8_t *pt, size_t pt_len,
                           unsigned char *out, size_t *out_len) {
    EVP_CIPHER_CTX *ctx = NULL;
    unsigned char iv[SKWOOP_GCM_IV];
    unsigned char tag[SKWOOP_GCM_TAG];
    int outl = 0;
    int fin = 0;
    int ok = -1;

    if (RAND_bytes(iv, sizeof iv) != 1) {
        return -1;
    }
    ctx = EVP_CIPHER_CTX_new();
    if (ctx == NULL) {
        return -1;
    }
    if (EVP_EncryptInit_ex(ctx, EVP_aes_256_gcm(), NULL, NULL, NULL) != 1 ||
        EVP_CIPHER_CTX_ctrl(ctx, EVP_CTRL_GCM_SET_IVLEN, SKWOOP_GCM_IV, NULL) != 1 ||
        EVP_EncryptInit_ex(ctx, NULL, NULL, key, iv) != 1 ||
        EVP_EncryptUpdate(ctx, out + SKWOOP_SEC_HDR, &outl, pt, (int)pt_len) != 1 ||
        EVP_EncryptFinal_ex(ctx, out + SKWOOP_SEC_HDR + outl, &fin) != 1 ||
        EVP_CIPHER_CTX_ctrl(ctx, EVP_CTRL_GCM_GET_TAG, SKWOOP_GCM_TAG, tag) != 1) {
        goto done;
    }
    memcpy(out, "SKS1", 4);
    memcpy(out + 4, iv, SKWOOP_GCM_IV);
    memcpy(out + 4 + SKWOOP_GCM_IV, tag, SKWOOP_GCM_TAG);
    *out_len = SKWOOP_SEC_HDR + (size_t)outl + (size_t)fin;
    ok = 0;
done:
    EVP_CIPHER_CTX_free(ctx);
    wipe(iv, sizeof iv);
    wipe(tag, sizeof tag);
    return ok;
}

static int aes_gcm_decrypt(const unsigned char *key, const unsigned char *in, size_t in_len,
                           uint8_t *pt, size_t pt_cap, size_t *pt_len) {
    EVP_CIPHER_CTX *ctx = NULL;
    unsigned char tag[SKWOOP_GCM_TAG];
    int outl = 0;
    int fin = 0;
    int ok = -1;
    size_t ct_len;

    if (in_len < SKWOOP_SEC_HDR || memcmp(in, "SKS1", 4) != 0) {
        return -1;
    }
    ct_len = in_len - SKWOOP_SEC_HDR;
    if (ct_len > pt_cap) {
        return -1;
    }
    memcpy(tag, in + 4 + SKWOOP_GCM_IV, SKWOOP_GCM_TAG);
    ctx = EVP_CIPHER_CTX_new();
    if (ctx == NULL) {
        return -1;
    }
    if (EVP_DecryptInit_ex(ctx, EVP_aes_256_gcm(), NULL, NULL, NULL) != 1 ||
        EVP_CIPHER_CTX_ctrl(ctx, EVP_CTRL_GCM_SET_IVLEN, SKWOOP_GCM_IV, NULL) != 1 ||
        EVP_DecryptInit_ex(ctx, NULL, NULL, key, in + 4) != 1 ||
        EVP_DecryptUpdate(ctx, pt, &outl, in + SKWOOP_SEC_HDR, (int)ct_len) != 1 ||
        EVP_CIPHER_CTX_ctrl(ctx, EVP_CTRL_GCM_SET_TAG, SKWOOP_GCM_TAG, tag) != 1 ||
        EVP_DecryptFinal_ex(ctx, pt + outl, &fin) != 1) {
        goto done;
    }
    *pt_len = (size_t)outl + (size_t)fin;
    ok = 0;
done:
    EVP_CIPHER_CTX_free(ctx);
    wipe(tag, sizeof tag);
    if (ok != 0) {
        wipe(pt, pt_cap);
    }
    return ok;
}

int skwoop_cfg_store_secrets(const uint8_t *pt, size_t pt_len) {
    char dir[400];
    char path[512];
    unsigned char key[SKWOOP_KEY_LEN];
    unsigned char blob[SKWOOP_SEC_HDR + 4096];
    size_t blob_len = 0;
    int rc = -1;

    if (pt == NULL || pt_len > 4096) {
        return -1;
    }
    if (config_dir(dir, sizeof dir) != 0 || ensure_dir(dir) != 0 ||
        join_path(path, sizeof path, dir, "secrets.bin") != 0) {
        return -1;
    }
    if (load_or_create_key(dir, key) != 0) {
        return -1;
    }
    if (aes_gcm_encrypt(key, pt, pt_len, blob, &blob_len) != 0) {
        goto done;
    }
    rc = write_atomic(path, blob, blob_len, 0600);
done:
    wipe(key, sizeof key);
    wipe(blob, sizeof blob);
    return rc;
}

int skwoop_cfg_load_secrets(uint8_t *pt, size_t pt_cap, size_t *pt_len, int *present) {
    char dir[400];
    char path[512];
    unsigned char blob[SKWOOP_SEC_HDR + 4096];
    unsigned char key[SKWOOP_KEY_LEN];
    size_t blob_len = 0;
    int rc;

    if (config_dir(dir, sizeof dir) != 0 ||
        join_path(path, sizeof path, dir, "secrets.bin") != 0) {
        return -1;
    }
    if (read_whole(path, blob, sizeof blob, &blob_len, present) != 0) {
        return -1;
    }
    if (!*present) {
        return 0;
    }
    /* Do not create host.key here. A fresh key cannot open an existing blob.
     * Device builds will use flash encryption / eFuse instead of host.key. */
    if (load_key(dir, key) != 0) {
        wipe(blob, sizeof blob);
        return -1;
    }
    rc = aes_gcm_decrypt(key, blob, blob_len, pt, pt_cap, pt_len);
    wipe(key, sizeof key);
    wipe(blob, sizeof blob);
    return rc;
}

int skwoop_cfg_store_base(const char *url) {
    char dir[400];
    char path[512];
    char body[SKWOOP_BASE_URL_MAX + 2];
    size_t n;

    if (config_dir(dir, sizeof dir) != 0 || ensure_dir(dir) != 0 ||
        join_path(path, sizeof path, dir, "provider_base.txt") != 0) {
        return -1;
    }
    if (url == NULL || url[0] == '\0') {
        if (unlink(path) != 0 && errno != ENOENT) {
            return -1;
        }
        return 0;
    }
    n = strlen(url);
    if (n == 0 || n > SKWOOP_BASE_URL_MAX) {
        return -1;
    }
    memcpy(body, url, n);
    body[n] = '\n';
    return write_atomic(path, body, n + 1, 0644);
}

int skwoop_cfg_load_base(char *url, size_t url_cap, int *present) {
    char dir[400];
    char path[512];
    unsigned char raw[SKWOOP_BASE_URL_MAX + 8];
    size_t len = 0;
    size_t n;

    if (url == NULL || url_cap == 0 || present == NULL) {
        return -1;
    }
    if (config_dir(dir, sizeof dir) != 0 ||
        join_path(path, sizeof path, dir, "provider_base.txt") != 0) {
        return -1;
    }
    if (read_whole(path, raw, sizeof raw, &len, present) != 0) {
        return -1;
    }
    if (!*present) {
        return 0;
    }
    if (len == 0 || len > SKWOOP_BASE_URL_MAX + 1 || raw[len - 1] != '\n') {
        return -1;
    }
    n = len - 1;
    if (n == 0 || n >= url_cap) {
        return -1;
    }
    if (memchr(raw, '\n', n) != NULL || memchr(raw, '\r', n) != NULL) {
        return -1;
    }
    memcpy(url, raw, n);
    url[n] = '\0';
    if (strncmp(url, "https://", 8) != 0 || url[8] == '\0' || url[8] == '/') {
        return -1;
    }
    return 0;
}

int skwoop_cfg_store_clip_base(const char *url) {
    char dir[400];
    char path[512];
    char body[SKWOOP_BASE_URL_MAX + 2];
    size_t n;

    if (config_dir(dir, sizeof dir) != 0 || ensure_dir(dir) != 0 ||
        join_path(path, sizeof path, dir, "clip_base.txt") != 0) {
        return -1;
    }
    if (url == NULL || url[0] == '\0') {
        if (unlink(path) != 0 && errno != ENOENT) {
            return -1;
        }
        return 0;
    }
    n = strlen(url);
    if (n == 0 || n > SKWOOP_BASE_URL_MAX) {
        return -1;
    }
    memcpy(body, url, n);
    body[n] = '\n';
    return write_atomic(path, body, n + 1, 0644);
}

int skwoop_cfg_load_clip_base(char *url, size_t url_cap, int *present) {
    char dir[400];
    char path[512];
    unsigned char raw[SKWOOP_BASE_URL_MAX + 8];
    size_t len = 0;
    size_t n;
    int http;

    if (url == NULL || url_cap == 0 || present == NULL) {
        return -1;
    }
    if (config_dir(dir, sizeof dir) != 0 ||
        join_path(path, sizeof path, dir, "clip_base.txt") != 0) {
        return -1;
    }
    if (read_whole(path, raw, sizeof raw, &len, present) != 0) {
        return -1;
    }
    if (!*present) {
        return 0;
    }
    if (len == 0 || len > SKWOOP_BASE_URL_MAX + 1 || raw[len - 1] != '\n') {
        return -1;
    }
    n = len - 1;
    if (n == 0 || n >= url_cap) {
        return -1;
    }
    if (memchr(raw, '\n', n) != NULL || memchr(raw, '\r', n) != NULL) {
        return -1;
    }
    memcpy(url, raw, n);
    url[n] = '\0';
    http = strncmp(url, "http://", 7) == 0;
    if (strncmp(url, "https://", 8) != 0 && !http) {
        return -1;
    }
    if (http) {
        if (url[7] == '\0' || url[7] == '/' || url[7] == ':') {
            return -1;
        }
    } else if (url[8] == '\0' || url[8] == '/' || url[8] == ':') {
        return -1;
    }
    return 0;
}
