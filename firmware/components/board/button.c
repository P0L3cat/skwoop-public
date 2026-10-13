#include "button.h"

#include "board_pins.h"

#include "driver/gpio.h"
#include "esp_check.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#define BOARD_BUTTON_DEBOUNCE_MS 30
#define BOARD_BUTTON_POLL_MS 20
#define BOARD_BUTTON_LONG_PRESS_US (1000 * 1000)
#define BOARD_BUTTON_TASK_STACK 4096
#define BOARD_BUTTON_TASK_PRIO 5

static const char *TAG = "button";

static board_button_cb_t s_on_press;
static board_button_cb_t s_on_long_press;
static TaskHandle_t s_task;

static void IRAM_ATTR board_button_isr(void *arg)
{
    (void)arg;
    BaseType_t woken = pdFALSE;

    vTaskNotifyGiveFromISR(s_task, &woken);

    if (woken == pdTRUE) {
        portYIELD_FROM_ISR();
    }
}

static void board_button_task(void *arg)
{
    (void)arg;
    bool down = false;
    bool long_sent = false;
    int64_t down_at_us = 0;

    for (;;) {
        TickType_t wait = down ? pdMS_TO_TICKS(BOARD_BUTTON_POLL_MS) : portMAX_DELAY;
        uint32_t notified = ulTaskNotifyTake(pdTRUE, wait);
        int level = gpio_get_level(BOARD_PIN_BOOT);

        if (notified > 0) {
            vTaskDelay(pdMS_TO_TICKS(BOARD_BUTTON_DEBOUNCE_MS));
            (void)ulTaskNotifyTake(pdTRUE, 0);
            level = gpio_get_level(BOARD_PIN_BOOT);
        }

        bool pressed = (level == 0);

        if (pressed && !down) {
            down = true;
            long_sent = false;
            down_at_us = esp_timer_get_time();

            if (s_on_press != NULL) {
                s_on_press();
            }
        } else if (!pressed && down) {
            down = false;
            long_sent = false;
        } else if (pressed && down && !long_sent &&
                   (esp_timer_get_time() - down_at_us) >= BOARD_BUTTON_LONG_PRESS_US) {
            long_sent = true;

            if (s_on_long_press != NULL) {
                s_on_long_press();
            }
        }
    }
}

esp_err_t board_button_init(board_button_cb_t on_press, board_button_cb_t on_long_press)
{
    s_on_press = on_press;
    s_on_long_press = on_long_press;

    gpio_config_t cfg = {
        .pin_bit_mask = 1ULL << BOARD_PIN_BOOT,
        .mode = GPIO_MODE_INPUT,
        .pull_up_en = GPIO_PULLUP_ENABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_ANYEDGE,
    };

    ESP_RETURN_ON_ERROR(gpio_config(&cfg), TAG, "gpio config");

    BaseType_t created = xTaskCreate(board_button_task, "board_btn", BOARD_BUTTON_TASK_STACK, NULL,
                                     BOARD_BUTTON_TASK_PRIO, &s_task);
    ESP_RETURN_ON_FALSE(created == pdPASS, ESP_ERR_NO_MEM, TAG, "button task");

    esp_err_t isr = gpio_install_isr_service(0);

    if (isr != ESP_OK && isr != ESP_ERR_INVALID_STATE) {
        return isr;
    }

    return gpio_isr_handler_add(BOARD_PIN_BOOT, board_button_isr, NULL);
}
