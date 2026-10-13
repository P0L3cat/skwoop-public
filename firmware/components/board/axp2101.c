#include "axp2101.h"

#include "esp_check.h"

#define AXP2101_ADDR_7BIT 0x34
#define AXP2101_I2C_HZ 400000
#define AXP2101_I2C_TIMEOUT_MS 100

#define AXP2101_REG_PWROFF_EN 0x22
#define AXP2101_REG_IRQ_OFF_ON_LEVEL 0x27
#define AXP2101_REG_DCDC_ONOFF 0x80
#define AXP2101_REG_LDO_ONOFF_0 0x90
#define AXP2101_REG_LDO_ONOFF_1 0x91
#define AXP2101_REG_DCDC1_VOLTAGE 0x82
#define AXP2101_REG_ALDO1_VOLTAGE 0x92
#define AXP2101_REG_IPRECHG 0x61
#define AXP2101_REG_ICC_CHG 0x62
#define AXP2101_REG_ITERM_CHG 0x63
#define AXP2101_REG_CV_VOLTAGE 0x64

/* REG 0x22 bits 2:1: die over-temp level 2 and PWRON>OFFLEVEL as power-off sources. */
#define AXP2101_PWROFF_EN_BTN_AND_DIE_OT 0x06
/* REG 0x27: power-key hold time of 4s before power-off. */
#define AXP2101_OFFLEVEL_HOLD_4S 0x10
#define AXP2101_DCDC_ONLY_DC1 0x01
#define AXP2101_LDO_ALL_OFF 0x00
#define AXP2101_LDO_ALDO1_ON 0x01
#define AXP2101_DCDC1_MV_MIN 1500
#define AXP2101_DCDC1_MV_STEP 100
#define AXP2101_ALDO_MV_MIN 500
#define AXP2101_ALDO_MV_STEP 100
#define AXP2101_DCDC1_3V3 ((3300 - AXP2101_DCDC1_MV_MIN) / AXP2101_DCDC1_MV_STEP)
#define AXP2101_ALDO1_3V3 ((3300 - AXP2101_ALDO_MV_MIN) / AXP2101_ALDO_MV_STEP)
#define AXP2101_PRECHARGE_50MA 0x02
/* 0x08 is the reference charge-current code (datasheet step is 200 mA at this code). */
#define AXP2101_CHARGE_CODE 0x08
#define AXP2101_TERM_25MA 0x01
#define AXP2101_CV_4V1 0x02

static const char *TAG = "axp2101";

static i2c_master_dev_handle_t s_dev;

static esp_err_t axp2101_write(uint8_t reg, uint8_t value)
{
    uint8_t payload[2] = {reg, value};

    return i2c_master_transmit(s_dev, payload, sizeof(payload), AXP2101_I2C_TIMEOUT_MS);
}

esp_err_t axp2101_init(i2c_master_bus_handle_t bus)
{
    ESP_RETURN_ON_FALSE(bus != NULL, ESP_ERR_INVALID_ARG, TAG, "null i2c bus");

    i2c_device_config_t dev_cfg = {
        .dev_addr_length = I2C_ADDR_BIT_LEN_7,
        .device_address = AXP2101_ADDR_7BIT,
        .scl_speed_hz = AXP2101_I2C_HZ,
    };

    ESP_RETURN_ON_ERROR(i2c_master_bus_add_device(bus, &dev_cfg, &s_dev), TAG, "add axp2101");

    ESP_RETURN_ON_ERROR(axp2101_write(AXP2101_REG_PWROFF_EN, AXP2101_PWROFF_EN_BTN_AND_DIE_OT), TAG, "pwroff en");
    ESP_RETURN_ON_ERROR(axp2101_write(AXP2101_REG_IRQ_OFF_ON_LEVEL, AXP2101_OFFLEVEL_HOLD_4S), TAG, "offlevel");
    ESP_RETURN_ON_ERROR(axp2101_write(AXP2101_REG_DCDC_ONOFF, AXP2101_DCDC_ONLY_DC1), TAG, "dcdc onoff");
    ESP_RETURN_ON_ERROR(axp2101_write(AXP2101_REG_LDO_ONOFF_0, AXP2101_LDO_ALL_OFF), TAG, "ldo off 0");
    ESP_RETURN_ON_ERROR(axp2101_write(AXP2101_REG_LDO_ONOFF_1, AXP2101_LDO_ALL_OFF), TAG, "ldo off 1");
    ESP_RETURN_ON_ERROR(axp2101_write(AXP2101_REG_DCDC1_VOLTAGE, AXP2101_DCDC1_3V3), TAG, "dc1 voltage");
    ESP_RETURN_ON_ERROR(axp2101_write(AXP2101_REG_ALDO1_VOLTAGE, AXP2101_ALDO1_3V3), TAG, "aldo1 voltage");
    ESP_RETURN_ON_ERROR(axp2101_write(AXP2101_REG_LDO_ONOFF_0, AXP2101_LDO_ALDO1_ON), TAG, "aldo1 on");
    ESP_RETURN_ON_ERROR(axp2101_write(AXP2101_REG_CV_VOLTAGE, AXP2101_CV_4V1), TAG, "cv voltage");
    ESP_RETURN_ON_ERROR(axp2101_write(AXP2101_REG_IPRECHG, AXP2101_PRECHARGE_50MA), TAG, "precharge");
    ESP_RETURN_ON_ERROR(axp2101_write(AXP2101_REG_ICC_CHG, AXP2101_CHARGE_CODE), TAG, "charge current");
    ESP_RETURN_ON_ERROR(axp2101_write(AXP2101_REG_ITERM_CHG, AXP2101_TERM_25MA), TAG, "term current");

    return ESP_OK;
}
