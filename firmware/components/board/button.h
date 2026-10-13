#pragma once

#include "board.h"

esp_err_t board_button_init(board_button_cb_t on_press, board_button_cb_t on_long_press);
