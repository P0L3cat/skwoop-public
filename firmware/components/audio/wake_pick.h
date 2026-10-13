#pragma once

/* Pick the WakeNet model from what the model partition holds: the preferred
 * keyword first (custom "Hey Skwoop"), then the fallback ("Hi ESP"). Pure C. */

#include <stddef.h>

/* names: model names as packed (e.g. "wn9_hiesp", "mn7_en"). Only names that
 * start with prefix ("wn") count. Returns the index of the first name
 * containing preferred, else the first containing fallback, else -1. */
int wake_pick(const char *const *names, size_t count, const char *prefix, const char *preferred,
              const char *fallback);
