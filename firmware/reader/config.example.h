// Copy this file to config.h (same folder) and fill it in. config.h is git-ignored.
#pragma once

// --- network -------------------------------------------------------------------------------------
#define WIFI_SSID "your-hotspot-name"
#define WIFI_PASSWORD "your-hotspot-password"
// The laptop's IP address on that network (not "localhost"), no trailing slash.
#define SERVER_URL "http://192.168.43.10:8000"

// --- identity ------------------------------------------------------------------------------------
// Printed by:  python -m tools.seed --reset
#define DEVICE_ID "reader-1"
#define DEVICE_SECRET_HEX "00112233445566778899aabbccddeeff"

// --- wiring (ESP32 DevKit V1, 30 pin) ------------------------------------------------------------
// PN532 in SPI mode:  SCK -> 18, MISO -> 19, MOSI -> 23, SS -> PN532_SS
#define PN532_SS 5
// SSD1306 OLED on I2C
#define OLED_SDA 21
#define OLED_SCL 22
#define OLED_ADDR 0x3C
// indicators (each LED through a ~330 ohm resistor; buzzer is the active type)
#define LED_OK_PIN 25
#define LED_BAD_PIN 26
#define BUZZER_PIN 27

// --- optional timing (milliseconds), defaults shown ----------------------------------------------
// #define POLL_MS 1000
// #define RESULT_MS 2000
// #define COOLDOWN_MS 1200
// #define HTTP_TIMEOUT_MS 4000
