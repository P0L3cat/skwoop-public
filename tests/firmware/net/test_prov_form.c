/* Host check for the provisioning form parser. No ESP-IDF.
 * cc -Wall -Wextra -Werror -I../../../firmware/components/net ../../../firmware/components/net/prov_form.c test_prov_form.c
 */

#include "prov_form.h"

#include <assert.h>
#include <errno.h>
#include <string.h>

static void expect_ok(const char *body, const char *ssid, const char *pass)
{
    prov_creds_t creds;
    size_t i;
    int rc;

    memset(&creds, 0x5a, sizeof(creds));
    rc = prov_form_parse(body, strlen(body), &creds);

    assert(rc == 0);
    assert(strcmp(creds.ssid, ssid) == 0);
    assert(strcmp(creds.pass, pass) == 0);

    for (i = strlen(ssid) + 1; i < sizeof(creds.ssid); i++) {
        assert(creds.ssid[i] == '\0');
    }

    for (i = strlen(pass) + 1; i < sizeof(creds.pass); i++) {
        assert(creds.pass[i] == '\0');
    }
}

static void expect_err(const void *body, size_t len, int err)
{
    prov_creds_t creds;
    prov_creds_t saved;
    int rc;

    memset(&creds, 0x5a, sizeof(creds));
    saved = creds;
    rc = prov_form_parse(body, len, &creds);

    assert(rc == err);
    assert(memcmp(&creds, &saved, sizeof(creds)) == 0);
}

static void test_happy_and_order(void)
{
    expect_ok("ssid=Home&pass=secret12", "Home", "secret12");
    expect_ok("pass=secret12&ssid=Home", "Home", "secret12");
    expect_ok("foo=1&ssid=Home&bar=2&pass=secret12", "Home", "secret12");
    expect_ok("ssid=No&ssid=Yes&pass=short&pass=12345678", "Yes", "12345678");
    expect_ok("ssid=Home&&pass=12345678&", "Home", "12345678");
}

static void test_url_decode(void)
{
    expect_ok("ssid=My+Net%20X&pass=p%40ssw0rd", "My Net X", "p@ssw0rd");
    expect_ok("ssid=a%2Bb&pass=%31%32%33%34%35%36%37%38", "a+b", "12345678");
    expect_ok("ssid=%6a%4A&pass=12345678", "jJ", "12345678");
    expect_ok("ssid=Home&pass=12345%36%37%38", "Home", "12345678");
}

static void test_open_network(void)
{
    expect_ok("ssid=Cafe&pass=", "Cafe", "");
    expect_ok("ssid=Cafe", "Cafe", "");
    expect_ok("ssid=A&pass=12345678", "A", "12345678");
}

static void test_length_bounds(void)
{
    char ssid32[64];
    char ssid_enc[64];
    char pass64[96];
    char body256[256];
    char name32[33];
    char name_enc[33];
    char secret64[65];
    prov_creds_t creds;
    size_t used = 0;
    int rc;

    memset(name32, 'S', 32);
    name32[32] = '\0';
    memcpy(ssid32, "ssid=", 5);
    memset(ssid32 + 5, 'S', 32);
    memcpy(ssid32 + 37, "&pass=12345678", 15);
    expect_ok(ssid32, name32, "12345678");

    /* 31 raw bytes plus one %XX is 32 decoded bytes, not 34. */
    memset(name_enc, 'S', 31);
    name_enc[31] = 'A';
    name_enc[32] = '\0';
    memcpy(ssid_enc, "ssid=", 5);
    memset(ssid_enc + 5, 'S', 31);
    memcpy(ssid_enc + 36, "%41&pass=12345678", 18);
    expect_ok(ssid_enc, name_enc, "12345678");

    memset(secret64, 'P', 64);
    secret64[64] = '\0';
    memcpy(pass64, "ssid=N&pass=", 12);
    memset(pass64 + 12, 'P', 64);
    pass64[76] = '\0';
    expect_ok(pass64, "N", secret64);

    memcpy(body256, "ssid=N&pass=12345678", 20);
    used = 20;

    while (used < sizeof(body256)) {
        memcpy(body256 + used, "&x=y", 4);
        used += 4;
    }

    assert(used == 256);
    memset(&creds, 0x5a, sizeof(creds));
    rc = prov_form_parse(body256, 256, &creds);
    assert(rc == 0);
    assert(strcmp(creds.ssid, "N") == 0);
    assert(strcmp(creds.pass, "12345678") == 0);
}

static void test_reject_shape(void)
{
    char ssid33[80];
    char ssid_enc33[80];
    char pass65[96];
    char huge[257];

    expect_err("pass=12345678", strlen("pass=12345678"), -ENOENT);
    expect_err("", 0, -ENOENT);
    expect_err("foo=bar&x=y", strlen("foo=bar&x=y"), -ENOENT);

    expect_err("ssid=&pass=12345678", strlen("ssid=&pass=12345678"), -EINVAL);
    expect_err("ssid&pass=12345678", strlen("ssid&pass=12345678"), -EINVAL);

    memcpy(ssid33, "ssid=", 5);
    memset(ssid33 + 5, 'S', 33);
    memcpy(ssid33 + 38, "&pass=12345678", 15);
    expect_err(ssid33, strlen(ssid33), -EINVAL);

    memcpy(ssid_enc33, "ssid=", 5);
    memset(ssid_enc33 + 5, 'S', 32);
    memcpy(ssid_enc33 + 37, "%41&pass=12345678", 18);
    expect_err(ssid_enc33, strlen(ssid_enc33), -EINVAL);

    expect_err("ssid=Home&pass=123456%37", strlen("ssid=Home&pass=123456%37"), -EINVAL);
    expect_err("ssid=Home&pass=1234567", strlen("ssid=Home&pass=1234567"), -EINVAL);

    memcpy(pass65, "ssid=N&pass=", 12);
    memset(pass65 + 12, 'P', 65);
    pass65[77] = '\0';
    expect_err(pass65, strlen(pass65), -EINVAL);

    memset(huge, 'a', sizeof(huge));
    expect_err(huge, 257, -EMSGSIZE);
    expect_err(huge, 1000, -EMSGSIZE);
}

static void test_reject_encoding(void)
{
    char raw_nul[] = "ssid=A\0B&pass=12345678";
    prov_creds_t creds;

    expect_err("ssid=%GG&pass=12345678", strlen("ssid=%GG&pass=12345678"), -EILSEQ);
    expect_err("ssid=A%&pass=12345678", strlen("ssid=A%&pass=12345678"), -EILSEQ);
    expect_err("ssid=%2&pass=12345678", strlen("ssid=%2&pass=12345678"), -EILSEQ);
    expect_err("ssid=Home&pass=12345678&x=%", strlen("ssid=Home&pass=12345678&x=%"), -EILSEQ);

    expect_err("ssid=A%00B&pass=12345678", strlen("ssid=A%00B&pass=12345678"), -EINVAL);
    expect_err("ssid=Home&pass=12345678&x=%00", strlen("ssid=Home&pass=12345678&x=%00"), -EINVAL);

    expect_err(raw_nul, sizeof(raw_nul) - 1, -EINVAL);

    memset(&creds, 0x5a, sizeof(creds));
    assert(prov_form_parse(NULL, 0, &creds) == -EINVAL);
    assert(prov_form_parse(NULL, 1000, &creds) == -EINVAL);
    assert(prov_form_parse("ssid=Home&pass=12345678", 23, NULL) == -EINVAL);
}

int main(void)
{
    test_happy_and_order();
    test_url_decode();
    test_open_network();
    test_length_bounds();
    test_reject_shape();
    test_reject_encoding();

    return 0;
}
