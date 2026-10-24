#pragma once
#include "Arduino.h"
#define WL_CONNECTED 3
#define WIFI_STA 1
struct WiFiStub {
  void mode(int) {}
  void setAutoReconnect(bool) {}
  void begin(const char*, const char*) {}
  void disconnect() {}
  int status() { return WL_CONNECTED; }
};
static WiFiStub WiFi;
