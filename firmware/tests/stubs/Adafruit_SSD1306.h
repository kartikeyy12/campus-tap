#pragma once
#include "Adafruit_GFX.h"
#include "Wire.h"
#define SSD1306_WHITE 1
#define SSD1306_SWITCHCAPVCC 0x02
class Adafruit_SSD1306 {
 public:
  Adafruit_SSD1306(uint8_t, uint8_t, TwoWire*, int8_t) {}
  bool begin(uint8_t, uint8_t) { return true; }
  void clearDisplay() {}
  void display() {}
  void setTextColor(uint16_t) {}
  void setTextSize(uint8_t) {}
  void setCursor(int16_t, int16_t) {}
  size_t print(const char*) { return 0; }
};
