#include "wake_pick.h"

#include <string.h>

static int find(const char *const *names, size_t count, const char *prefix, const char *keyword)
{
    if (keyword == NULL || keyword[0] == '\0') {
        return -1;
    }

    for (size_t i = 0; i < count; i++) {
        if (names[i] != NULL && strncmp(names[i], prefix, strlen(prefix)) == 0 && strstr(names[i], keyword) != NULL) {
            return (int)i;
        }
    }

    return -1;
}

int wake_pick(const char *const *names, size_t count, const char *prefix, const char *preferred,
              const char *fallback)
{
    int i;

    if (names == NULL || prefix == NULL) {
        return -1;
    }

    i = find(names, count, prefix, preferred);

    return i >= 0 ? i : find(names, count, prefix, fallback);
}
