#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "esp_err.h"

/* Press is the debounced down edge. Long-press fires once if the hold reaches 1s. */
typedef void (*board_button_cb_t)(void);

esp_err_t board_init(board_button_cb_t on_press, board_button_cb_t on_long_press);

/* `samples` is the number of int16_t elements.
 * Read: 2 interleaved channels (mic, playback reference) at 24 kHz, so samples must be even.
 * Write: mono speaker samples at 24 kHz. The buffer must stay writable.
 */
esp_err_t board_audio_read(int16_t *buf, size_t samples);

esp_err_t board_audio_write(int16_t *buf, size_t samples);

/* Speaker volume 0..100 (clamped) and mute, on the ES8311. */
esp_err_t board_set_volume(int percent);

esp_err_t board_set_mute(bool mute);
