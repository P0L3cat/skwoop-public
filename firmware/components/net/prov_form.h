#pragma once

#include <stddef.h>

/* Wi-Fi credentials taken from an application/x-www-form-urlencoded body.
 * ssid is 1..32 bytes. pass is empty (open network) or 8..64 bytes.
 * Both are NUL-terminated and the bytes after the NUL are zero. */
typedef struct {
    char ssid[33];
    char pass[65];
} prov_creds_t;

/* Parse ssid and pass. '+' is space and %XX is one byte. Other fields are ignored.
 * The last value of a repeated field is the one that is kept.
 *
 * Returns 0 on success.
 * On failure returns a negative errno-style code and leaves *out unchanged:
 *   -EINVAL   null argument, embedded NUL (raw or %00), or a field length out of range
 *   -ENOENT   ssid field absent
 *   -EILSEQ   '%' not followed by two hex digits
 *   -EMSGSIZE body longer than 256 bytes
 */
int prov_form_parse(const char *body, size_t len, prov_creds_t *out);
