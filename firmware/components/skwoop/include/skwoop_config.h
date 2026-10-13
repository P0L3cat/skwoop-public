#ifndef SKWOOP_CONFIG_H
#define SKWOOP_CONFIG_H

/*
 * Portable config store for a standalone Skwoop pod.
 *
 * Host tests persist under $SKWOOP_CONFIG_DIR (default /tmp/skwoop-config-test):
 *   wifi.json         — plaintext SSID, passphrase, standalone flag
 *   secrets.bin       — AES-256-GCM blob of provider API keys
 *   host.key          — random 32-byte AES key, HOST TESTS ONLY
 *   provider_base.txt — optional https provider base URL (not a secret)
 *   clip_base.txt     — clip_base_url, optional http(s) clip-host base (not a secret)
 *
 * standalone=1: the pod uses skwoop_net, the local fact store, and clip_base_url.
 * standalone=0: bridge websocket mode. The host build does not link that
 * client (no bridge-host calls) and reports "bridge mode not linked in host build".
 *
 * On device, secrets will sit under flash encryption / eFuse. Do not ship
 * host.key and do not log API key material.
 *
 * Length limits are maximum stored characters (excluding the NUL).
 * Return 0 on success, <0 on error.
 */

#include <stddef.h>

#define SKWOOP_SSID_MAX      32
#define SKWOOP_PASS_MAX      64
#define SKWOOP_PROVIDER_MAX  16
#define SKWOOP_API_KEY_MAX   256
#define SKWOOP_API_KEY_SLOTS 8
#define SKWOOP_BASE_URL_MAX  256

int skwoop_config_load(void);
int skwoop_config_save(void);

/* Refuse empty ssid. pass may be empty. Copies are NUL-terminated. */
int skwoop_config_set_wifi(const char *ssid, const char *pass);
int skwoop_config_get_wifi(char *ssid, size_t ssid_cap, char *pass, size_t pass_cap);

/* provider is a short id ("openai", "xai"). Refuse key length 0 or >256. */
int skwoop_config_set_api_key(const char *provider, const char *key);
int skwoop_config_get_api_key(const char *provider, char *key, size_t key_cap);

int skwoop_config_set_standalone(int on);
int skwoop_config_get_standalone(void);

/* https:// base URL for the streaming provider. Not an API key.
 * Refuses empty values and anything that does not start with "https://".
 * get returns <0 when no base URL is stored. */
int skwoop_config_set_provider_base(const char *url);
int skwoop_config_get_provider_base(char *url, size_t url_cap);

/* clip_base_url: base URL of the clip host (bridge host or dock). http:// or https://.
 * The pod fetches one clip at a time from here. It does not store the library.
 * get returns <0 when no clip base is stored. */
int skwoop_config_set_clip_base(const char *url);
int skwoop_config_get_clip_base(char *url, size_t url_cap);

/* Same pair. The name matches the README env key SKWOOP_CLIP_BASE_URL. */
#define skwoop_config_set_clip_base_url skwoop_config_set_clip_base
#define skwoop_config_get_clip_base_url skwoop_config_get_clip_base

#endif
