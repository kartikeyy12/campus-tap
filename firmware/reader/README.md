# Reader firmware (ESP32 + PN532 + OLED)

The reader makes no decisions. It reads the card, sends the signed message to the server, and shows the
answer. Balance, blocked cards, attendance and replay checks all live on the server.

## Status, honestly

| Part | Verified how |
|---|---|
| HMAC signing, request body, reply/poll parsing, screen text, card-read command sequence (`core.h`) | Compiled on a PC with sanitizers and checked against the real server and Python's `hmac` (`tests/test_firmware_core.py`) |
| `reader.ino` | Compiles against stub headers only. **Not run on a board yet.** |
| PN532 reading an NTAG 424 DNA through `inDataExchange` | **Not tested.** First thing to try when the hardware arrives |

## Parts and wiring (ESP32 DevKit V1, 30 pin)

| PN532 (SPI mode) | ESP32 |
|---|---|
| VCC | 3V3 (the V3 board accepts 3.3 V or 5 V; use 3.3 V to match the logic) |
| GND | GND |
| SCK / MISO / MOSI / SS | GPIO 18 / 19 / 23 / 5 |

Set the two DIP switches on the PN532 board to **SPI** (check the labels printed on the board; they differ
between versions). I2C mode is known to hang with the ESP32, so SPI is used.

| OLED (SSD1306, I2C) | ESP32 |
|---|---|
| VCC / GND | 3V3 / GND |
| SDA / SCL | GPIO 21 / 22 |

Green LED on GPIO 25, red LED on GPIO 26 (each with a ~330 ohm resistor to GND), active buzzer on GPIO 27.
All pins can be changed in `config.h`.

## Build

1. Arduino IDE (or PlatformIO) with the **ESP32** board package.
2. Libraries: `Adafruit PN532`, `Adafruit SSD1306`, `Adafruit GFX`.
3. Copy `config.example.h` to `config.h` and fill it in. The reader secret is printed by
   `python -m tools.seed --reset` on the server.
4. Open `reader.ino` (keep `core.h` next to it), pick board "ESP32 Dev Module", upload.

## How a tap works

1. Idle: polls `GET /api/device/poll` once a second and shows the mode, session title or vendor charge.
2. A card appears: reads the NDEF file with ISO 7816 commands. After the two SELECTs only READ BINARY
   commands are sent, in chunks of 32 bytes. The card bumps its read counter once per uninterrupted run
   of reads (NXP NT4H2421Gx datasheet, section 9.3.1), so all chunks carry the same counter.
3. Sends `{"ndef": "<hex>", "charge_id": <the charge on screen or null>}` signed with HMAC-SHA256.
4. Shows the server's answer for 2 seconds (green or red LED, beep pattern from the server's tone).
5. Waits until the card has left the field before it accepts another one.

If the network fails mid-tap, the same bytes are sent again. The server answers a repeat with the
original result, so nothing is charged twice. If there is still no reply the screen says
"NO REPLY / Result unknown" instead of guessing.

## Checking it on the bench (in this order)

1. Power on with no network config problems: OLED shows `BOOT`, then `NO PN532` if the SPI wiring or DIP
   switches are wrong, then `WIFI`, then the idle screen.
2. Start the server and a session; the OLED should show the mode and title.
3. Tap an unregistered card: `NEW CARD / Not registered`. That proves reading and signature checking work.
4. Register it on the admin page, tap again.
