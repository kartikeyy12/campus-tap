// Hardware-free logic for the reader. Nothing here touches Arduino, WiFi or the PN532, so the exact
// same code is compiled and tested on a PC (firmware/tests) and then shipped to the ESP32.
//
// Contents: SHA-256 / HMAC signing, hex helpers, request/response JSON, the card-reading APDU
// sequence, and the screen text for each mode.
#pragma once

#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

namespace core {

// ------------------------------------------------------------------ SHA-256 and HMAC-SHA256
struct Sha256 {
  uint32_t h[8];
  uint8_t buf[64];
  uint64_t total;
  size_t blen;

  static uint32_t rotr(uint32_t x, int n) { return (x >> n) | (x << (32 - n)); }

  void init() {
    static const uint32_t H0[8] = {0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a,
                                   0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19};
    memcpy(h, H0, sizeof h);
    total = 0;
    blen = 0;
  }

  void block(const uint8_t* p) {
    static const uint32_t K[64] = {
        0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
        0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
        0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
        0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
        0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
        0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
        0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
        0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2};
    uint32_t w[64];
    for (int i = 0; i < 16; i++)
      w[i] = ((uint32_t)p[4 * i] << 24) | ((uint32_t)p[4 * i + 1] << 16) | ((uint32_t)p[4 * i + 2] << 8) |
             (uint32_t)p[4 * i + 3];
    for (int i = 16; i < 64; i++) {
      uint32_t s0 = rotr(w[i - 15], 7) ^ rotr(w[i - 15], 18) ^ (w[i - 15] >> 3);
      uint32_t s1 = rotr(w[i - 2], 17) ^ rotr(w[i - 2], 19) ^ (w[i - 2] >> 10);
      w[i] = w[i - 16] + s0 + w[i - 7] + s1;
    }
    uint32_t a = h[0], b = h[1], c = h[2], d = h[3], e = h[4], f = h[5], g = h[6], hh = h[7];
    for (int i = 0; i < 64; i++) {
      uint32_t S1 = rotr(e, 6) ^ rotr(e, 11) ^ rotr(e, 25);
      uint32_t ch = (e & f) ^ (~e & g);
      uint32_t t1 = hh + S1 + ch + K[i] + w[i];
      uint32_t S0 = rotr(a, 2) ^ rotr(a, 13) ^ rotr(a, 22);
      uint32_t mj = (a & b) ^ (a & c) ^ (b & c);
      uint32_t t2 = S0 + mj;
      hh = g; g = f; f = e; e = d + t1; d = c; c = b; b = a; a = t1 + t2;
    }
    h[0] += a; h[1] += b; h[2] += c; h[3] += d; h[4] += e; h[5] += f; h[6] += g; h[7] += hh;
  }

  void update(const uint8_t* data, size_t n) {
    total += n;
    while (n > 0) {
      size_t take = 64 - blen;
      if (take > n) take = n;
      memcpy(buf + blen, data, take);
      blen += take;
      data += take;
      n -= take;
      if (blen == 64) {
        block(buf);
        blen = 0;
      }
    }
  }

  void final(uint8_t out[32]) {
    uint64_t bits = total * 8;
    uint8_t pad = 0x80;
    update(&pad, 1);
    uint8_t zero = 0;
    while (blen != 56) update(&zero, 1);
    uint8_t len[8];
    for (int i = 0; i < 8; i++) len[i] = (uint8_t)(bits >> (56 - 8 * i));
    update(len, 8);
    for (int i = 0; i < 8; i++) {
      out[4 * i] = (uint8_t)(h[i] >> 24);
      out[4 * i + 1] = (uint8_t)(h[i] >> 16);
      out[4 * i + 2] = (uint8_t)(h[i] >> 8);
      out[4 * i + 3] = (uint8_t)h[i];
    }
  }
};

inline void to_hex(const uint8_t* in, size_t n, char* out) {  // out needs 2n+1 bytes, lower case
  static const char* d = "0123456789abcdef";
  for (size_t i = 0; i < n; i++) {
    out[2 * i] = d[in[i] >> 4];
    out[2 * i + 1] = d[in[i] & 15];
  }
  out[2 * n] = 0;
}

inline void to_hex_upper(const uint8_t* in, size_t n, char* out) {
  static const char* d = "0123456789ABCDEF";
  for (size_t i = 0; i < n; i++) {
    out[2 * i] = d[in[i] >> 4];
    out[2 * i + 1] = d[in[i] & 15];
  }
  out[2 * n] = 0;
}

inline int nib(char c) {
  if (c >= '0' && c <= '9') return c - '0';
  if (c >= 'a' && c <= 'f') return c - 'a' + 10;
  if (c >= 'A' && c <= 'F') return c - 'A' + 10;
  return -1;
}

inline bool from_hex(const char* hex, uint8_t* out, size_t cap, size_t* n) {
  size_t len = strlen(hex);
  if (len % 2 != 0 || len / 2 > cap) return false;
  for (size_t i = 0; i < len / 2; i++) {
    int a = nib(hex[2 * i]), b = nib(hex[2 * i + 1]);
    if (a < 0 || b < 0) return false;
    out[i] = (uint8_t)(a * 16 + b);
  }
  *n = len / 2;
  return true;
}

// HMAC-SHA256, result as 64 lower-case hex chars plus terminator.
inline void hmac_sha256_hex(const uint8_t* key, size_t klen, const uint8_t* msg, size_t mlen, char out[65]) {
  uint8_t k[64] = {0};
  if (klen > 64) {
    Sha256 t;
    t.init();
    t.update(key, klen);
    t.final(k);  // 32 bytes, rest stays zero
  } else {
    memcpy(k, key, klen);
  }
  uint8_t ipad[64], opad[64];
  for (int i = 0; i < 64; i++) {
    ipad[i] = k[i] ^ 0x36;
    opad[i] = k[i] ^ 0x5c;
  }
  uint8_t inner[32], mac[32];
  Sha256 s;
  s.init(); s.update(ipad, 64); s.update(msg, mlen); s.final(inner);
  s.init(); s.update(opad, 64); s.update(inner, 32); s.final(mac);
  to_hex(mac, 32, out);
}

// ------------------------------------------------------------------ request body
// {"ndef":"<hex>","charge_id":<number|null>}.  Returns length, or 0 if it does not fit.
inline size_t build_tap_body(const uint8_t* ndef, size_t n, bool has_charge, long charge_id, char* out, size_t cap) {
  const char* head = "{\"ndef\":\"";
  size_t need = strlen(head) + 2 * n + 40;
  if (cap < need + 1) return 0;
  size_t pos = 0;
  memcpy(out + pos, head, strlen(head));
  pos += strlen(head);
  to_hex_upper(ndef, n, out + pos);
  pos += 2 * n;
  int w;
  if (has_charge) w = snprintf(out + pos, cap - pos, "\",\"charge_id\":%ld}", charge_id);
  else w = snprintf(out + pos, cap - pos, "\",\"charge_id\":null}");
  if (w < 0 || (size_t)w >= cap - pos) return 0;
  return pos + (size_t)w;
}

// ------------------------------------------------------------------ minimal JSON reading
// The server's replies are small, flat objects (one nested object for "charge"). These helpers look up a
// top-level key, never descend into nested values, and copy text safely into fixed buffers.

// `s` must point at '{'. Returns a pointer to the value of top-level `key`, or nullptr.
inline const char* json_find(const char* s, const char* key) {
  if (!s || *s != '{') return nullptr;
  int depth = 0;
  size_t klen = strlen(key);
  while (*s) {
    char c = *s;
    if (c == '"') {
      const char* start = s + 1;
      const char* e = start;
      while (*e && *e != '"') {
        if (*e == '\\' && e[1]) e++;
        e++;
      }
      if (!*e) return nullptr;
      s = e + 1;
      if (depth == 1) {
        const char* p = s;
        while (*p == ' ') p++;
        if (*p == ':') {
          if ((size_t)(e - start) == klen && strncmp(start, key, klen) == 0) {
            p++;
            while (*p == ' ') p++;
            return p;
          }
        }
      }
      continue;
    }
    if (c == '{' || c == '[') depth++;
    else if (c == '}' || c == ']') depth--;
    s++;
  }
  return nullptr;
}

// Copy a JSON string (v points at the opening quote) into out as printable ASCII.
// Non-ASCII bytes and \uXXXX escapes become '?'; output is always terminated and never overflows.
inline bool json_str(const char* v, char* out, size_t cap) {
  if (!v || *v != '"' || cap == 0) return false;
  size_t o = 0;
  const char* p = v + 1;
  auto put = [&](char c) {
    if (o + 1 < cap) out[o++] = c;
  };
  while (*p && *p != '"') {
    unsigned char c = (unsigned char)*p;
    if (c == '\\') {
      p++;
      char e = *p;
      if (!e) return false;
      if (e == 'u') {
        put('?');
        for (int i = 0; i < 4 && p[1]; i++) p++;
      } else if (e == 'n' || e == 't' || e == 'r' || e == 'b' || e == 'f') {
        put(' ');
      } else {
        put(e);  // \" \\ \/
      }
      p++;
      continue;
    }
    if (c >= 0xC0) put('?');         // start of a multi-byte character
    else if (c >= 0x80) { /* continuation byte, skip */ }
    else if (c >= 0x20 && c < 0x7F) put((char)c);
    p++;
  }
  if (*p != '"') return false;
  out[o] = 0;
  return true;
}

inline bool json_bool(const char* v, bool* out) {
  if (!v) return false;
  if (strncmp(v, "true", 4) == 0) { *out = true; return true; }
  if (strncmp(v, "false", 5) == 0) { *out = false; return true; }
  return false;
}

inline bool json_long(const char* v, long* out) {
  if (!v) return false;
  char* end = nullptr;
  long x = strtol(v, &end, 10);
  if (end == v) return false;
  *out = x;
  return true;
}

inline bool json_double(const char* v, double* out) {
  if (!v) return false;
  char* end = nullptr;
  double x = strtod(v, &end);
  if (end == v) return false;
  *out = x;
  return true;
}

// ------------------------------------------------------------------ server replies
struct Reply {          // answer to a tap
  bool ok;
  char code[20];
  char big[16];
  char l1[32];
  char l2[32];
  char tone[8];         // "ok", "deny" or "warn"
};

inline bool parse_reply(const char* json, Reply* r) {
  memset(r, 0, sizeof *r);
  if (!json_bool(json_find(json, "ok"), &r->ok)) return false;
  if (!json_str(json_find(json, "code"), r->code, sizeof r->code)) return false;
  if (!json_str(json_find(json, "big"), r->big, sizeof r->big)) return false;
  if (!json_str(json_find(json, "l1"), r->l1, sizeof r->l1)) return false;
  if (!json_str(json_find(json, "l2"), r->l2, sizeof r->l2)) return false;
  if (!json_str(json_find(json, "tone"), r->tone, sizeof r->tone)) return false;
  return true;
}

struct Poll {           // answer to the once-a-second poll
  char mode[12];        // idle | attendance | mess | vendor
  char title[48];
  char vendor[48];
  bool has_charge;
  long charge_id;
  long amount;
  double ttl;
};

inline bool parse_poll(const char* json, Poll* p) {
  memset(p, 0, sizeof *p);
  if (!json_str(json_find(json, "mode"), p->mode, sizeof p->mode)) return false;
  json_str(json_find(json, "title"), p->title, sizeof p->title);
  json_str(json_find(json, "vendor"), p->vendor, sizeof p->vendor);
  const char* ch = json_find(json, "charge");
  if (ch && *ch == '{') {
    if (!json_long(json_find(ch, "id"), &p->charge_id)) return false;
    if (!json_long(json_find(ch, "amount"), &p->amount)) return false;
    if (!json_double(json_find(ch, "ttl"), &p->ttl)) p->ttl = 0;
    p->has_charge = p->amount > 0;
  }
  return true;
}

// ------------------------------------------------------------------ reading the card
// Transport: send one APDU, get the response (data + SW1 SW2). `*rlen` is the buffer size on entry and the
// response length on return.
typedef bool (*ApduFn)(void* ctx, const uint8_t* cmd, uint8_t clen, uint8_t* resp, uint8_t* rlen);

static const uint8_t NDEF_CHUNK = 32;  // the PN532 library's buffer is 64 bytes, so keep reads well below it

inline bool sw_ok(const uint8_t* r, uint8_t n) { return n >= 2 && r[n - 2] == 0x90 && r[n - 1] == 0x00; }

// Reads the NDEF file (2-byte length + message) into out. Returns false on any failure.
//
// IMPORTANT: after the two SELECT commands, only READ BINARY commands may be sent. The card bumps its
// read counter once per uninterrupted run of reads (NT4H2421Gx datasheet, section 9.3.1), so a run of
// reads shares one counter value and one set of mirrored values. Any other command in between would
// start a new run, and the encrypted ID and the MAC would then come from different counters.
inline bool read_ndef(ApduFn fn, void* ctx, uint8_t* out, size_t cap, size_t* n) {
  uint8_t resp[64];
  uint8_t rl;

  static const uint8_t sel_app[] = {0x00, 0xA4, 0x04, 0x00, 0x07, 0xD2, 0x76, 0x00, 0x00, 0x85, 0x01, 0x01, 0x00};
  rl = sizeof resp;
  if (!fn(ctx, sel_app, sizeof sel_app, resp, &rl) || !sw_ok(resp, rl)) return false;

  static const uint8_t sel_file[] = {0x00, 0xA4, 0x00, 0x0C, 0x02, 0xE1, 0x04};
  rl = sizeof resp;
  if (!fn(ctx, sel_file, sizeof sel_file, resp, &rl) || !sw_ok(resp, rl)) return false;

  uint8_t rd[5] = {0x00, 0xB0, 0x00, 0x00, 0x02};   // first two bytes: NLEN
  rl = sizeof resp;
  if (!fn(ctx, rd, sizeof rd, resp, &rl) || rl != 4 || !sw_ok(resp, rl)) return false;
  size_t nlen = ((size_t)resp[0] << 8) | resp[1];
  size_t total = 2 + nlen;
  if (nlen < 5 || total > cap || total > 0x7FFF) return false;
  out[0] = resp[0];
  out[1] = resp[1];

  size_t off = 2;
  while (off < total) {
    size_t chunk = total - off;
    if (chunk > NDEF_CHUNK) chunk = NDEF_CHUNK;
    rd[2] = (uint8_t)(off >> 8);
    rd[3] = (uint8_t)(off & 0xFF);
    rd[4] = (uint8_t)chunk;
    rl = sizeof resp;
    if (!fn(ctx, rd, sizeof rd, resp, &rl) || rl != chunk + 2 || !sw_ok(resp, rl)) return false;
    memcpy(out + off, resp, chunk);
    off += chunk;
  }
  *n = total;
  return true;
}

// ------------------------------------------------------------------ what the screen says
struct Screen {
  char big[16];   // large text, up to 10 characters
  char l1[32];    // small text, up to 21 characters
  char l2[32];
};

inline void put_screen(Screen* s, const char* big, const char* l1, const char* l2) {
  snprintf(s->big, sizeof s->big, "%.10s", big);
  snprintf(s->l1, sizeof s->l1, "%.21s", l1);
  snprintf(s->l2, sizeof s->l2, "%.21s", l2);
}

// Idle screen for the current mode. `age_s` is how many seconds ago the poll answer arrived.
inline void compose_idle(const Poll& p, double age_s, Screen* s) {
  char buf[32];
  if (strcmp(p.mode, "attendance") == 0) {
    put_screen(s, "ATTEND", p.title, "Tap your card");
  } else if (strcmp(p.mode, "mess") == 0) {
    put_screen(s, "MESS", p.title, "Tap your card");
  } else if (strcmp(p.mode, "vendor") == 0) {
    if (p.has_charge && p.ttl - age_s > 0) {
      snprintf(buf, sizeof buf, "Rs %ld", p.amount);
      char l2[32];
      double left = p.ttl - age_s;
      int secs = (int)left;
      if (left > secs) secs++;   // round up: 17.0005 s left shows 18
      snprintf(l2, sizeof l2, "Tap to pay  %ds", secs);
      put_screen(s, buf, p.vendor, l2);
    } else {
      put_screen(s, "VENDOR", p.vendor, "Waiting for amount");
    }
  } else {
    put_screen(s, "IDLE", "Reader not in use", "Ask the admin");
  }
}

// The charge id to send with a tap: only while an unexpired charge is on screen.
inline bool displayed_charge(const Poll& p, double age_s, long* id) {
  if (strcmp(p.mode, "vendor") == 0 && p.has_charge && p.ttl - age_s > 0) {
    *id = p.charge_id;
    return true;
  }
  return false;
}

}  // namespace core
