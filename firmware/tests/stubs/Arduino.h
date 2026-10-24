// Minimal stand-ins for the Arduino/ESP32 API, only so reader.ino can be compiled on a PC as a check.
// They mirror the real signatures as documented; they do not prove behaviour on a board.
#pragma once
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <string>

#define HIGH 1
#define LOW 0
#define OUTPUT 1
inline unsigned long millis() { return 0; }
inline void delay(unsigned long) {}
inline void pinMode(int, int) {}
inline void digitalWrite(int, int) {}

class String {
 public:
  String() {}
  String(const char* s) : s_(s) {}
  String operator+(const char* o) const { return String((s_ + o).c_str()); }
  const char* c_str() const { return s_.c_str(); }
 private:
  std::string s_;
};

struct SerialStub {
  void begin(unsigned long) {}
  void println(const char*) {}
};
static SerialStub Serial;

struct EspStub { void restart() {} };
static EspStub ESP;
