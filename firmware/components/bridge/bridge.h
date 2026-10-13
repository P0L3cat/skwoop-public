#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "esp_err.h"
#include "bridge_msg.h"

typedef struct {
    void (*on_msg)(const bridge_msg_t *msg);
    void (*on_audio)(const uint8_t *data, size_t len);
} bridge_callbacks_t;

/* Copy callbacks and spawn the bridge task. The task POSTs an OTA check-in,
 * then keeps a websocket open and dispatches server frames to the callbacks. */
esp_err_t bridge_start(const bridge_callbacks_t *callbacks);

/* Send one binary audio frame. False when the socket is down or the send fails. */
bool bridge_send_audio(const uint8_t *data, size_t len);

/* Send one NUL-terminated JSON text frame. False when it cannot be sent. */
bool bridge_send_text(const char *json);
