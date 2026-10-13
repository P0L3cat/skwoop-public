#pragma once

/* Fixed-size LRU cache of voice clips on the storage partition. Lines cache
 * as they are spoken; often-used ones stay. Layout under dir:
 *   index.bin   magic, tick, entries (id, bytes, last_used), CRC32
 *   <hash>.clp  one clip blob (the WAV as fetched)
 *   *.tmp       in-flight writes; a complete file is renamed into place
 * Eviction is least-recently-used by bytes. Plain stdio/POSIX, so the same
 * code runs on FATFS and in host tests. */

#include <stddef.h>
#include <stdint.h>

#define CLIPCACHE_ID_MAX 64
#define CLIPCACHE_ENTRIES_MAX 256
#define CLIPCACHE_DIR_MAX 96
#define CLIPCACHE_PATH_MAX (CLIPCACHE_DIR_MAX + 24)

typedef struct {
    char id[CLIPCACHE_ID_MAX];
    uint32_t bytes;
    uint32_t last_used;
} clipcache_entry_t;

typedef struct {
    char dir[CLIPCACHE_DIR_MAX];
    uint32_t cap_bytes;
    uint32_t used_bytes;
    uint32_t tick;
    uint32_t count;
    clipcache_entry_t entries[CLIPCACHE_ENTRIES_MAX];
} clipcache_t;

/* Load the index (index.bin, else a complete index.tmp left by a crash).
 * A corrupt or missing index starts empty. Entries whose blob is missing or
 * the wrong size are dropped; stray .tmp and unindexed .clp files are
 * deleted; then the cache is trimmed to cap_bytes. */
int clipcache_open(clipcache_t *cache, const char *dir, uint32_t cap_bytes);

/* Hit: writes the blob path, marks it most recently used, returns 0.
 * Miss: -ENOENT. */
int clipcache_lookup(clipcache_t *cache, const char *id, char *path, size_t cap);

/* Where a miss should be downloaded before clipcache_commit. */
int clipcache_temp_path(const clipcache_t *cache, const char *id, char *path, size_t cap);

/* Move the downloaded temp file into the cache, evicting LRU clips until it
 * fits. A clip larger than the cap is deleted and gets -EFBIG. */
int clipcache_commit(clipcache_t *cache, const char *id);
