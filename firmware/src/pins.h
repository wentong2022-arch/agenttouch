// Copyright (c) 2026 yuwentong
// Licensed under PolyForm Noncommercial 1.0.0 (see LICENSE)
// Waveshare ESP32-S3-Touch-AMOLED-2.16 pin map (verified against schematic
// and the claude-desktop-buddy-esp32 port).
#pragma once

// AMOLED CO5300 over QSPI
#define PIN_LCD_SDIO0  4
#define PIN_LCD_SDIO1  5
#define PIN_LCD_SDIO2  6
#define PIN_LCD_SDIO3  7
#define PIN_LCD_SCLK   38
#define PIN_LCD_CS     12
#define PIN_LCD_RESET  39
#define PIN_TP_RESET   40
#define PIN_TP_INT     11

// Shared I2C bus: CST92xx touch, AXP2101 PMU, QMI8658 IMU, PCF85063 RTC, codecs
#define PIN_I2C_SDA    15
#define PIN_I2C_SCL    14

// Physical keys (user naming: PWR=middle, IO18=right/voice, BOOT=left)
#define PIN_KEY_PWR    16   // NOT a key sense line: GPIO16 is SYS_OUT (never moves when PWR is pressed, measured 2026-09-19). The middle key reaches the firmware only through the AXP2101 PWRON edge IRQs (pmuKeyScan in main.cpp).
#define PIN_KEY_IO18   18   // right key, active LOW (10K pull-up)
#define PIN_KEY_BOOT   0    // left key, active LOW

// Audio: ES8311 codec (I2C 0x18 on the shared bus) + speaker PA
#define PIN_I2S_MCLK   42
#define PIN_I2S_BCLK   9
#define PIN_I2S_LRCK   45
#define PIN_I2S_DOUT   8    // ESP32 -> ES8311 (playback)
#define PIN_I2S_DIN    10   // ES7210 -> ESP32 (mic capture; shares BCLK/LRCK/MCLK with the ES8311)
#define PIN_PA_CTRL    46   // speaker amp enable, active HIGH

#define LCD_W 480
#define LCD_H 480

// microSD over SPI (SPI3 host — the AMOLED QSPI owns SPI2). Schematic: doc/01.
#define PIN_SD_MOSI    1
#define PIN_SD_SCK     2
#define PIN_SD_MISO    3
#define PIN_SD_CS      41
