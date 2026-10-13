#include "clipcache.h"

#include <dirent.h>
#include <errno.h>
#include <stdbool.h>
#include <stdio.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

#define CC_MAGIC 0x31434b53u /* "SKC1" little-endian */
#define CC_INDEX "index.bin"
#define CC_INDEX_TMP "index.tmp"

static uint32_t crc32_update(uint32_t crc, const void *data, size_t len)
{
    /* SAFETY: data points at len readable bytes; read as bytes only. */
    const uint8_t *p = (const uint8_t *)data;

    crc = ~crc;

    while (len-- > 0) {
        crc ^= *p++;

        for (int k = 0; k < 8; k++) {
            crc = (crc >> 1) ^ (0xEDB88320u & (0u - (crc & 1u)));
        }
    }

    return ~crc;
}

static uint32_t id_hash(const char *id)
{
    uint32_t h = 2166136261u;

    for (; *id != '\0'; id++) {
        h = (h ^ (uint8_t)*id) * 16777619u;
    }

    return h;
}

static int dir_path(const clipcache_t *cache, const char *name, char *path, size_t cap)
{
    return snprintf(path, cap, "%s/%s", cache->dir, name) < (int)cap ? 0 : -ENAMETOOLONG;
}

static int blob_path(const clipcache_t *cache, const char *id, const char *ext, char *path, size_t cap)
{
    return snprintf(path, cap, "%s/%08x.%s", cache->dir, (unsigned)id_hash(id), ext) < (int)cap ? 0 : -ENAMETOOLONG;
}

static bool id_ok(const char *id)
{
    return id != NULL && id[0] != '\0' && strlen(id) < CLIPCACHE_ID_MAX;
}

static int find(const clipcache_t *cache, const char *id)
{
    for (uint32_t i = 0; i < cache->count; i++) {
        if (strcmp(cache->entries[i].id, id) == 0) {
            return (int)i;
        }
    }

    return -1;
}

static void drop(clipcache_t *cache, uint32_t i, bool delete_blob)
{
    char path[CLIPCACHE_PATH_MAX];

    if (delete_blob && blob_path(cache, cache->entries[i].id, "clp", path, sizeof path) == 0) {
        (void)unlink(path);
    }

    cache->used_bytes -= cache->entries[i].bytes;
    cache->entries[i] = cache->entries[--cache->count];
}

static void evict_lru(clipcache_t *cache)
{
    uint32_t oldest = 0;

    for (uint32_t i = 1; i < cache->count; i++) {
        if (cache->entries[i].last_used < cache->entries[oldest].last_used) {
            oldest = i;
        }
    }

    drop(cache, oldest, true);
}

/* Index file: magic, tick, count, entries[count], crc32 of everything before. */
static int save_index(const clipcache_t *cache)
{
    char tmp[CLIPCACHE_PATH_MAX];
    char final[CLIPCACHE_PATH_MAX];
    uint32_t head[3] = { CC_MAGIC, cache->tick, cache->count };
    size_t body = cache->count * sizeof(clipcache_entry_t);
    uint32_t crc = crc32_update(crc32_update(0, head, sizeof head), cache->entries, body);
    FILE *f;
    bool ok;

    if (dir_path(cache, CC_INDEX_TMP, tmp, sizeof tmp) != 0 || dir_path(cache, CC_INDEX, final, sizeof final) != 0) {
        return -ENAMETOOLONG;
    }

    f = fopen(tmp, "wb");

    if (f == NULL) {
        return -EIO;
    }

    ok = fwrite(head, sizeof head, 1, f) == 1 && (body == 0 || fwrite(cache->entries, body, 1, f) == 1)
         && fwrite(&crc, sizeof crc, 1, f) == 1;
    ok = (fclose(f) == 0) && ok;

    if (!ok) {
        (void)unlink(tmp);

        return -EIO;
    }

    /* FAT rename cannot replace a file. A crash between these two calls
     * leaves a complete index.tmp, which clipcache_open accepts. */
    (void)unlink(final);

    return rename(tmp, final) == 0 ? 0 : -EIO;
}

static bool load_index_file(clipcache_t *cache, const char *path)
{
    uint32_t head[3];
    uint32_t crc;
    size_t body;
    FILE *f = fopen(path, "rb");
    bool ok;

    if (f == NULL) {
        return false;
    }

    ok = fread(head, sizeof head, 1, f) == 1 && head[0] == CC_MAGIC && head[2] <= CLIPCACHE_ENTRIES_MAX;
    body = ok ? head[2] * sizeof(clipcache_entry_t) : 0;
    ok = ok && (body == 0 || fread(cache->entries, body, 1, f) == 1) && fread(&crc, sizeof crc, 1, f) == 1
         && fgetc(f) == EOF && crc == crc32_update(crc32_update(0, head, sizeof head), cache->entries, body);
    fclose(f);

    if (!ok) {
        return false;
    }

    cache->tick = head[1];
    cache->count = head[2];

    return true;
}

/* Keep entries whose id is sane and whose blob exists with the indexed size. */
static void verify_entries(clipcache_t *cache)
{
    cache->used_bytes = 0;

    for (uint32_t i = 0; i < cache->count; i++) {
        cache->used_bytes += cache->entries[i].bytes;
    }

    for (uint32_t i = 0; i < cache->count;) {
        clipcache_entry_t *e = &cache->entries[i];
        char path[CLIPCACHE_PATH_MAX];
        struct stat st;
        bool ok;

        e->id[CLIPCACHE_ID_MAX - 1] = '\0';
        ok = id_ok(e->id) && find(cache, e->id) == (int)i
             && blob_path(cache, e->id, "clp", path, sizeof path) == 0 && stat(path, &st) == 0
             && (uint32_t)st.st_size == e->bytes;

        if (ok) {
            i++;
        } else {
            drop(cache, i, false);
        }
    }
}

static bool indexed_blob(const clipcache_t *cache, const char *name)
{
    char want[16];

    for (uint32_t i = 0; i < cache->count; i++) {
        snprintf(want, sizeof want, "%08x.clp", (unsigned)id_hash(cache->entries[i].id));

        if (strcasecmp(want, name) == 0) {
            return true;
        }
    }

    return false;
}

static void sweep_dir(const clipcache_t *cache)
{
    DIR *d = opendir(cache->dir);
    struct dirent *ent;

    if (d == NULL) {
        return;
    }

    while ((ent = readdir(d)) != NULL) {
        const char *dot = strrchr(ent->d_name, '.');
        char path[CLIPCACHE_PATH_MAX];
        bool stray;

        if (dot == NULL) {
            continue;
        }

        stray = strcasecmp(dot, ".tmp") == 0 || (strcasecmp(dot, ".clp") == 0 && !indexed_blob(cache, ent->d_name));

        if (stray && dir_path(cache, ent->d_name, path, sizeof path) == 0) {
            (void)unlink(path);
        }
    }

    closedir(d);
}

int clipcache_open(clipcache_t *cache, const char *dir, uint32_t cap_bytes)
{
    char path[CLIPCACHE_PATH_MAX];
    bool loaded;

    if (cache == NULL || dir == NULL || strlen(dir) >= CLIPCACHE_DIR_MAX || cap_bytes == 0) {
        return -EINVAL;
    }

    memset(cache, 0, sizeof *cache);
    strcpy(cache->dir, dir);
    cache->cap_bytes = cap_bytes;

    if (mkdir(dir, 0755) != 0 && errno != EEXIST) {
        return -EIO;
    }

    loaded = dir_path(cache, CC_INDEX, path, sizeof path) == 0 && load_index_file(cache, path);

    if (!loaded) {
        cache->count = 0;
        loaded = dir_path(cache, CC_INDEX_TMP, path, sizeof path) == 0 && load_index_file(cache, path);
    }

    if (!loaded) {
        cache->count = 0;
        cache->tick = 0;
    }

    verify_entries(cache);
    sweep_dir(cache);

    while (cache->used_bytes > cache->cap_bytes) {
        evict_lru(cache);
    }

    return save_index(cache);
}

int clipcache_lookup(clipcache_t *cache, const char *id, char *path, size_t cap)
{
    int i;

    if (cache == NULL || !id_ok(id) || path == NULL) {
        return -EINVAL;
    }

    i = find(cache, id);

    if (i < 0) {
        return -ENOENT;
    }

    if (blob_path(cache, id, "clp", path, cap) != 0) {
        return -ENAMETOOLONG;
    }

    cache->entries[i].last_used = ++cache->tick;

    return save_index(cache);
}

int clipcache_temp_path(const clipcache_t *cache, const char *id, char *path, size_t cap)
{
    if (cache == NULL || !id_ok(id) || path == NULL) {
        return -EINVAL;
    }

    return blob_path(cache, id, "tmp", path, cap);
}

int clipcache_commit(clipcache_t *cache, const char *id)
{
    char tmp[CLIPCACHE_PATH_MAX];
    char final[CLIPCACHE_PATH_MAX];
    struct stat st;
    uint32_t bytes;
    char want[16];

    if (cache == NULL || !id_ok(id) || blob_path(cache, id, "tmp", tmp, sizeof tmp) != 0
        || blob_path(cache, id, "clp", final, sizeof final) != 0) {
        return -EINVAL;
    }

    if (stat(tmp, &st) != 0) {
        return -ENOENT;
    }

    bytes = (uint32_t)st.st_size;

    if (bytes == 0 || bytes > cache->cap_bytes) {
        (void)unlink(tmp);

        return bytes == 0 ? -EBADMSG : -EFBIG;
    }

    /* Same id, or another id hashing to the same blob name: replace it. */
    snprintf(want, sizeof want, "%08x", (unsigned)id_hash(id));

    for (uint32_t i = 0; i < cache->count;) {
        char have[16];

        snprintf(have, sizeof have, "%08x", (unsigned)id_hash(cache->entries[i].id));

        if (strcmp(have, want) == 0) {
            drop(cache, i, true);
        } else {
            i++;
        }
    }

    while (cache->count > 0
           && (cache->used_bytes + bytes > cache->cap_bytes || cache->count >= CLIPCACHE_ENTRIES_MAX)) {
        evict_lru(cache);
    }

    (void)unlink(final);

    if (rename(tmp, final) != 0) {
        (void)unlink(tmp);

        return -EIO;
    }

    clipcache_entry_t *e = &cache->entries[cache->count++];

    memset(e, 0, sizeof *e);
    strcpy(e->id, id);
    e->bytes = bytes;
    e->last_used = ++cache->tick;
    cache->used_bytes += bytes;

    return save_index(cache);
}
