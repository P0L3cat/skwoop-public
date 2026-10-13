#include "skwoop_clips.h"

#include "skwoop_config.h"

#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>

#include <arpa/inet.h>
#include <netdb.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <unistd.h>

/* Version-1 clips.idx. See harness-stt base/docs/clips-idx.md.
 * The pod keeps the index long enough to resolve one phrase. Wav bytes
 * stay on the clip host. */

#define SKWOOP_IDX_MAX (8u * 1024u * 1024u)
#define KIND_N 13

struct skwoop_clip_index {
    uint8_t *bytes;
    size_t len;
    uint32_t off[KIND_N + 1];
    uint32_t count[KIND_N + 1];
    uint32_t rec[KIND_N + 1];
};

static struct skwoop_clip_index *g_live;

static const uint32_t k_rec[KIND_N + 1] = {
    0, 1, 16, 16, 16, 16, 16, 16, 16, 16, 16, 20, 1, 1};

static uint32_t rd_u32(const uint8_t *p) {
    return (uint32_t)p[0] | ((uint32_t)p[1] << 8) | ((uint32_t)p[2] << 16) |
           ((uint32_t)p[3] << 24);
}

static int span_ok(size_t len, uint32_t off, uint32_t count, uint32_t rec) {
    uint64_t nbytes;
    if ((off & 3u) != 0 || (size_t)off > len || rec == 0) {
        return 0;
    }
    nbytes = (uint64_t)count * (uint64_t)rec;
    if (nbytes > len - (size_t)off) {
        return 0;
    }
    return 1;
}

static int ref_ok(const struct skwoop_clip_index *idx, uint32_t off, uint32_t nlen) {
    uint32_t pool;
    if ((off & 3u) != 0) {
        return 0;
    }
    pool = idx->count[1];
    if ((uint64_t)off + (uint64_t)nlen > pool) {
        return 0;
    }
    return 1;
}

static int range_ok(const struct skwoop_clip_index *idx, int target, uint32_t start,
                    uint32_t count) {
    if ((uint64_t)start + (uint64_t)count > idx->count[target]) {
        return 0;
    }
    return 1;
}

static int validate(struct skwoop_clip_index *idx) {
    const uint8_t *b = idx->bytes;
    uint32_t i;
    uint32_t seen = 0;
    int k;
    static const int k_refs[KIND_N + 1] = {0, 0, 1, 2, 1, 2, 2, 2, 2, 1, 2, 2, 0, 0};
    static const int k_range[KIND_N + 1] = {0, 0, 3, 0, 5, 0, 0, 0, 0, 10, 0, 0, 0, 0};

    if (idx->len < 52 || idx->len > SKWOOP_IDX_MAX) {
        return -EBADMSG;
    }
    if (memcmp(b, "SKIDX\0\0\0", 8) != 0 || rd_u32(b + 8) != 1 || rd_u32(b + 12) != 0 ||
        rd_u32(b + 48) != KIND_N) {
        return -EBADMSG;
    }
    if (idx->len < 52u + KIND_N * 16u) {
        return -EBADMSG;
    }
    for (i = 0; i < KIND_N; i++) {
        const uint8_t *e = b + 52 + i * 16;
        uint32_t kind = rd_u32(e);
        uint32_t off = rd_u32(e + 4);
        uint32_t count = rd_u32(e + 8);
        uint32_t rec = rd_u32(e + 12);
        if (kind < 1 || kind > KIND_N || (seen & (1u << kind)) != 0 || rec != k_rec[kind] ||
            !span_ok(idx->len, off, count, rec)) {
            return -EBADMSG;
        }
        idx->off[kind] = off;
        idx->count[kind] = count;
        idx->rec[kind] = rec;
        seen |= 1u << kind;
    }
    for (k = 2; k <= 11; k++) {
        uint32_t n;
        for (n = 0; n < idx->count[k]; n++) {
            const uint8_t *recp = b + idx->off[k] + (size_t)n * idx->rec[k];
            int r;
            for (r = 0; r < k_refs[k]; r++) {
                if (!ref_ok(idx, rd_u32(recp + (size_t)r * 8), rd_u32(recp + (size_t)r * 8 + 4))) {
                    return -EBADMSG;
                }
            }
            if (k_range[k] != 0 &&
                !range_ok(idx, k_range[k], rd_u32(recp + 8), rd_u32(recp + 12))) {
                return -EBADMSG;
            }
        }
    }
    return 0;
}

static int adopt(struct skwoop_clip_index *idx) {
    int rc = validate(idx);
    if (rc != 0) {
        free(idx->bytes);
        free(idx);
        return rc;
    }
    g_live = idx;
    return 0;
}

int skwoop_clips_load_mem(const void *data, size_t len, skwoop_clip_index **out) {
    struct skwoop_clip_index *idx;
    if (out == NULL || (data == NULL && len != 0)) {
        return -EINVAL;
    }
    *out = NULL;
    idx = calloc(1, sizeof *idx);
    if (idx == NULL) {
        return -ENOMEM;
    }
    idx->bytes = malloc(len > 0 ? len : 1);
    if (idx->bytes == NULL) {
        free(idx);
        return -ENOMEM;
    }
    if (len > 0) {
        memcpy(idx->bytes, data, len);
    }
    idx->len = len;
    if (adopt(idx) != 0) {
        return -EBADMSG;
    }
    *out = idx;
    return 0;
}

int skwoop_clips_load_file(const char *path, skwoop_clip_index **out) {
    FILE *f;
    long sz;
    uint8_t *buf;
    size_t n;
    int rc;
    if (out == NULL) {
        return -EINVAL;
    }
    *out = NULL;
    if (path == NULL) {
        return -EINVAL;
    }
    f = fopen(path, "rb");
    if (f == NULL) {
        return errno == ENOENT ? -ENOENT : -EIO;
    }
    if (fseek(f, 0, SEEK_END) != 0 || (sz = ftell(f)) < 0 || fseek(f, 0, SEEK_SET) != 0) {
        fclose(f);
        return -EIO;
    }
    if ((unsigned long)sz > SKWOOP_IDX_MAX) {
        fclose(f);
        return -EBADMSG;
    }
    buf = malloc((size_t)sz > 0 ? (size_t)sz : 1);
    if (buf == NULL) {
        fclose(f);
        return -ENOMEM;
    }
    n = fread(buf, 1, (size_t)sz, f);
    fclose(f);
    if (n != (size_t)sz) {
        free(buf);
        return -EIO;
    }
    rc = skwoop_clips_load_mem(buf, n, out);
    free(buf);
    return rc;
}

void skwoop_clips_free(skwoop_clip_index *idx) {
    if (idx == NULL) {
        return;
    }
    if (g_live == idx) {
        g_live = NULL;
    }
    free(idx->bytes);
    free(idx);
}

static const uint8_t *pool_bytes(const struct skwoop_clip_index *idx, uint32_t off) {
    return idx->bytes + idx->off[1] + off;
}

static int key_cmp(const uint8_t *key, uint32_t klen, const char *probe, size_t plen) {
    size_t n = klen < plen ? klen : plen;
    int c = memcmp(key, probe, n);
    if (c != 0) {
        return c;
    }
    if (klen < plen) {
        return -1;
    }
    if (klen > plen) {
        return 1;
    }
    return 0;
}

/* Bytewise binary search. Record key is the first string ref. Returns record index or -1. */
static int bsearch_key(const struct skwoop_clip_index *idx, int kind, const char *probe) {
    uint32_t lo = 0;
    uint32_t hi = idx->count[kind];
    size_t plen = strlen(probe);
    while (lo < hi) {
        uint32_t mid = lo + (hi - lo) / 2;
        const uint8_t *rec =
            idx->bytes + idx->off[kind] + (size_t)mid * idx->rec[kind];
        uint32_t off = rd_u32(rec);
        uint32_t nlen = rd_u32(rec + 4);
        int c = key_cmp(pool_bytes(idx, off), nlen, probe, plen);
        if (c < 0) {
            lo = mid + 1;
        } else if (c > 0) {
            hi = mid;
        } else {
            return (int)mid;
        }
    }
    return -1;
}

static int copy_ref(const struct skwoop_clip_index *idx, uint32_t off, uint32_t nlen, char *dst,
                    size_t cap) {
    const uint8_t *s;
    if (nlen >= cap || memchr(pool_bytes(idx, off), '\0', nlen) != NULL) {
        return -1;
    }
    s = pool_bytes(idx, off);
    memcpy(dst, s, nlen);
    dst[nlen] = '\0';
    return 0;
}

static int norm_text(const char *text, char *out, size_t cap) {
    size_t o = 0;
    int gap = 1;
    const unsigned char *p = (const unsigned char *)text;
    while (*p != 0) {
        unsigned char c = *p++;
        if (c == 0xe2 && p[0] == 0x80 && (p[1] == 0x98 || p[1] == 0x99)) {
            c = '\'';
            p += 2;
        }
        if (c >= 'A' && c <= 'Z') {
            c = (unsigned char)(c - 'A' + 'a');
        }
        if (c == '-') {
            c = ' ';
        }
        if (c == ' ' || c == '\t' || c == '\n' || c == '\r') {
            if (gap || o + 1 >= cap) {
                if (!gap && o + 1 >= cap) {
                    return -1;
                }
                continue;
            }
            out[o++] = ' ';
            gap = 1;
            continue;
        }
        if (!((c >= 'a' && c <= 'z') || (c >= '0' && c <= '9') || c == '\'')) {
            continue;
        }
        if (o + 1 >= cap) {
            return -1;
        }
        out[o++] = (char)c;
        gap = 0;
    }
    if (o > 0 && out[o - 1] == ' ') {
        o--;
    }
    out[o] = '\0';
    return 0;
}

static int mood_rank(const char *mood) {
    static const char *order[] = {"idle", "happy", "calm", "alert", "sad"};
    int i;
    for (i = 0; i < 5; i++) {
        if (strcmp(mood, order[i]) == 0) {
            return i;
        }
    }
    return 100;
}

static int path_ok(const char *path) {
    const char *p;
    if (path == NULL || path[0] == '\0' || path[0] == '/' || path[0] == '\\') {
        return 0;
    }
    for (p = path; *p != '\0'; p++) {
        unsigned char c = (unsigned char)*p;
        int ok = (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || (c >= '0' && c <= '9') ||
                 c == '.' || c == '_' || c == '-' || c == '/';
        if (!ok) {
            return 0;
        }
    }
    if (strstr(path, "..") != NULL || strstr(path, "//") != NULL) {
        return 0;
    }
    return 1;
}

int skwoop_clips_resolve(const skwoop_clip_index *idx, const char *text, skwoop_clip *out) {
    char key[SKWOOP_CLIP_TEXT_MAX];
    int whole;
    int best = -1;
    int best_rank = 1000;
    uint32_t start;
    uint32_t count;
    uint32_t i;
    char id[SKWOOP_CLIP_ID_MAX];
    int clip_i;
    const uint8_t *clip;
    if (idx == NULL || text == NULL || out == NULL) {
        return -EINVAL;
    }
    memset(out, 0, sizeof *out);
    if (norm_text(text, key, sizeof key) != 0) {
        return -EINVAL;
    }
    if (key[0] == '\0') {
        return 1;
    }
    whole = bsearch_key(idx, 2, key);
    if (whole < 0) {
        return 1;
    }
    {
        const uint8_t *rec = idx->bytes + idx->off[2] + (size_t)whole * 16u;
        start = rd_u32(rec + 8);
        count = rd_u32(rec + 12);
    }
    if (count == 0) {
        return 1;
    }
    for (i = 0; i < count; i++) {
        const uint8_t *pair = idx->bytes + idx->off[3] + (size_t)(start + i) * 16u;
        char mood[32];
        int rank;
        if (copy_ref(idx, rd_u32(pair), rd_u32(pair + 4), mood, sizeof mood) != 0) {
            return -EBADMSG;
        }
        rank = mood_rank(mood);
        if (best < 0 || rank < best_rank) {
            best = (int)i;
            best_rank = rank;
            if (rank == 0) {
                break;
            }
        }
    }
    {
        const uint8_t *pair =
            idx->bytes + idx->off[3] + (size_t)(start + (uint32_t)best) * 16u;
        if (copy_ref(idx, rd_u32(pair + 8), rd_u32(pair + 12), id, sizeof id) != 0) {
            return -EBADMSG;
        }
    }
    clip_i = bsearch_key(idx, 11, id);
    if (clip_i < 0) {
        return -EBADMSG;
    }
    clip = idx->bytes + idx->off[11] + (size_t)clip_i * 20u;
    if (copy_ref(idx, rd_u32(clip), rd_u32(clip + 4), out->id, sizeof out->id) != 0 ||
        copy_ref(idx, rd_u32(clip + 8), rd_u32(clip + 12), out->path, sizeof out->path) != 0 ||
        !path_ok(out->path)) {
        memset(out, 0, sizeof *out);
        return -EBADMSG;
    }
    memcpy(out->text, key, strlen(key) + 1);
    return 0;
}

static int http_get_file(const char *host, const char *port, const char *req_path, const char *out_path,
                         uint32_t expect_n) {
    char req[1400];
    unsigned char hdr[4096];
    size_t hdr_n = 0;
    size_t body_have = 0;
    const unsigned char *sep = NULL;
    int req_len;
    int fd = -1;
    int status = 0;
    FILE *out = NULL;
    struct addrinfo hints;
    struct addrinfo *res = NULL;
    struct timeval tv;
    int rc = -EIO;
    uint32_t written = 0;
    int saw_len = 0;
    unsigned long content_len = 0;

    req_len = snprintf(req, sizeof req,
                       "GET %s HTTP/1.0\r\n"
                       "Host: %s\r\n"
                       "User-Agent: skwoop-clips/0\r\n"
                       "Accept: */*\r\n"
                       "Connection: close\r\n"
                       "\r\n",
                       req_path, host);
    if (req_len < 0 || req_len >= (int)sizeof req) {
        return -EINVAL;
    }
    memset(&hints, 0, sizeof hints);
    hints.ai_family = AF_UNSPEC;
    hints.ai_socktype = SOCK_STREAM;
    if (getaddrinfo(host, port, &hints, &res) != 0) {
        return -EIO;
    }
    fd = socket(res->ai_family, res->ai_socktype, res->ai_protocol);
    if (fd < 0) {
        freeaddrinfo(res);
        return -errno;
    }
    tv.tv_sec = 5;
    tv.tv_usec = 0;
    setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof tv);
    setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof tv);
    if (connect(fd, res->ai_addr, res->ai_addrlen) != 0) {
        rc = -errno;
        freeaddrinfo(res);
        close(fd);
        return rc;
    }
    freeaddrinfo(res);
    if (send(fd, req, (size_t)req_len, 0) != req_len) {
        close(fd);
        return -EIO;
    }
    while (sep == NULL && hdr_n < sizeof hdr) {
        ssize_t n = recv(fd, hdr + hdr_n, sizeof hdr - hdr_n, 0);
        if (n <= 0) {
            break;
        }
        hdr_n += (size_t)n;
        sep = memmem(hdr, hdr_n, "\r\n\r\n", 4);
    }
    if (sep == NULL || hdr_n < 12 || memcmp(hdr, "HTTP/1.", 7) != 0) {
        close(fd);
        return -EBADMSG;
    }
    {
        const unsigned char *sp = memchr(hdr, ' ', (size_t)(sep - hdr));
        int digits = 0;
        if (sp == NULL) {
            close(fd);
            return -EBADMSG;
        }
        sp++;
        while (digits < 3 && sp + digits < sep && sp[digits] >= '0' && sp[digits] <= '9') {
            status = status * 10 + (sp[digits] - '0');
            digits++;
        }
        if (digits != 3 || status != 200) {
            close(fd);
            return -EBADMSG;
        }
    }
    {
        const char *cl = NULL;
        size_t hlen = (size_t)(sep - hdr);
        size_t i;
        for (i = 0; i + 15 < hlen; i++) {
            if ((hdr[i] == 'C' || hdr[i] == 'c') && strncasecmp((const char *)hdr + i, "content-length:", 15) == 0) {
                cl = (const char *)hdr + i + 15;
                break;
            }
        }
        if (cl != NULL) {
            char *end = NULL;
            while (*cl == ' ') {
                cl++;
            }
            content_len = strtoul(cl, &end, 10);
            if (end == cl) {
                close(fd);
                return -EBADMSG;
            }
            saw_len = 1;
            if (content_len != expect_n) {
                close(fd);
                return -EBADMSG;
            }
        }
    }
    out = fopen(out_path, "wb");
    if (out == NULL) {
        close(fd);
        return -EIO;
    }
    body_have = hdr_n - (size_t)(sep + 4 - hdr);
    if (body_have > 0) {
        uint32_t take = (uint32_t)body_have;
        if (saw_len && take > expect_n) {
            take = expect_n;
        }
        if (fwrite(sep + 4, 1, take, out) != take) {
            fclose(out);
            unlink(out_path);
            close(fd);
            return -EIO;
        }
        written = take;
    }
    while (!saw_len || written < expect_n) {
        unsigned char buf[4096];
        ssize_t n = recv(fd, buf, sizeof buf, 0);
        size_t take;
        if (n < 0) {
            rc = -EIO;
            goto fail;
        }
        if (n == 0) {
            break;
        }
        take = (size_t)n;
        if (saw_len && written + take > expect_n) {
            take = expect_n - written;
        }
        if (take > 0 && fwrite(buf, 1, take, out) != take) {
            rc = -EIO;
            goto fail;
        }
        written += (uint32_t)take;
        if (saw_len && written >= expect_n) {
            break;
        }
    }
    if (written != expect_n) {
        rc = -EBADMSG;
        goto fail;
    }
    if (fclose(out) != 0) {
        unlink(out_path);
        close(fd);
        return -EIO;
    }
    close(fd);
    return 0;

fail:
    fclose(out);
    unlink(out_path);
    close(fd);
    return rc;
}

int skwoop_clips_fetch(const char *id, const char *out_path) {
    char base[SKWOOP_BASE_URL_MAX + 1];
    char host[256];
    char port[8];
    char req_path[512];
    const char *p;
    const char *slash;
    const char *colon;
    const char *host_end;
    size_t host_len;
    int clip_i;
    const uint8_t *rec;
    char rel[SKWOOP_CLIP_PATH_MAX];
    uint32_t nbytes;
    const char *prefix;
    size_t prefix_len;

    if (id == NULL || id[0] == '\0' || out_path == NULL || out_path[0] == '\0' || g_live == NULL) {
        return -EINVAL;
    }
    clip_i = bsearch_key(g_live, 11, id);
    if (clip_i < 0) {
        return -ENOENT;
    }
    rec = g_live->bytes + g_live->off[11] + (size_t)clip_i * 20u;
    if (copy_ref(g_live, rd_u32(rec + 8), rd_u32(rec + 12), rel, sizeof rel) != 0 || !path_ok(rel)) {
        return -EBADMSG;
    }
    nbytes = rd_u32(rec + 16);
    if (skwoop_config_get_clip_base(base, sizeof base) != 0) {
        return -ENOENT;
    }
    if (strncmp(base, "http://", 7) != 0) {
        return -EPROTONOSUPPORT;
    }
    p = base + 7;
    slash = strchr(p, '/');
    host_end = slash != NULL ? slash : p + strlen(p);
    colon = memchr(p, ':', (size_t)(host_end - p));
    if (colon != NULL) {
        host_len = (size_t)(colon - p);
        if (host_len == 0 || host_len >= sizeof host || (size_t)(host_end - colon) <= 1 ||
            (size_t)(host_end - colon) > sizeof port) {
            return -EINVAL;
        }
        memcpy(port, colon + 1, (size_t)(host_end - (colon + 1)));
        port[host_end - (colon + 1)] = '\0';
    } else {
        host_len = (size_t)(host_end - p);
        if (host_len == 0 || host_len >= sizeof host) {
            return -EINVAL;
        }
        memcpy(port, "80", 3);
    }
    memcpy(host, p, host_len);
    host[host_len] = '\0';
    prefix = slash != NULL ? slash : "";
    prefix_len = slash != NULL ? strlen(slash) : 0;
    while (prefix_len > 0 && prefix[prefix_len - 1] == '/') {
        prefix_len--;
    }
    if (snprintf(req_path, sizeof req_path, "%.*s/%s", (int)prefix_len, prefix, rel) >=
        (int)sizeof req_path) {
        return -EINVAL;
    }
    if (req_path[0] != '/') {
        return -EINVAL;
    }
    return http_get_file(host, port, req_path, out_path, nbytes);
}
