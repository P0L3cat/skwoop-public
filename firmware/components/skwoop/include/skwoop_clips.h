#ifndef SKWOOP_CLIPS_H
#define SKWOOP_CLIPS_H

#include <stddef.h>
#include <stdint.h>

/*
 * Pod does not store the clip library. clips.idx may be a local file or a
 * buffer already fetched. Audio is HTTP GET from skwoop_config clip_base_url
 * (bridge host or a dock). This host step speaks http:// only.
 *
 * Return 0 on success. skwoop_clips_resolve returns 1 when the text is absent.
 * Other failures are negative errno-style codes.
 */

#define SKWOOP_CLIP_ID_MAX   64
#define SKWOOP_CLIP_PATH_MAX 256
#define SKWOOP_CLIP_TEXT_MAX 160

typedef struct skwoop_clip {
    char id[SKWOOP_CLIP_ID_MAX];
    char path[SKWOOP_CLIP_PATH_MAX]; /* relative path under clip_base_url */
    char text[SKWOOP_CLIP_TEXT_MAX]; /* normalized lookup key */
} skwoop_clip;

typedef struct skwoop_clip_index skwoop_clip_index;

/* Load a version-1 clips.idx. Does not keep the wav library. */
int skwoop_clips_load_file(const char *path, skwoop_clip_index **out);
int skwoop_clips_load_mem(const void *data, size_t len, skwoop_clip_index **out);
void skwoop_clips_free(skwoop_clip_index *idx);

/* text → clip id + relative path. Mood order: idle, happy, calm, alert, sad,
 * else the first pair. */
int skwoop_clips_resolve(const skwoop_clip_index *idx, const char *text,
                         skwoop_clip *out);

/* HTTP GET {clip_base_url}/{path for id} into out_path.
 * Uses the newest index still open from load_file / load_mem. */
int skwoop_clips_fetch(const char *id, const char *out_path);

#endif
