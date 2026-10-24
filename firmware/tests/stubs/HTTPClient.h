#pragma once
#include "Arduino.h"
class HTTPClient {
 public:
  bool begin(String) { return true; }
  void setConnectTimeout(uint16_t) {}
  void setTimeout(uint16_t) {}
  void addHeader(const String&, const String&) {}
  int POST(uint8_t*, size_t) { return 200; }
  int GET() { return 200; }
  String getString() { return String(); }
  void end() {}
};
