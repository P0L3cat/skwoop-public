#pragma once

#include "mood.h"

/* Load the embedded v1.1 table. Returns mood_load's result, or -1 when
 * components/mood/mood_v11.bin was absent at build time (mood disabled). */
int mood_builtin(mood_model_t *m);
