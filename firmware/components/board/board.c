#include "board.h"

#include "axp2101.h"
#include "board_pins.h"
#include "button.h"

#include "driver/i2c_master.h"
#include "driver/i2s_std.h"
#include "driver/i2s_tdm.h"
#include "driver/spi_master.h"
#include "esp_check.h"
#include "esp_lcd_co5300.h"
#include "esp_lcd_io_i2c.h"
#include "esp_lcd_panel_io.h"
#include "esp_lcd_panel_ops.h"
#include "esp_lcd_touch_cst9217.h"
#include "esp_codec_dev.h"
#include "esp_codec_dev_defaults.h"
#include "esp_lvgl_port.h"

#include <limits.h>

#define BOARD_I2C_GLITCH_FILTER 7
#define BOARD_LCD_PCLK_HZ (40 * 1000 * 1000)
#define BOARD_LCD_DRAW_LINES 40
#define BOARD_TOUCH_I2C_HZ 400000
#define BOARD_AUDIO_DMA_DESC_NUM 6
#define BOARD_AUDIO_DMA_FRAME_NUM 240
#define BOARD_AUDIO_RX_CHANNELS 2
#define BOARD_AUDIO_TX_CHANNELS 1
#define BOARD_AUDIO_ES7210_SLOTS 4
#define BOARD_AUDIO_MIC_GAIN_DB 30.0f
#define BOARD_AUDIO_OUTPUT_VOLUME 70

static const char *TAG = "board";

static i2c_master_bus_handle_t s_i2c;
static esp_lcd_panel_io_handle_t s_panel_io;
static esp_codec_dev_handle_t s_input;
static esp_codec_dev_handle_t s_output;

/* File-scope compound literals: the panel driver keeps these pointers. */
static const co5300_lcd_init_cmd_t s_vendor_init[] = {
    {0xFE, (const uint8_t[]){0x20}, 1, 0},
    {0x19, (const uint8_t[]){0x10}, 1, 0},
    {0x1C, (const uint8_t[]){0xA0}, 1, 0},
    {0xFE, (const uint8_t[]){0x00}, 1, 0},
    {0xC4, (const uint8_t[]){0x80}, 1, 0},
    {0x3A, (const uint8_t[]){0x55}, 1, 0},
    {0x35, (const uint8_t[]){0x00}, 1, 0},
    {0x53, (const uint8_t[]){0x20}, 1, 0},
    {0x51, (const uint8_t[]){0xFF}, 1, 0},
    {0x63, (const uint8_t[]){0xFF}, 1, 0},
    {0x2A, (const uint8_t[]){0x00, 0x06, 0x01, 0xD7}, 4, 0},
    {0x2B, (const uint8_t[]){0x00, 0x00, 0x01, 0xD1}, 4, 600},
    {0x11, NULL, 0, 600},
    {0x29, NULL, 0, 0},
};

static esp_err_t board_i2c_init(void)
{
    i2c_master_bus_config_t cfg = {
        .i2c_port = I2C_NUM_0,
        .sda_io_num = BOARD_PIN_I2C_SDA,
        .scl_io_num = BOARD_PIN_I2C_SCL,
        .clk_source = I2C_CLK_SRC_DEFAULT,
        .glitch_ignore_cnt = BOARD_I2C_GLITCH_FILTER,
        .flags = {
            .enable_internal_pullup = 1,
        },
    };

    return i2c_new_master_bus(&cfg, &s_i2c);
}

static void board_display_round_area(lv_event_t *event)
{
    void *param = lv_event_get_param(event);

    if (param == NULL) {
        return;
    }

    // SAFETY: LV_EVENT_INVALIDATE_AREA passes the invalidated area as lv_area_t*.
    lv_area_t *area = param;
    int32_t x1 = area->x1;
    int32_t y1 = area->y1;
    int32_t x2 = area->x2;
    int32_t y2 = area->y2;

    area->x1 = (x1 >> 1) << 1;
    area->y1 = (y1 >> 1) << 1;
    area->x2 = ((x2 >> 1) << 1) + 1;
    area->y2 = ((y2 >> 1) << 1) + 1;
}

static esp_err_t board_display_init(void)
{
    spi_bus_config_t buscfg = {
        .sclk_io_num = BOARD_PIN_LCD_PCLK,
        .data0_io_num = BOARD_PIN_LCD_DATA0,
        .data1_io_num = BOARD_PIN_LCD_DATA1,
        .data2_io_num = BOARD_PIN_LCD_DATA2,
        .data3_io_num = BOARD_PIN_LCD_DATA3,
        .data4_io_num = GPIO_NUM_NC,
        .data5_io_num = GPIO_NUM_NC,
        .data6_io_num = GPIO_NUM_NC,
        .data7_io_num = GPIO_NUM_NC,
        .max_transfer_sz = BOARD_LCD_H_RES * BOARD_LCD_V_RES * (int)sizeof(uint16_t),
        .flags = SPICOMMON_BUSFLAG_QUAD,
    };

    ESP_RETURN_ON_ERROR(spi_bus_initialize(SPI2_HOST, &buscfg, SPI_DMA_CH_AUTO), TAG, "spi bus");

    esp_lcd_panel_io_spi_config_t io_config = {
        .cs_gpio_num = BOARD_PIN_LCD_CS,
        .dc_gpio_num = GPIO_NUM_NC,
        .spi_mode = 0,
        .pclk_hz = BOARD_LCD_PCLK_HZ,
        .trans_queue_depth = 10,
        .lcd_cmd_bits = 32,
        .lcd_param_bits = 8,
        .flags = {
            .quad_mode = 1,
        },
    };

    ESP_RETURN_ON_ERROR(esp_lcd_new_panel_io_spi(SPI2_HOST, &io_config, &s_panel_io), TAG, "panel io");

    co5300_vendor_config_t vendor_config = {
        .init_cmds = s_vendor_init,
        .init_cmds_size = sizeof(s_vendor_init) / sizeof(s_vendor_init[0]),
        .flags = {
            .use_qspi_interface = 1,
        },
    };
    esp_lcd_panel_dev_config_t panel_config = {
        .reset_gpio_num = BOARD_PIN_LCD_RST,
        .rgb_ele_order = LCD_RGB_ELEMENT_ORDER_RGB,
        .bits_per_pixel = 16,
        .vendor_config = &vendor_config,
    };
    esp_lcd_panel_handle_t panel = NULL;

    ESP_RETURN_ON_ERROR(esp_lcd_new_panel_co5300(s_panel_io, &panel_config, &panel), TAG, "co5300");
    ESP_RETURN_ON_ERROR(esp_lcd_panel_set_gap(panel, 0x06, 0), TAG, "gap");
    ESP_RETURN_ON_ERROR(esp_lcd_panel_reset(panel), TAG, "reset");
    ESP_RETURN_ON_ERROR(esp_lcd_panel_init(panel), TAG, "init");
    ESP_RETURN_ON_ERROR(esp_lcd_panel_invert_color(panel, false), TAG, "invert");
    ESP_RETURN_ON_ERROR(esp_lcd_panel_mirror(panel, false, false), TAG, "mirror");
    ESP_RETURN_ON_ERROR(esp_lcd_panel_disp_on_off(panel, true), TAG, "disp on");

    uint8_t brightness = 0xFF;
    int lcd_cmd = 0x51;

    lcd_cmd &= 0xff;
    lcd_cmd <<= 8;
    lcd_cmd |= 0x02 << 24;
    ESP_RETURN_ON_ERROR(esp_lcd_panel_io_tx_param(s_panel_io, lcd_cmd, &brightness, sizeof(brightness)), TAG, "brightness");

    lvgl_port_cfg_t lvgl_cfg = ESP_LVGL_PORT_INIT_CONFIG();

    lvgl_cfg.task_stack = 8192;
    ESP_RETURN_ON_ERROR(lvgl_port_init(&lvgl_cfg), TAG, "lvgl port");

    const lvgl_port_display_cfg_t disp_cfg = {
        .io_handle = s_panel_io,
        .panel_handle = panel,
        .buffer_size = BOARD_LCD_H_RES * BOARD_LCD_DRAW_LINES,
        .double_buffer = true,
        .hres = BOARD_LCD_H_RES,
        .vres = BOARD_LCD_V_RES,
        .color_format = LV_COLOR_FORMAT_RGB565,
        .rotation = {
            .swap_xy = false,
            .mirror_x = false,
            .mirror_y = false,
        },
        .flags = {
            .buff_dma = 1,
            .buff_spiram = 1,
            .swap_bytes = 1,
        },
    };
    lv_display_t *disp = lvgl_port_add_disp(&disp_cfg);

    ESP_RETURN_ON_FALSE(disp != NULL, ESP_FAIL, TAG, "add disp");
    /* QSPI draw windows must start even and end odd or the panel tears the column. */
    lv_display_add_event_cb(disp, board_display_round_area, LV_EVENT_INVALIDATE_AREA, NULL);

    esp_lcd_panel_io_i2c_config_t tp_io_config = ESP_LCD_TOUCH_IO_I2C_CST9217_CONFIG();

    tp_io_config.scl_speed_hz = BOARD_TOUCH_I2C_HZ;

    esp_lcd_panel_io_handle_t tp_io = NULL;

    ESP_RETURN_ON_ERROR(esp_lcd_new_panel_io_i2c(s_i2c, &tp_io_config, &tp_io), TAG, "touch io");

    esp_lcd_touch_config_t tp_cfg = {
        .x_max = BOARD_LCD_H_RES - 1,
        .y_max = BOARD_LCD_V_RES - 1,
        .rst_gpio_num = BOARD_PIN_TOUCH_RST,
        .int_gpio_num = BOARD_PIN_TOUCH_INT,
        .levels = {
            .reset = 0,
            .interrupt = 0,
        },
        .flags = {
            .swap_xy = 0,
            .mirror_x = 1,
            .mirror_y = 1,
        },
    };
    esp_lcd_touch_handle_t tp = NULL;

    ESP_RETURN_ON_ERROR(esp_lcd_touch_new_i2c_cst9217(tp_io, &tp_cfg, &tp), TAG, "cst9217");

    const lvgl_port_touch_cfg_t touch_cfg = {
        .disp = disp,
        .handle = tp,
    };

    ESP_RETURN_ON_FALSE(lvgl_port_add_touch(&touch_cfg) != NULL, ESP_FAIL, TAG, "add touch");

    return ESP_OK;
}

static esp_err_t board_audio_open_channels(i2s_chan_handle_t *tx, i2s_chan_handle_t *rx)
{
    i2s_chan_config_t chan_cfg = {
        .id = I2S_NUM_0,
        .role = I2S_ROLE_MASTER,
        .dma_desc_num = BOARD_AUDIO_DMA_DESC_NUM,
        .dma_frame_num = BOARD_AUDIO_DMA_FRAME_NUM,
        .auto_clear_after_cb = true,
        .auto_clear_before_cb = false,
        .intr_priority = 0,
    };

    ESP_RETURN_ON_ERROR(i2s_new_channel(&chan_cfg, tx, rx), TAG, "i2s channel");

    i2s_std_config_t std_cfg = {
        .clk_cfg = {
            .sample_rate_hz = BOARD_AUDIO_SAMPLE_RATE_HZ,
            .clk_src = I2S_CLK_SRC_DEFAULT,
            .ext_clk_freq_hz = 0,
            .mclk_multiple = I2S_MCLK_MULTIPLE_256,
        },
        .slot_cfg = {
            .data_bit_width = I2S_DATA_BIT_WIDTH_16BIT,
            .slot_bit_width = I2S_SLOT_BIT_WIDTH_AUTO,
            .slot_mode = I2S_SLOT_MODE_STEREO,
            .slot_mask = I2S_STD_SLOT_BOTH,
            .ws_width = I2S_DATA_BIT_WIDTH_16BIT,
            .ws_pol = false,
            .bit_shift = true,
            .left_align = true,
            .big_endian = false,
            .bit_order_lsb = false,
        },
        .gpio_cfg = {
            .mclk = BOARD_PIN_I2S_MCLK,
            .bclk = BOARD_PIN_I2S_BCLK,
            .ws = BOARD_PIN_I2S_WS,
            .dout = BOARD_PIN_I2S_DOUT,
            .din = I2S_GPIO_UNUSED,
            .invert_flags = {
                .mclk_inv = false,
                .bclk_inv = false,
                .ws_inv = false,
            },
        },
    };
    i2s_tdm_config_t tdm_cfg = {
        .clk_cfg = {
            .sample_rate_hz = BOARD_AUDIO_SAMPLE_RATE_HZ,
            .clk_src = I2S_CLK_SRC_DEFAULT,
            .ext_clk_freq_hz = 0,
            .mclk_multiple = I2S_MCLK_MULTIPLE_256,
            .bclk_div = 8,
        },
        .slot_cfg = {
            .data_bit_width = I2S_DATA_BIT_WIDTH_16BIT,
            .slot_bit_width = I2S_SLOT_BIT_WIDTH_AUTO,
            .slot_mode = I2S_SLOT_MODE_STEREO,
            .slot_mask = (i2s_tdm_slot_mask_t)(I2S_TDM_SLOT0 | I2S_TDM_SLOT1 | I2S_TDM_SLOT2 | I2S_TDM_SLOT3),
            .ws_width = I2S_TDM_AUTO_WS_WIDTH,
            .ws_pol = false,
            .bit_shift = true,
            .left_align = false,
            .big_endian = false,
            .bit_order_lsb = false,
            .skip_mask = false,
            .total_slot = I2S_TDM_AUTO_SLOT_NUM,
        },
        .gpio_cfg = {
            .mclk = BOARD_PIN_I2S_MCLK,
            .bclk = BOARD_PIN_I2S_BCLK,
            .ws = BOARD_PIN_I2S_WS,
            .dout = I2S_GPIO_UNUSED,
            .din = BOARD_PIN_I2S_DIN,
            .invert_flags = {
                .mclk_inv = false,
                .bclk_inv = false,
                .ws_inv = false,
            },
        },
    };

    ESP_RETURN_ON_ERROR(i2s_channel_init_std_mode(*tx, &std_cfg), TAG, "i2s tx");
    ESP_RETURN_ON_ERROR(i2s_channel_init_tdm_mode(*rx, &tdm_cfg), TAG, "i2s rx");

    return ESP_OK;
}

static esp_err_t board_codec_check(int rc, const char *what)
{
    if (rc != ESP_CODEC_DEV_OK) {
        ESP_LOGE(TAG, "%s failed: %d", what, rc);

        return ESP_FAIL;
    }

    return ESP_OK;
}

static esp_err_t board_audio_init(void)
{
    i2s_chan_handle_t tx = NULL;
    i2s_chan_handle_t rx = NULL;

    ESP_RETURN_ON_ERROR(board_audio_open_channels(&tx, &rx), TAG, "i2s");

    audio_codec_i2s_cfg_t i2s_cfg = {
        .port = I2S_NUM_0,
        .rx_handle = rx,
        .tx_handle = tx,
    };
    const audio_codec_data_if_t *data_if = audio_codec_new_i2s_data(&i2s_cfg);

    ESP_RETURN_ON_FALSE(data_if != NULL, ESP_ERR_NO_MEM, TAG, "i2s data");

    audio_codec_i2c_cfg_t i2c_cfg = {
        .port = I2C_NUM_0,
        .addr = ES8311_CODEC_DEFAULT_ADDR,
        .bus_handle = s_i2c,
    };
    const audio_codec_ctrl_if_t *out_ctrl = audio_codec_new_i2c_ctrl(&i2c_cfg);
    const audio_codec_gpio_if_t *gpio_if = audio_codec_new_gpio();

    ESP_RETURN_ON_FALSE(out_ctrl != NULL && gpio_if != NULL, ESP_ERR_NO_MEM, TAG, "es8311 ctrl");

    es8311_codec_cfg_t es8311_cfg = {
        .ctrl_if = out_ctrl,
        .gpio_if = gpio_if,
        .codec_mode = ESP_CODEC_DEV_WORK_MODE_DAC,
        .pa_pin = BOARD_PIN_PA,
        .use_mclk = true,
        .hw_gain = {
            .pa_voltage = 5.0f,
            .codec_dac_voltage = 3.3f,
        },
    };
    const audio_codec_if_t *out_codec = es8311_codec_new(&es8311_cfg);

    ESP_RETURN_ON_FALSE(out_codec != NULL, ESP_ERR_NO_MEM, TAG, "es8311");

    esp_codec_dev_cfg_t dev_cfg = {
        .dev_type = ESP_CODEC_DEV_TYPE_OUT,
        .codec_if = out_codec,
        .data_if = data_if,
    };

    s_output = esp_codec_dev_new(&dev_cfg);
    ESP_RETURN_ON_FALSE(s_output != NULL, ESP_ERR_NO_MEM, TAG, "spk dev");

    i2c_cfg.addr = ES7210_CODEC_DEFAULT_ADDR;

    const audio_codec_ctrl_if_t *in_ctrl = audio_codec_new_i2c_ctrl(&i2c_cfg);

    ESP_RETURN_ON_FALSE(in_ctrl != NULL, ESP_ERR_NO_MEM, TAG, "es7210 ctrl");

    es7210_codec_cfg_t es7210_cfg = {
        .ctrl_if = in_ctrl,
        .mic_selected = ES7210_SEL_MIC1 | ES7210_SEL_MIC2 | ES7210_SEL_MIC3 | ES7210_SEL_MIC4,
    };
    const audio_codec_if_t *in_codec = es7210_codec_new(&es7210_cfg);

    ESP_RETURN_ON_FALSE(in_codec != NULL, ESP_ERR_NO_MEM, TAG, "es7210");

    dev_cfg.dev_type = ESP_CODEC_DEV_TYPE_IN;
    dev_cfg.codec_if = in_codec;
    s_input = esp_codec_dev_new(&dev_cfg);
    ESP_RETURN_ON_FALSE(s_input != NULL, ESP_ERR_NO_MEM, TAG, "mic dev");

    esp_codec_dev_sample_info_t out_fs = {
        .bits_per_sample = 16,
        .channel = BOARD_AUDIO_TX_CHANNELS,
        .channel_mask = 0,
        .sample_rate = BOARD_AUDIO_SAMPLE_RATE_HZ,
        .mclk_multiple = 0,
    };

    ESP_RETURN_ON_ERROR(board_codec_check(esp_codec_dev_open(s_output, &out_fs), "spk open"), TAG, "spk open");
    ESP_RETURN_ON_ERROR(board_codec_check(esp_codec_dev_set_out_vol(s_output, BOARD_AUDIO_OUTPUT_VOLUME), "volume"),
                        TAG, "volume");

    esp_codec_dev_sample_info_t in_fs = {
        .bits_per_sample = 16,
        .channel = BOARD_AUDIO_ES7210_SLOTS,
        .channel_mask = ESP_CODEC_DEV_MAKE_CHANNEL_MASK(0) | ESP_CODEC_DEV_MAKE_CHANNEL_MASK(1),
        .sample_rate = BOARD_AUDIO_SAMPLE_RATE_HZ,
        .mclk_multiple = 0,
    };

    ESP_RETURN_ON_ERROR(board_codec_check(esp_codec_dev_open(s_input, &in_fs), "mic open"), TAG, "mic open");
    ESP_RETURN_ON_ERROR(
        board_codec_check(esp_codec_dev_set_in_channel_gain(s_input, ESP_CODEC_DEV_MAKE_CHANNEL_MASK(0),
                                                            BOARD_AUDIO_MIC_GAIN_DB),
                          "mic gain"),
        TAG, "mic gain");

    return ESP_OK;
}

static esp_err_t board_audio_xfer(esp_codec_dev_handle_t dev, int16_t *buf, size_t samples, size_t channels, bool write)
{
    if (dev == NULL) {
        return ESP_ERR_INVALID_STATE;
    }

    if (buf == NULL || samples == 0 || (samples % channels) != 0 || samples > (size_t)INT_MAX / sizeof(int16_t)) {
        return ESP_ERR_INVALID_ARG;
    }

    int len = (int)(samples * sizeof(int16_t));
    int rc = write ? esp_codec_dev_write(dev, buf, len) : esp_codec_dev_read(dev, buf, len);

    if (rc != ESP_CODEC_DEV_OK) {
        return ESP_FAIL;
    }

    return ESP_OK;
}

esp_err_t board_audio_read(int16_t *buf, size_t samples)
{
    return board_audio_xfer(s_input, buf, samples, BOARD_AUDIO_RX_CHANNELS, false);
}

esp_err_t board_audio_write(int16_t *buf, size_t samples)
{
    return board_audio_xfer(s_output, buf, samples, BOARD_AUDIO_TX_CHANNELS, true);
}

esp_err_t board_set_volume(int percent)
{
    if (s_output == NULL) {
        return ESP_ERR_INVALID_STATE;
    }

    percent = percent < 0 ? 0 : (percent > 100 ? 100 : percent);

    return board_codec_check(esp_codec_dev_set_out_vol(s_output, percent), "volume");
}

esp_err_t board_set_mute(bool mute)
{
    if (s_output == NULL) {
        return ESP_ERR_INVALID_STATE;
    }

    return board_codec_check(esp_codec_dev_set_out_mute(s_output, mute), "mute");
}

esp_err_t board_init(board_button_cb_t on_press, board_button_cb_t on_long_press)
{
    ESP_RETURN_ON_ERROR(board_i2c_init(), TAG, "i2c");
    ESP_RETURN_ON_ERROR(axp2101_init(s_i2c), TAG, "axp2101");
    ESP_RETURN_ON_ERROR(board_display_init(), TAG, "display");
    ESP_RETURN_ON_ERROR(board_audio_init(), TAG, "audio");
    ESP_RETURN_ON_ERROR(board_button_init(on_press, on_long_press), TAG, "button");

    return ESP_OK;
}
