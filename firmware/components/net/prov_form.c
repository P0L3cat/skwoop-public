#include "prov_form.h"

#include <errno.h>
#include <string.h>

/* Provisioning POST bodies are a single form. Anything larger is rejected
 * before a byte is examined so a huge len cannot walk off the buffer. */
#define PROV_FORM_MAX_BODY 256

static int hex_nibble(char c)
{
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

/* Decode one field token into dst. dst_cap includes the trailing NUL.
 * Writes *out_n as the decoded length, not counting that NUL. */
static int url_decode(const char *src, size_t src_len, char *dst, size_t dst_cap, size_t *out_n)
{
    size_t i = 0;
    size_t n = 0;

    if (dst_cap == 0) {
        return -EINVAL;
    }

    while (i < src_len) {
        int byte;

        if (src[i] == '%') {
            int hi;
            int lo;

            if (i + 2 >= src_len) {
                return -EILSEQ;
            }

            hi = hex_nibble(src[i + 1]);
            lo = hex_nibble(src[i + 2]);

            if (hi < 0 || lo < 0) {
                return -EILSEQ;
            }

            byte = (hi << 4) | lo;
            i += 3;
        } else if (src[i] == '+') {
            byte = ' ';
            i += 1;
        } else {
            byte = (unsigned char)src[i];
            i += 1;
        }

        if (byte == 0) {
            return -EINVAL;
        }

        if (n + 1 >= dst_cap) {
            return -EINVAL;
        }

        dst[n] = (char)byte;
        n += 1;
    }

    dst[n] = '\0';
    *out_n = n;

    return 0;
}

static int decode_field(const char *body, size_t begin, size_t end,
                        char *key, size_t *key_len, char *val, size_t *val_len)
{
    size_t split = begin;
    int has_eq = 0;
    int rc;
    const size_t cap = PROV_FORM_MAX_BODY + 1;

    while (split < end) {
        if (body[split] == '=') {
            has_eq = 1;
            break;
        }

        split += 1;
    }

    if (!has_eq) {
        val[0] = '\0';
        *val_len = 0;

        return url_decode(body + begin, end - begin, key, cap, key_len);
    }

    rc = url_decode(body + begin, split - begin, key, cap, key_len);

    if (rc != 0) {
        return rc;
    }

    return url_decode(body + split + 1, end - (split + 1), val, cap, val_len);
}

static void keep_cred(const char *key, size_t key_len, const char *val, size_t val_len,
                      char *ssid, size_t *ssid_len, int *saw_ssid,
                      char *pass, size_t *pass_len)
{
    if (key_len == 4 && memcmp(key, "ssid", 4) == 0) {
        memcpy(ssid, val, val_len + 1);
        *ssid_len = val_len;
        *saw_ssid = 1;

        return;
    }

    if (key_len == 4 && memcmp(key, "pass", 4) == 0) {
        memcpy(pass, val, val_len + 1);
        *pass_len = val_len;
    }
}

int prov_form_parse(const char *body, size_t len, prov_creds_t *out)
{
    char key[PROV_FORM_MAX_BODY + 1];
    char val[PROV_FORM_MAX_BODY + 1];
    char ssid[PROV_FORM_MAX_BODY + 1];
    char pass[PROV_FORM_MAX_BODY + 1];
    size_t pos = 0;
    size_t ssid_len = 0;
    size_t pass_len = 0;
    int saw_ssid = 0;

    if (body == NULL || out == NULL) {
        return -EINVAL;
    }

    if (len > PROV_FORM_MAX_BODY) {
        return -EMSGSIZE;
    }

    ssid[0] = '\0';
    pass[0] = '\0';

    while (pos < len) {
        size_t begin = pos;
        size_t end;
        size_t key_len = 0;
        size_t val_len = 0;
        int rc;

        if (body[pos] == '\0') {
            return -EINVAL;
        }

        while (pos < len && body[pos] != '&') {
            if (body[pos] == '\0') {
                return -EINVAL;
            }

            pos += 1;
        }

        end = pos;

        if (pos < len) {
            pos += 1;
        }

        if (begin == end) {
            continue;
        }

        rc = decode_field(body, begin, end, key, &key_len, val, &val_len);

        if (rc != 0) {
            return rc;
        }

        keep_cred(key, key_len, val, val_len, ssid, &ssid_len, &saw_ssid, pass, &pass_len);
    }

    if (!saw_ssid) {
        return -ENOENT;
    }

    if (ssid_len < 1 || ssid_len > 32) {
        return -EINVAL;
    }

    if (pass_len > 64 || (pass_len > 0 && pass_len < 8)) {
        return -EINVAL;
    }

    memset(out->ssid, 0, sizeof(out->ssid));
    memset(out->pass, 0, sizeof(out->pass));
    memcpy(out->ssid, ssid, ssid_len);
    memcpy(out->pass, pass, pass_len);

    return 0;
}
