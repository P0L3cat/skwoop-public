#include "skwoop_config.h"

#include <stdint.h>
#include <string.h>

/* Host backend (src/skwoop_config_host.c). */
int skwoop_cfg_store_wifi(const char *ssid, const char *pass, int standalone);
int skwoop_cfg_load_wifi(char *ssid, size_t ssid_cap, char *pass, size_t pass_cap,
                         int *standalone, int *present);
int skwoop_cfg_store_secrets(const uint8_t *pt, size_t pt_len);
int skwoop_cfg_load_secrets(uint8_t *pt, size_t pt_cap, size_t *pt_len, int *present);
/* url NULL deletes provider_base.txt. */
int skwoop_cfg_store_base(const char *url);
int skwoop_cfg_load_base(char *url, size_t url_cap, int *present);
/* url NULL deletes clip_base.txt. */
int skwoop_cfg_store_clip_base(const char *url);
int skwoop_cfg_load_clip_base(char *url, size_t url_cap, int *present);

typedef struct skwoop_key_slot {
    int used;
    char provider[SKWOOP_PROVIDER_MAX + 1];
    char key[SKWOOP_API_KEY_MAX + 1];
} skwoop_key_slot;

static char g_ssid[SKWOOP_SSID_MAX + 1];
static char g_pass[SKWOOP_PASS_MAX + 1];
static char g_base[SKWOOP_BASE_URL_MAX + 1];
static char g_clip[SKWOOP_BASE_URL_MAX + 1];
static int g_wifi_set;
static int g_base_set;
static int g_clip_set;
static int g_standalone;
static skwoop_key_slot g_keys[SKWOOP_API_KEY_SLOTS];

static void wipe(void *p, size_t n) {
    volatile unsigned char *v = (volatile unsigned char *)p;
    while (n > 0) {
        *v++ = 0;
        n--;
    }
}

static void clear_ram(void) {
    wipe(g_ssid, sizeof g_ssid);
    wipe(g_pass, sizeof g_pass);
    wipe(g_base, sizeof g_base);
    wipe(g_clip, sizeof g_clip);
    g_wifi_set = 0;
    g_base_set = 0;
    g_clip_set = 0;
    g_standalone = 0;
    wipe(g_keys, sizeof g_keys);
}

static int provider_ok(const char *provider, size_t *out_len) {
    size_t i;
    if (provider == NULL || provider[0] == '\0') {
        return -1;
    }
    for (i = 0; provider[i] != '\0'; i++) {
        unsigned char c = (unsigned char)provider[i];
        int alnum = (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
                    (c >= '0' && c <= '9');
        if (!alnum && c != '_' && c != '-' && c != '.') {
            return -1;
        }
        if (i >= SKWOOP_PROVIDER_MAX) {
            return -1;
        }
    }
    *out_len = i;
    return 0;
}

static int copy_out(const char *src, char *dst, size_t cap) {
    size_t n;
    if (dst == NULL || cap == 0) {
        return -1;
    }
    n = strlen(src);
    if (n >= cap) {
        return -1;
    }
    memcpy(dst, src, n + 1);
    return 0;
}

static int find_slot(const char *provider) {
    int i;
    for (i = 0; i < SKWOOP_API_KEY_SLOTS; i++) {
        if (g_keys[i].used && strcmp(g_keys[i].provider, provider) == 0) {
            return i;
        }
    }
    return -1;
}

static int pack_secrets(uint8_t *buf, size_t cap, size_t *out_len) {
    size_t o = 4;
    uint32_t count = 0;
    int i;
    if (cap < 4) {
        return -1;
    }
    for (i = 0; i < SKWOOP_API_KEY_SLOTS; i++) {
        size_t plen;
        size_t klen;
        if (!g_keys[i].used) {
            continue;
        }
        plen = strlen(g_keys[i].provider);
        klen = strlen(g_keys[i].key);
        if (plen > 255 || klen > 65535 || o + 1 + plen + 2 + klen > cap) {
            return -1;
        }
        buf[o++] = (uint8_t)plen;
        memcpy(buf + o, g_keys[i].provider, plen);
        o += plen;
        buf[o++] = (uint8_t)((klen >> 8) & 0xff);
        buf[o++] = (uint8_t)(klen & 0xff);
        memcpy(buf + o, g_keys[i].key, klen);
        o += klen;
        count++;
    }
    buf[0] = (uint8_t)((count >> 24) & 0xff);
    buf[1] = (uint8_t)((count >> 16) & 0xff);
    buf[2] = (uint8_t)((count >> 8) & 0xff);
    buf[3] = (uint8_t)(count & 0xff);
    *out_len = o;
    return 0;
}

static int unpack_secrets(const uint8_t *buf, size_t len) {
    size_t o = 4;
    uint32_t count;
    uint32_t n;
    if (len < 4) {
        return -1;
    }
    count = ((uint32_t)buf[0] << 24) | ((uint32_t)buf[1] << 16) |
            ((uint32_t)buf[2] << 8) | (uint32_t)buf[3];
    if (count > SKWOOP_API_KEY_SLOTS) {
        return -1;
    }
    for (n = 0; n < count; n++) {
        uint8_t plen;
        uint16_t klen;
        int slot;
        if (o + 1 > len) {
            return -1;
        }
        plen = buf[o++];
        if (plen == 0 || plen > SKWOOP_PROVIDER_MAX || o + plen + 2 > len) {
            return -1;
        }
        if (o + plen + 2 > len) {
            return -1;
        }
        klen = (uint16_t)(((uint16_t)buf[o + plen] << 8) | buf[o + plen + 1]);
        if (klen == 0 || klen > SKWOOP_API_KEY_MAX ||
            o + plen + 2 + klen > len) {
            return -1;
        }
        {
            char provider[SKWOOP_PROVIDER_MAX + 1];
            size_t got_len = 0;
            memcpy(provider, buf + o, plen);
            provider[plen] = '\0';
            o += plen + 2;
            if (provider_ok(provider, &got_len) != 0 || got_len != plen) {
                return -1;
            }
            if (find_slot(provider) >= 0) {
                return -1;
            }
            for (slot = 0; slot < SKWOOP_API_KEY_SLOTS; slot++) {
                if (!g_keys[slot].used) {
                    break;
                }
            }
            if (slot == SKWOOP_API_KEY_SLOTS) {
                return -1;
            }
            memcpy(g_keys[slot].provider, provider, plen + 1);
            memcpy(g_keys[slot].key, buf + o, klen);
            g_keys[slot].key[klen] = '\0';
            g_keys[slot].used = 1;
            o += klen;
            wipe(provider, sizeof provider);
        }
    }
    if (o != len) {
        return -1;
    }
    return 0;
}

int skwoop_config_load(void) {
    char ssid[SKWOOP_SSID_MAX + 1];
    char pass[SKWOOP_PASS_MAX + 1];
    uint8_t pt[4 + SKWOOP_API_KEY_SLOTS * (1 + SKWOOP_PROVIDER_MAX + 2 + SKWOOP_API_KEY_MAX)];
    size_t pt_len = 0;
    char base[SKWOOP_BASE_URL_MAX + 1];
    int standalone = 0;
    int wifi_present = 0;
    int sec_present = 0;
    int base_present = 0;
    int rc;

    clear_ram();
    memset(ssid, 0, sizeof ssid);
    memset(pass, 0, sizeof pass);
    memset(base, 0, sizeof base);

    rc = skwoop_cfg_load_wifi(ssid, sizeof ssid, pass, sizeof pass, &standalone,
                              &wifi_present);
    if (rc != 0) {
        clear_ram();
        wipe(ssid, sizeof ssid);
        wipe(pass, sizeof pass);
        wipe(base, sizeof base);
        return -1;
    }
    if (wifi_present) {
        if (strlen(ssid) > SKWOOP_SSID_MAX || strlen(pass) > SKWOOP_PASS_MAX ||
            (ssid[0] == '\0' && pass[0] != '\0')) {
            clear_ram();
            wipe(ssid, sizeof ssid);
            wipe(pass, sizeof pass);
            wipe(base, sizeof base);
            return -1;
        }
        /* Empty ssid means Wi-Fi was never set. Standalone still persists. */
        if (ssid[0] != '\0') {
            memcpy(g_ssid, ssid, strlen(ssid) + 1);
            memcpy(g_pass, pass, strlen(pass) + 1);
            g_wifi_set = 1;
        }
        g_standalone = standalone ? 1 : 0;
    }

    rc = skwoop_cfg_load_secrets(pt, sizeof pt, &pt_len, &sec_present);
    if (rc != 0) {
        clear_ram();
        wipe(ssid, sizeof ssid);
        wipe(pass, sizeof pass);
        wipe(base, sizeof base);
        wipe(pt, sizeof pt);
        return -1;
    }
    if (sec_present) {
        if (unpack_secrets(pt, pt_len) != 0) {
            clear_ram();
            wipe(ssid, sizeof ssid);
            wipe(pass, sizeof pass);
            wipe(base, sizeof base);
            wipe(pt, sizeof pt);
            return -1;
        }
    }

    rc = skwoop_cfg_load_base(base, sizeof base, &base_present);
    if (rc != 0) {
        clear_ram();
        wipe(ssid, sizeof ssid);
        wipe(pass, sizeof pass);
        wipe(base, sizeof base);
        wipe(pt, sizeof pt);
        return -1;
    }
    if (base_present) {
        memcpy(g_base, base, strlen(base) + 1);
        g_base_set = 1;
    }

    {
        char clip[SKWOOP_BASE_URL_MAX + 1];
        int clip_present = 0;
        memset(clip, 0, sizeof clip);
        rc = skwoop_cfg_load_clip_base(clip, sizeof clip, &clip_present);
        if (rc != 0) {
            clear_ram();
            wipe(ssid, sizeof ssid);
            wipe(pass, sizeof pass);
            wipe(base, sizeof base);
            wipe(clip, sizeof clip);
            wipe(pt, sizeof pt);
            return -1;
        }
        if (clip_present) {
            memcpy(g_clip, clip, strlen(clip) + 1);
            g_clip_set = 1;
        }
        wipe(clip, sizeof clip);
    }
    wipe(ssid, sizeof ssid);
    wipe(pass, sizeof pass);
    wipe(base, sizeof base);
    wipe(pt, sizeof pt);
    return 0;
}

int skwoop_config_save(void) {
    uint8_t pt[4 + SKWOOP_API_KEY_SLOTS * (1 + SKWOOP_PROVIDER_MAX + 2 + SKWOOP_API_KEY_MAX)];
    size_t pt_len = 0;
    const char *ssid = g_wifi_set ? g_ssid : "";
    const char *pass = g_wifi_set ? g_pass : "";
    int rc;

    if (pack_secrets(pt, sizeof pt, &pt_len) != 0) {
        wipe(pt, sizeof pt);
        return -1;
    }
    rc = skwoop_cfg_store_wifi(ssid, pass, g_standalone);
    if (rc != 0) {
        wipe(pt, sizeof pt);
        return -1;
    }
    rc = skwoop_cfg_store_secrets(pt, pt_len);
    if (rc != 0) {
        wipe(pt, sizeof pt);
        return -1;
    }
    rc = skwoop_cfg_store_base(g_base_set ? g_base : NULL);
    if (rc != 0) {
        wipe(pt, sizeof pt);
        return -1;
    }
    rc = skwoop_cfg_store_clip_base(g_clip_set ? g_clip : NULL);
    wipe(pt, sizeof pt);
    return rc == 0 ? 0 : -1;
}

int skwoop_config_set_wifi(const char *ssid, const char *pass) {
    size_t slen;
    size_t plen;
    if (ssid == NULL || pass == NULL) {
        return -1;
    }
    slen = strlen(ssid);
    plen = strlen(pass);
    if (slen == 0 || slen > SKWOOP_SSID_MAX || plen > SKWOOP_PASS_MAX) {
        return -1;
    }
    memcpy(g_ssid, ssid, slen + 1);
    memcpy(g_pass, pass, plen + 1);
    g_wifi_set = 1;
    return 0;
}

int skwoop_config_get_wifi(char *ssid, size_t ssid_cap, char *pass, size_t pass_cap) {
    if (!g_wifi_set) {
        return -1;
    }
    if (copy_out(g_ssid, ssid, ssid_cap) != 0) {
        return -1;
    }
    if (copy_out(g_pass, pass, pass_cap) != 0) {
        wipe(ssid, ssid_cap);
        return -1;
    }
    return 0;
}

int skwoop_config_set_api_key(const char *provider, const char *key) {
    size_t plen = 0;
    size_t klen;
    int slot;
    if (provider_ok(provider, &plen) != 0 || key == NULL) {
        return -1;
    }
    klen = strlen(key);
    if (klen == 0 || klen > SKWOOP_API_KEY_MAX) {
        return -1;
    }
    slot = find_slot(provider);
    if (slot < 0) {
        int i;
        for (i = 0; i < SKWOOP_API_KEY_SLOTS; i++) {
            if (!g_keys[i].used) {
                slot = i;
                break;
            }
        }
    }
    if (slot < 0) {
        return -1;
    }
    memcpy(g_keys[slot].provider, provider, plen + 1);
    memcpy(g_keys[slot].key, key, klen + 1);
    g_keys[slot].used = 1;
    return 0;
}

int skwoop_config_get_api_key(const char *provider, char *key, size_t key_cap) {
    int slot;
    size_t plen = 0;
    if (provider_ok(provider, &plen) != 0) {
        return -1;
    }
    slot = find_slot(provider);
    if (slot < 0) {
        return -1;
    }
    return copy_out(g_keys[slot].key, key, key_cap);
}

int skwoop_config_set_standalone(int on) {
    g_standalone = on ? 1 : 0;
    return 0;
}

int skwoop_config_get_standalone(void) {
    return g_standalone;
}

static int base_url_ok(const char *url, size_t *out_len) {
    size_t i;
    static const char prefix[] = "https://";
    if (url == NULL) {
        return -1;
    }
    for (i = 0; prefix[i] != '\0'; i++) {
        if (url[i] != prefix[i]) {
            return -1;
        }
    }
    if (url[i] == '\0' || url[i] == '/') {
        return -1;
    }
    for (; url[i] != '\0'; i++) {
        unsigned char c = (unsigned char)url[i];
        if (c <= 0x20 || c >= 0x7f) {
            return -1;
        }
        if (i >= SKWOOP_BASE_URL_MAX) {
            return -1;
        }
    }
    *out_len = i;
    return 0;
}

int skwoop_config_set_provider_base(const char *url) {
    size_t n = 0;
    if (base_url_ok(url, &n) != 0) {
        return -1;
    }
    memcpy(g_base, url, n + 1);
    g_base_set = 1;
    return 0;
}

int skwoop_config_get_provider_base(char *url, size_t url_cap) {
    if (!g_base_set) {
        return -1;
    }
    return copy_out(g_base, url, url_cap);
}

/* http:// or https:// with a non-empty host. Clip audio is not the provider URL. */
static int clip_base_ok(const char *url, size_t *out_len) {
    size_t i;
    size_t prefix;
    if (url == NULL) {
        return -1;
    }
    if (strncmp(url, "https://", 8) == 0) {
        prefix = 8;
    } else if (strncmp(url, "http://", 7) == 0) {
        prefix = 7;
    } else {
        return -1;
    }
    if (url[prefix] == '\0' || url[prefix] == '/' || url[prefix] == ':') {
        return -1;
    }
    for (i = prefix; url[i] != '\0'; i++) {
        unsigned char c = (unsigned char)url[i];
        if (c <= 0x20 || c >= 0x7f) {
            return -1;
        }
        if (i >= SKWOOP_BASE_URL_MAX) {
            return -1;
        }
    }
    *out_len = i;
    return 0;
}

int skwoop_config_set_clip_base(const char *url) {
    size_t n = 0;
    if (clip_base_ok(url, &n) != 0) {
        return -1;
    }
    memcpy(g_clip, url, n + 1);
    g_clip_set = 1;
    return 0;
}

int skwoop_config_get_clip_base(char *url, size_t url_cap) {
    if (!g_clip_set) {
        return -1;
    }
    return copy_out(g_clip, url, url_cap);
}
