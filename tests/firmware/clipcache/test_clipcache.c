/* Host checks for the LRU clip cache: eviction by bytes, touch order,
 * oversize refusal, and recovery from corrupt/partial state. */

#include "clipcache.h"

#include <assert.h>
#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

static char g_dir[64];

static void download(clipcache_t *c, const char *id, size_t bytes)
{
    char path[CLIPCACHE_PATH_MAX];
    FILE *f;

    assert(clipcache_temp_path(c, id, path, sizeof path) == 0);
    f = fopen(path, "wb");
    assert(f != NULL);

    for (size_t i = 0; i < bytes; i++) {
        fputc((int)(i & 0xff), f);
    }

    fclose(f);
}

static void put(clipcache_t *c, const char *id, size_t bytes)
{
    download(c, id, bytes);
    assert(clipcache_commit(c, id) == 0);
}

static int hit(clipcache_t *c, const char *id)
{
    char path[CLIPCACHE_PATH_MAX];

    return clipcache_lookup(c, id, path, sizeof path) == 0;
}

static int file_count(const char *ext)
{
    char cmd[160];
    FILE *p;
    int n = -1;

    snprintf(cmd, sizeof cmd, "ls %s | grep -c '\\.%s$'", g_dir, ext);
    p = popen(cmd, "r");
    assert(p != NULL);
    assert(fscanf(p, "%d", &n) == 1);
    pclose(p);

    return n;
}

static void dir_file(const char *name, char *path, size_t cap)
{
    snprintf(path, cap, "%s/%s", g_dir, name);
}

static void test_lru_by_bytes(void)
{
    static clipcache_t c;

    assert(clipcache_open(&c, g_dir, 1000) == 0);
    put(&c, "hello", 400);
    put(&c, "okay", 300);
    put(&c, "sure", 200);
    assert(c.used_bytes == 900);

    assert(hit(&c, "hello"));
    put(&c, "bye", 300);
    assert(!hit(&c, "okay"));
    assert(hit(&c, "hello") && hit(&c, "sure") && hit(&c, "bye"));
    assert(c.used_bytes == 900);

    put(&c, "big", 900);
    assert(c.count == 1 && hit(&c, "big"));

    download(&c, "huge", 1001);
    assert(clipcache_commit(&c, "huge") == -EFBIG);
    assert(hit(&c, "big") && file_count("tmp") == 0);

    put(&c, "big", 500);
    assert(c.count == 1 && c.used_bytes == 500);
    assert(file_count("clp") == 1);
}

static void test_persist_and_shrink(void)
{
    static clipcache_t c;

    assert(clipcache_open(&c, g_dir, 1000) == 0);
    assert(c.count == 1 && hit(&c, "big"));
    put(&c, "a", 200);
    put(&c, "b", 200);
    assert(hit(&c, "big"));

    assert(clipcache_open(&c, g_dir, 550) == 0);
    assert(c.count == 1 && hit(&c, "big") && c.used_bytes == 500);
}

static void test_corrupt_index(void)
{
    static clipcache_t c;
    char path[CLIPCACHE_PATH_MAX];
    FILE *f;

    assert(clipcache_open(&c, g_dir, 1000) == 0);
    put(&c, "x", 100);
    dir_file("index.bin", path, sizeof path);
    f = fopen(path, "r+b");
    assert(f != NULL);
    fseek(f, 20, SEEK_SET);
    fputc(0x5a, f);
    fclose(f);

    assert(clipcache_open(&c, g_dir, 1000) == 0);
    assert(c.count == 0 && c.used_bytes == 0);
    assert(file_count("clp") == 0);
    assert(!hit(&c, "x"));

    put(&c, "y", 100);
    assert(truncate(path, 7) == 0);
    assert(clipcache_open(&c, g_dir, 1000) == 0);
    assert(c.count == 0 && file_count("clp") == 0);
}

static void test_crash_states(void)
{
    static clipcache_t c;
    char index[CLIPCACHE_PATH_MAX];
    char index_tmp[CLIPCACHE_PATH_MAX];
    char blob[CLIPCACHE_PATH_MAX];

    assert(clipcache_open(&c, g_dir, 1000) == 0);
    put(&c, "keep", 100);
    put(&c, "gone", 100);

    /* Crash between unlink(index.bin) and rename(index.tmp). */
    dir_file("index.bin", index, sizeof index);
    dir_file("index.tmp", index_tmp, sizeof index_tmp);
    assert(rename(index, index_tmp) == 0);

    /* A blob lost and a half-written download. */
    assert(clipcache_lookup(&c, "gone", blob, sizeof blob) == 0);
    assert(unlink(blob) == 0);
    download(&c, "partial", 50);

    assert(clipcache_open(&c, g_dir, 1000) == 0);
    assert(c.count == 1 && hit(&c, "keep") && !hit(&c, "gone"));
    assert(file_count("tmp") == 0 && file_count("clp") == 1);

    /* Blob size changed under the index. */
    assert(clipcache_lookup(&c, "keep", blob, sizeof blob) == 0);
    assert(truncate(blob, 10) == 0);
    assert(clipcache_open(&c, g_dir, 1000) == 0);
    assert(c.count == 0 && file_count("clp") == 0);
}

static void test_bad_args(void)
{
    static clipcache_t c;
    char path[CLIPCACHE_PATH_MAX];
    char long_id[CLIPCACHE_ID_MAX + 1];

    memset(long_id, 'a', sizeof long_id - 1);
    long_id[sizeof long_id - 1] = '\0';
    assert(clipcache_open(&c, g_dir, 0) == -EINVAL);
    assert(clipcache_open(&c, g_dir, 1000) == 0);
    assert(clipcache_lookup(&c, long_id, path, sizeof path) == -EINVAL);
    assert(clipcache_lookup(&c, "", path, sizeof path) == -EINVAL);
    assert(clipcache_commit(&c, "never-downloaded") == -ENOENT);
}

int main(void)
{
    char cmd[96];

    strcpy(g_dir, "/tmp/clipcache-XXXXXX");
    assert(mkdtemp(g_dir) != NULL);
    test_lru_by_bytes();
    test_persist_and_shrink();
    test_corrupt_index();
    test_crash_states();
    test_bad_args();
    snprintf(cmd, sizeof cmd, "rm -rf %s", g_dir);
    assert(system(cmd) == 0);
    puts("clipcache ok");

    return 0;
}
