// Campus card reader: ESP32 + PN532 (SPI) + SSD1306 OLED.
//
// The reader makes no decisions. It reads the card's signed message, sends it to the server, and shows
// whatever the server answers. All rules (balance, blocked cards, attendance, replay checks) live on the
// server. See core.h for the logic that is tested on a PC, and README.md for wiring and libraries.
//
// STATUS: written without access to the hardware. The logic in core.h is tested; the calls into the
// Adafruit libraries below have not been run on a real board yet.

#include <Arduino.h>
#include <HTTPClient.h>
#include <SPI.h>
#include <WiFi.h>
#include <Wire.h>

#include <Adafruit_GFX.h>
#include <Adafruit_PN532.h>
#include <Adafruit_SSD1306.h>

#include "config.h"
#include "core.h"

#ifndef POLL_MS
#define POLL_MS 1000
#endif
#ifndef RESULT_MS
#define RESULT_MS 2000
#endif
#ifndef COOLDOWN_MS
#define COOLDOWN_MS 1200
#endif
#ifndef HTTP_TIMEOUT_MS
#define HTTP_TIMEOUT_MS 4000
#endif
#define SERVER_DOWN_AFTER 3      // failed polls in a row before the reader stops accepting cards
#define ABSENT_POLLS 3           // "no card" readings needed before the next card is accepted
#define HEALTH_EVERY_MS 30000UL  // how often the PN532 is checked while idle
#define WIFI_RETRY_MS 10000UL
#define WIFI_RESTART_AFTER_MS 120000UL

enum State { ST_IDLE, ST_SHOW };

static Adafruit_SSD1306 oled(128, 64, &Wire, -1);
static Adafruit_PN532 nfc(PN532_SS);

static uint8_t g_secret[16];
static State g_state = ST_IDLE;
static unsigned long g_show_until = 0;
static unsigned long g_cooldown_until = 0;
static bool g_wait_removal = false;   // after every result, wait until the card has left the field
static int g_absent = 0;

static core::Poll g_poll;
static bool g_poll_valid = false;
static bool g_polled = false;
static unsigned long g_last_poll = 0;
static unsigned long g_poll_at = 0;   // millis() when g_poll arrived
static int g_poll_fails = 0;
static bool g_auth_bad = false;

static unsigned long g_wifi_down_since = 0;
static unsigned long g_wifi_last_try = 0;
static unsigned long g_last_health = 0;

static core::Screen g_shown;
static bool g_shown_valid = false;

// ------------------------------------------------------------------ display and indicators
static void draw(const core::Screen& s) {
  if (g_shown_valid && memcmp(&g_shown, &s, sizeof s) == 0) return;   // nothing changed, avoid flicker
  g_shown = s;
  g_shown_valid = true;
  oled.clearDisplay();
  oled.setTextColor(SSD1306_WHITE);
  oled.setTextSize(2);
  oled.setCursor(0, 0);
  oled.print(s.big);
  oled.setTextSize(1);
  oled.setCursor(0, 26);
  oled.print(s.l1);
  oled.setCursor(0, 38);
  oled.print(s.l2);
  oled.setCursor(0, 56);
  oled.print(DEVICE_ID);
  oled.display();
}

static void message(const char* big, const char* l1, const char* l2) {
  core::Screen s;
  memset(&s, 0, sizeof s);
  core::put_screen(&s, big, l1, l2);
  draw(s);
}

static void lights_off() {
  digitalWrite(LED_OK_PIN, LOW);
  digitalWrite(LED_BAD_PIN, LOW);
}

static void beep(int ms) {
  digitalWrite(BUZZER_PIN, HIGH);
  delay(ms);
  digitalWrite(BUZZER_PIN, LOW);
}

// LED shows the verdict, the beep pattern shows the tone the server chose.
static void feedback(bool ok, const char* tone) {
  digitalWrite(LED_OK_PIN, ok ? HIGH : LOW);
  digitalWrite(LED_BAD_PIN, ok ? LOW : HIGH);
  if (strcmp(tone, "ok") == 0) {
    beep(80);
  } else if (strcmp(tone, "warn") == 0) {
    beep(60);
    delay(60);
    beep(60);
  } else {
    beep(250);
    delay(80);
    beep(250);
  }
}

static void show_result(const core::Screen& s, bool ok, const char* tone) {
  draw(s);
  feedback(ok, tone);
  g_state = ST_SHOW;
  g_show_until = millis() + RESULT_MS;
}

[[noreturn]] static void halt_with(const char* big, const char* l1, const char* l2) {
  message(big, l1, l2);
  while (true) delay(1000);
}

// ------------------------------------------------------------------ talking to the server
struct HttpResult {
  int status;      // HTTP status, or <= 0 when the request itself failed
  String body;
};

// body == nullptr means GET /poll style request (signed message is the fixed word "poll").
static HttpResult http_call(const char* path, const char* body, size_t body_len) {
  HttpResult r;
  r.status = -1;
  const char* signed_msg = body ? body : "poll";
  size_t signed_len = body ? body_len : 4;
  char sig[65];
  core::hmac_sha256_hex(g_secret, sizeof g_secret, (const uint8_t*)signed_msg, signed_len, sig);

  HTTPClient http;
  String url = String(SERVER_URL) + path;
  if (!http.begin(url)) return r;
  http.setConnectTimeout(HTTP_TIMEOUT_MS);
  http.setTimeout(HTTP_TIMEOUT_MS);
  http.addHeader("X-Device-Id", DEVICE_ID);
  http.addHeader("X-Signature", sig);
  if (body) {
    http.addHeader("Content-Type", "application/json");
    r.status = http.POST((uint8_t*)body, body_len);
  } else {
    r.status = http.GET();
  }
  if (r.status > 0) r.body = http.getString();
  http.end();
  return r;
}

static void do_poll() {
  HttpResult r = http_call("/api/device/poll", nullptr, 0);
  core::Poll p;
  if (r.status == 200 && core::parse_poll(r.body.c_str(), &p)) {
    g_poll = p;
    g_poll_valid = true;
    g_poll_at = millis();
    g_poll_fails = 0;
    g_auth_bad = false;
  } else {
    g_poll_fails++;
    if (r.status == 401) g_auth_bad = true;
  }
}

static bool server_ok() { return g_poll_valid && g_poll_fails < SERVER_DOWN_AFTER && !g_auth_bad; }

// ------------------------------------------------------------------ reading a card
static bool apdu(void*, const uint8_t* cmd, uint8_t len, uint8_t* resp, uint8_t* rlen) {
  return nfc.inDataExchange((uint8_t*)cmd, len, resp, rlen);
}

static void try_card() {
  uint8_t uid[7];
  uint8_t uid_len = 0;
  if (!nfc.readPassiveTargetID(PN532_MIFARE_ISO14443A, uid, &uid_len, 60)) return;

  core::Screen s;
  message("READING", "Hold still", "");

  uint8_t ndef[128];
  size_t n = 0;
  if (!core::read_ndef(apdu, nullptr, ndef, sizeof ndef, &n)) {
    memset(&s, 0, sizeof s);
    core::put_screen(&s, "TRY AGAIN", "Card not read", "Hold it steady");
    show_result(s, false, "warn");
    return;
  }

  // Send the charge id that is on screen. If the vendor changed the amount since, the server refuses
  // and shows the new amount instead of charging something the student did not see.
  double age = (millis() - g_poll_at) / 1000.0;
  long charge_id = 0;
  bool has_charge = core::displayed_charge(g_poll, age, &charge_id);

  char body[512];
  size_t blen = core::build_tap_body(ndef, n, has_charge, charge_id, body, sizeof body);
  if (blen == 0) {
    memset(&s, 0, sizeof s);
    core::put_screen(&s, "ERROR", "Card data too big", "Tell the admin");
    show_result(s, false, "warn");
    return;
  }

  HttpResult r = http_call("/api/device/tap", body, blen);
  if (r.status <= 0) {   // network failure: resend the very same bytes; the server answers a repeat with the original result
    delay(300);
    r = http_call("/api/device/tap", body, blen);
  }

  memset(&s, 0, sizeof s);
  core::Reply rep;
  if (r.status == 200 && core::parse_reply(r.body.c_str(), &rep)) {
    core::put_screen(&s, rep.big, rep.l1, rep.l2);
    show_result(s, rep.ok, rep.tone);
  } else if (r.status == 401) {
    core::put_screen(&s, "BAD KEY", "Reader secret wrong", "Check config.h");
    show_result(s, false, "warn");
  } else {
    // The server may or may not have processed the tap, so do not claim either.
    core::put_screen(&s, "NO REPLY", "Result unknown", "Check your balance");
    show_result(s, false, "warn");
  }
}

// ------------------------------------------------------------------ housekeeping
static void nfc_health(unsigned long now) {
  if ((long)(now - g_last_health) < (long)HEALTH_EVERY_MS) return;
  g_last_health = now;
  if (nfc.getFirmwareVersion()) return;
  nfc.begin();
  nfc.SAMConfig();
  if (!nfc.getFirmwareVersion()) ESP.restart();   // reader chip stopped answering: reboot and start clean
}

static void wifi_down(unsigned long now) {
  if (g_wifi_down_since == 0) g_wifi_down_since = now ? now : 1;
  message("WIFI", "Connecting...", WIFI_SSID);
  if ((long)(now - g_wifi_last_try) >= (long)WIFI_RETRY_MS) {
    g_wifi_last_try = now;
    WiFi.disconnect();
    WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  }
  if ((long)(now - g_wifi_down_since) >= (long)WIFI_RESTART_AFTER_MS) ESP.restart();
  delay(200);
}

void setup() {
  Serial.begin(115200);
  pinMode(LED_OK_PIN, OUTPUT);
  pinMode(LED_BAD_PIN, OUTPUT);
  pinMode(BUZZER_PIN, OUTPUT);
  lights_off();
  digitalWrite(BUZZER_PIN, LOW);

  Wire.begin(OLED_SDA, OLED_SCL);
  if (!oled.begin(SSD1306_SWITCHCAPVCC, OLED_ADDR)) Serial.println("OLED not found (continuing without display)");
  message("BOOT", "Starting...", "");

  size_t n = 0;
  if (!core::from_hex(DEVICE_SECRET_HEX, g_secret, sizeof g_secret, &n) || n != sizeof g_secret) {
    halt_with("BAD CONFIG", "DEVICE_SECRET_HEX", "needs 32 hex chars");
  }

  nfc.begin();
  while (!nfc.getFirmwareVersion()) {
    message("NO PN532", "Check wiring and", "the SPI DIP switches");
    delay(1000);
    nfc.begin();
  }
  nfc.SAMConfig();

  WiFi.mode(WIFI_STA);
  WiFi.setAutoReconnect(true);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  g_last_health = millis();
}

void loop() {
  unsigned long now = millis();

  if (WiFi.status() != WL_CONNECTED) {
    wifi_down(now);
    return;
  }
  g_wifi_down_since = 0;

  if (g_state == ST_SHOW) {
    if ((long)(now - g_show_until) < 0) {
      delay(10);
      return;
    }
    lights_off();
    g_state = ST_IDLE;
    g_polled = false;                         // refresh the screen straight away
    g_cooldown_until = now + COOLDOWN_MS;
    g_wait_removal = true;                    // the same card must leave the field before it counts again
    g_absent = 0;
  }

  if (!g_polled || (long)(now - g_last_poll) >= (long)POLL_MS) {
    do_poll();
    g_polled = true;
    g_last_poll = millis();
  }

  if (!server_ok()) {
    message("SERVER?", g_auth_bad ? "Reader key rejected" : "Cannot reach server", "Retrying...");
    delay(50);
    return;
  }

  core::Screen s;
  memset(&s, 0, sizeof s);
  core::compose_idle(g_poll, (millis() - g_poll_at) / 1000.0, &s);
  draw(s);

  if ((long)(now - g_cooldown_until) < 0) {
    delay(10);
    return;
  }

  if (g_wait_removal) {
    uint8_t uid[7];
    uint8_t len = 0;
    if (nfc.readPassiveTargetID(PN532_MIFARE_ISO14443A, uid, &len, 60)) g_absent = 0;
    else if (++g_absent >= ABSENT_POLLS) g_wait_removal = false;
    return;
  }

  try_card();
  nfc_health(now);
}
