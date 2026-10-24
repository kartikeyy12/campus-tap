#pragma once
#include "SPI.h"
#define PN532_MIFARE_ISO14443A 0x00
class Adafruit_PN532 {
 public:
  Adafruit_PN532(uint8_t, SPIClass* = &SPI) {}
  void begin() {}
  uint32_t getFirmwareVersion() { return 1; }
  bool SAMConfig() { return true; }
  bool readPassiveTargetID(uint8_t, uint8_t*, uint8_t*, uint16_t = 0) { return false; }
  bool inDataExchange(uint8_t*, uint8_t, uint8_t*, uint8_t*) { return false; }
};
