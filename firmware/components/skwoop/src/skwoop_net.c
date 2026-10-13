#include "skwoop_net.h"

#include "skwoop_config.h"

#include <errno.h>
#include <stdio.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/time.h>

#include <openssl/bio.h>
#include <openssl/ssl.h>

/* Host TLS GET uses OpenSSL BIO. Device WSS will use esp_websocket_client. */

static int parse_https_url(const char *url, char *host, size_t host_cap, char *port,
                           size_t port_cap, char *path, size_t path_cap) {
    const char *p;
    const char *host_end;
    const char *slash;
    const char *colon;
    size_t host_len;
    size_t path_len;

    if (url == NULL || strncmp(url, "https://", 8) != 0) {
        return -EINVAL;
    }
    p = url + 8;
    if (*p == '\0' || *p == '/' || *p == ':') {
        return -EINVAL;
    }
    slash = strchr(p, '/');
    host_end = slash != NULL ? slash : p + strlen(p);
    colon = memchr(p, ':', (size_t)(host_end - p));
    if (colon != NULL) {
        host_len = (size_t)(colon - p);
        if (host_len == 0 || (size_t)(host_end - colon) <= 1) {
            return -EINVAL;
        }
        if ((size_t)(host_end - (colon + 1)) >= port_cap) {
            return -EINVAL;
        }
        memcpy(port, colon + 1, (size_t)(host_end - (colon + 1)));
        port[host_end - (colon + 1)] = '\0';
    } else {
        host_len = (size_t)(host_end - p);
        if (port_cap < 4) {
            return -EINVAL;
        }
        memcpy(port, "443", 4);
    }
    if (host_len == 0 || host_len >= host_cap) {
        return -EINVAL;
    }
    memcpy(host, p, host_len);
    host[host_len] = '\0';
    if (slash == NULL) {
        if (path_cap < 2) {
            return -EINVAL;
        }
        memcpy(path, "/", 2);
        return 0;
    }
    path_len = strlen(slash);
    if (path_len >= path_cap) {
        return -EINVAL;
    }
    memcpy(path, slash, path_len + 1);
    return 0;
}

static int bio_write_all(BIO *bio, const char *data, int len) {
    int off = 0;
    int spins = 0;
    while (off < len) {
        int n = BIO_write(bio, data + off, len - off);
        if (n > 0) {
            off += n;
            spins = 0;
            continue;
        }
        if (BIO_should_retry(bio) && spins < 50) {
            spins++;
            continue;
        }
        return -1;
    }
    return 0;
}

static int parse_response(const unsigned char *raw, size_t n, int *status, size_t *body_off) {
    const unsigned char *sep;
    const unsigned char *line;
    const unsigned char *sp;
    int code = 0;
    int digits = 0;

    if (n < 12 || memcmp(raw, "HTTP/1.", 7) != 0) {
        return -EBADMSG;
    }
    sep = (const unsigned char *)memmem(raw, n, "\r\n\r\n", 4);
    if (sep == NULL) {
        return -EBADMSG;
    }
    line = raw;
    sp = memchr(line, ' ', (size_t)(sep - line));
    if (sp == NULL || (size_t)(sep - sp) < 5) {
        return -EBADMSG;
    }
    sp++;
    while (digits < 3 && sp + digits < sep && sp[digits] >= '0' && sp[digits] <= '9') {
        code = code * 10 + (sp[digits] - '0');
        digits++;
    }
    if (digits != 3) {
        return -EBADMSG;
    }
    *status = code;
    *body_off = (size_t)(sep - raw) + 4;
    return 0;
}

static void load_trust(SSL_CTX *ctx) {
    /* Default OpenSSL dir plus SSL_CERT_FILE. /etc/ssl/cert.pem covers common Linux layouts. */
    SSL_CTX_set_default_verify_paths(ctx);
    SSL_CTX_load_verify_locations(ctx, "/etc/ssl/cert.pem", NULL);
}

int skwoop_http_get(const char *url, char *buf, size_t buflen, size_t *out_n) {
    char host[256];
    char port[8];
    char path[1024];
    char conn[300];
    char req[1400];
    unsigned char raw[65536];
    size_t got = 0;
    size_t body_off = 0;
    size_t body_len;
    size_t copy;
    int status = 0;
    int req_len;
    int fd = -1;
    SSL_CTX *ctx = NULL;
    SSL *ssl = NULL;
    BIO *bio = NULL;
    struct timeval tv;
    int rc = -EIO;

    if (out_n != NULL) {
        *out_n = 0;
    }
    if (url == NULL || buf == NULL || buflen < 2 || out_n == NULL) {
        return -EINVAL;
    }
    buf[0] = '\0';
    if (parse_https_url(url, host, sizeof host, port, sizeof port, path, sizeof path) != 0) {
        return -EINVAL;
    }
    if (snprintf(conn, sizeof conn, "%s:%s", host, port) >= (int)sizeof conn) {
        return -EINVAL;
    }
    req_len = snprintf(req, sizeof req,
                       "GET %s HTTP/1.1\r\n"
                       "Host: %s\r\n"
                       "User-Agent: skwoop-net/0\r\n"
                       "Accept: */*\r\n"
                       "Connection: close\r\n"
                       "\r\n",
                       path, host);
    if (req_len < 0 || req_len >= (int)sizeof req) {
        return -EINVAL;
    }

    ctx = SSL_CTX_new(TLS_client_method());
    if (ctx == NULL) {
        return -EIO;
    }
    SSL_CTX_set_verify(ctx, SSL_VERIFY_PEER, NULL);
    load_trust(ctx);
    bio = BIO_new_ssl_connect(ctx);
    if (bio == NULL) {
        SSL_CTX_free(ctx);
        return -EIO;
    }
    BIO_get_ssl(bio, &ssl);
    if (ssl == NULL) {
        BIO_free_all(bio);
        SSL_CTX_free(ctx);
        return -EIO;
    }
    SSL_set_tlsext_host_name(ssl, host);
    SSL_set1_host(ssl, host);
    SSL_set_mode(ssl, SSL_MODE_AUTO_RETRY);
    BIO_set_conn_hostname(bio, conn);
    if (BIO_do_connect(bio) <= 0) {
        rc = -EIO;
        goto done;
    }
    if (BIO_get_fd(bio, &fd) >= 0 && fd >= 0) {
        tv.tv_sec = 10;
        tv.tv_usec = 0;
        setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof tv);
        setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof tv);
    }
    if (bio_write_all(bio, req, req_len) != 0) {
        rc = -EIO;
        goto done;
    }
    {
        int spins = 0;
        while (got < sizeof raw) {
            int n = BIO_read(bio, raw + got, (int)(sizeof raw - got));
            if (n > 0) {
                got += (size_t)n;
                spins = 0;
                continue;
            }
            if (BIO_should_retry(bio) && spins < 50) {
                spins++;
                continue;
            }
            break;
        }
    }
    if (parse_response(raw, got, &status, &body_off) != 0 || body_off > got) {
        rc = -EBADMSG;
        goto done;
    }
    body_len = got - body_off;
    copy = body_len;
    if (copy >= buflen) {
        copy = buflen - 1;
    }
    if (copy > 0) {
        memcpy(buf, raw + body_off, copy);
    }
    buf[copy] = '\0';
    *out_n = copy;
    rc = status;

done:
    BIO_free_all(bio);
    SSL_CTX_free(ctx);
    return rc;
}

int skwoop_wss_connect(const char *url) {
    (void)url;
    /*
     * Host stub. ESP-IDF esp_websocket_client will own the device WSS
     * handshake later. This build does not open a WebSocket.
     */
    return -ENOSYS;
}

int skwoop_net_provider_base(char *buf, size_t buflen) {
    int rc;
    if (buf == NULL || buflen == 0) {
        return -EINVAL;
    }
    buf[0] = '\0';
    rc = skwoop_config_get_provider_base(buf, buflen);
    if (rc != 0) {
        buf[0] = '\0';
        /* Config has no provider base URL (missing file or never set). */
        return -ENOENT;
    }
    return 0;
}
