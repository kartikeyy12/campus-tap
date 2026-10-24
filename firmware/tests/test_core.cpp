// Host-side test of firmware/reader/core.h. The expected values come from the real Python server and
// Python's own hmac/hashlib (see tests/test_firmware_core.py), so this checks the shipped code against it.
//
//   test_core <vectors.tsv>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include <string>
#include <vector>

#include "../reader/core.h"

static int g_pass = 0, g_fail = 0;
static int g_line = 0;
#define CHECK(cond, ...)                                      \
  do {                                                        \
    if (cond) { g_pass++; }                                   \
    else { g_fail++; printf("FAIL line %d: ", g_line); printf(__VA_ARGS__); printf("\n"); } \
  } while (0)

static std::vector<std::string> split(const std::string& s) {
  std::vector<std::string> out;
  size_t start = 0;
  while (true) {
    size_t t = s.find('\t', start);
    if (t == std::string::npos) { out.push_back(s.substr(start)); break; }
    out.push_back(s.substr(start, t - start));
    start = t + 1;
  }
  return out;
}

static std::string unhex(const std::string& h) {
  std::vector<uint8_t> b(h.size() / 2 + 1);
  size_t n = 0;
  if (!core::from_hex(h.c_str(), b.data(), b.size(), &n)) return "<badhex>";
  return std::string((const char*)b.data(), n);
}

// ---------------------------------------------------------------- a software NTAG 424 DNA (NDEF part only)
struct FakeCard {
  std::vector<uint8_t> file;   // 256-byte NDEF file
  int cmd_count = 0;
  int fail_at = -1;            // fail (card removed) when this command index is reached
  int bad_sw_at = -1;          // answer with 6A82 at this index
  bool selected_app = false, selected_file = false;
  bool last_was_read = false;
  int read_runs = 0;           // uninterrupted runs of READ BINARY = how often the card would bump its counter
  int max_le = 0;
  bool bad_order = false;
};

static bool fake_apdu(void* ctx, const uint8_t* cmd, uint8_t clen, uint8_t* resp, uint8_t* rlen) {
  FakeCard* c = (FakeCard*)ctx;
  int idx = c->cmd_count++;
  if (idx == c->fail_at) return false;
  auto sw = [&](uint8_t a, uint8_t b, size_t at) { resp[at] = a; resp[at + 1] = b; *rlen = (uint8_t)(at + 2); return true; };
  if (idx == c->bad_sw_at) return sw(0x6A, 0x82, 0);
  if (clen == 13 && cmd[1] == 0xA4 && cmd[2] == 0x04) {
    static const uint8_t aid[] = {0xD2, 0x76, 0x00, 0x00, 0x85, 0x01, 0x01};
    if (memcmp(cmd + 5, aid, 7) != 0) return sw(0x6A, 0x82, 0);
    c->selected_app = true; c->last_was_read = false;
    return sw(0x90, 0x00, 0);
  }
  if (clen == 7 && cmd[1] == 0xA4 && cmd[2] == 0x00) {
    if (!c->selected_app || cmd[5] != 0xE1 || cmd[6] != 0x04) return sw(0x6A, 0x82, 0);
    c->selected_file = true; c->last_was_read = false;
    return sw(0x90, 0x00, 0);
  }
  if (clen == 5 && cmd[0] == 0x00 && cmd[1] == 0xB0) {
    if (!c->selected_file) return sw(0x69, 0x86, 0);
    if (!c->last_was_read) c->read_runs++;
    c->last_was_read = true;
    size_t off = ((size_t)cmd[2] << 8) | cmd[3];
    size_t le = cmd[4];
    if ((int)le > c->max_le) c->max_le = (int)le;
    if (off + le > c->file.size()) return sw(0x6B, 0x00, 0);
    if (2 + le + 2 > *rlen) return false;   // the PN532 buffer would overflow
    memcpy(resp, c->file.data() + off, le);
    return sw(0x90, 0x00, le);
  }
  c->bad_order = true;
  return sw(0x6D, 0x00, 0);
}

static void ndef_case(const std::string& file_hex) {
  std::string raw = unhex(file_hex);
  size_t nlen = ((uint8_t)raw[0] << 8) | (uint8_t)raw[1];
  size_t total = 2 + nlen;
  CHECK(raw.size() == total, "vector file has wrong length");

  FakeCard base;
  base.file.assign(256, 0);
  memcpy(base.file.data(), raw.data(), raw.size());

  // happy path
  {
    FakeCard c = base;
    uint8_t out[128]; size_t n = 0;
    bool ok = core::read_ndef(fake_apdu, &c, out, sizeof out, &n);
    CHECK(ok && n == total && memcmp(out, raw.data(), total) == 0, "ndef read mismatch (ok=%d n=%zu)", ok, n);
    CHECK(c.read_runs == 1, "reads must be one uninterrupted run, got %d", c.read_runs);
    CHECK(!c.bad_order, "unexpected command sent");
    CHECK(c.max_le <= 32, "read too large for the PN532 buffer: %d", c.max_le);
  }
  // buffer too small -> clean failure
  {
    FakeCard c = base;
    std::vector<uint8_t> small(total - 1);
    size_t n = 0;
    CHECK(!core::read_ndef(fake_apdu, &c, small.data(), small.size(), &n), "small buffer must fail");
  }
  // card removed / error status at every possible command
  FakeCard probe = base;
  uint8_t out[128]; size_t n = 0;
  core::read_ndef(fake_apdu, &probe, out, sizeof out, &n);
  for (int k = 0; k < probe.cmd_count; k++) {
    FakeCard c1 = base; c1.fail_at = k;
    CHECK(!core::read_ndef(fake_apdu, &c1, out, sizeof out, &n), "removal at command %d must fail", k);
    FakeCard c2 = base; c2.bad_sw_at = k;
    CHECK(!core::read_ndef(fake_apdu, &c2, out, sizeof out, &n), "bad status at command %d must fail", k);
  }
  // a file that claims an absurd length
  {
    FakeCard c = base;
    c.file[0] = 0xFF; c.file[1] = 0xFF;
    CHECK(!core::read_ndef(fake_apdu, &c, out, sizeof out, &n), "absurd NLEN must fail");
    FakeCard d = base;
    d.file[0] = 0x00; d.file[1] = 0x02;
    CHECK(!core::read_ndef(fake_apdu, &d, out, sizeof out, &n), "tiny NLEN must fail");
  }
}

int main(int argc, char** argv) {
  if (argc < 2) { fprintf(stderr, "usage: %s vectors.tsv\n", argv[0]); return 2; }
  FILE* f = fopen(argv[1], "r");
  if (!f) { perror("vectors"); return 2; }
  static char line[8192];
  while (fgets(line, sizeof line, f)) {
    g_line++;
    size_t L = strlen(line);
    while (L && (line[L - 1] == '\n' || line[L - 1] == '\r')) line[--L] = 0;
    if (!L || line[0] == '#') continue;
    std::vector<std::string> t = split(line);
    const std::string& kind = t[0];

    if (kind == "HMAC") {
      std::string key = unhex(t[1]), msg = unhex(t[2]);
      char out[65];
      core::hmac_sha256_hex((const uint8_t*)key.data(), key.size(), (const uint8_t*)msg.data(), msg.size(), out);
      CHECK(t[3] == out, "hmac keylen=%zu msglen=%zu got %s want %s", key.size(), msg.size(), out, t[3].c_str());

    } else if (kind == "BODY") {
      std::string ndef = unhex(t[1]);
      bool has = t[2] != "-";
      long cid = has ? atol(t[2].c_str()) : 0;
      char out[1024];
      size_t n = core::build_tap_body((const uint8_t*)ndef.data(), ndef.size(), has, cid, out, sizeof out);
      CHECK(n == t[3].size() && t[3] == out, "body mismatch: got %s", out);
      char tiny[20];
      CHECK(core::build_tap_body((const uint8_t*)ndef.data(), ndef.size(), has, cid, tiny, sizeof tiny) == 0,
            "body must refuse a buffer that is too small");

    } else if (kind == "REPLY") {
      core::Reply r;
      bool ok = core::parse_reply(t[1].c_str(), &r);
      CHECK(ok, "reply should parse: %s", t[1].c_str());
      if (ok) {
        CHECK(r.ok == (t[2] == "1"), "ok flag");
        CHECK(t[3] == r.code, "code got %s want %s", r.code, t[3].c_str());
        CHECK(t[4] == r.big, "big got '%s' want '%s'", r.big, t[4].c_str());
        CHECK(t[5] == r.l1, "l1 got '%s' want '%s'", r.l1, t[5].c_str());
        CHECK(t[6] == r.l2, "l2 got '%s' want '%s'", r.l2, t[6].c_str());
        CHECK(t[7] == r.tone, "tone got %s want %s", r.tone, t[7].c_str());
      }

    } else if (kind == "REPLY_BAD") {
      core::Reply r;
      CHECK(!core::parse_reply(t[1].c_str(), &r), "must reject: %s", t[1].c_str());

    } else if (kind == "POLL") {
      core::Poll p;
      bool ok = core::parse_poll(t[1].c_str(), &p);
      CHECK(ok, "poll should parse: %s", t[1].c_str());
      if (ok) {
        CHECK(t[2] == p.mode, "mode got %s want %s", p.mode, t[2].c_str());
        CHECK(t[3] == p.title, "title got '%s' want '%s'", p.title, t[3].c_str());
        CHECK(t[4] == p.vendor, "vendor got '%s' want '%s'", p.vendor, t[4].c_str());
        CHECK(p.has_charge == (t[5] == "1"), "has_charge");
        CHECK(p.charge_id == atol(t[6].c_str()), "charge id %ld", p.charge_id);
        CHECK(p.amount == atol(t[7].c_str()), "amount %ld", p.amount);
      }

    } else if (kind == "POLL_BAD") {
      core::Poll p;
      CHECK(!core::parse_poll(t[1].c_str(), &p), "must reject poll: %s", t[1].c_str());

    } else if (kind == "SCREEN") {   // json, age, big, l1, l2, charge_id or -
      core::Poll p;
      if (!core::parse_poll(t[1].c_str(), &p)) { CHECK(false, "screen poll parse"); continue; }
      double age = atof(t[2].c_str());
      core::Screen s;
      core::compose_idle(p, age, &s);
      CHECK(t[3] == s.big, "big got '%s' want '%s'", s.big, t[3].c_str());
      CHECK(t[4] == s.l1, "l1 got '%s' want '%s'", s.l1, t[4].c_str());
      CHECK(t[5] == s.l2, "l2 got '%s' want '%s'", s.l2, t[5].c_str());
      CHECK(strlen(s.big) <= 10 && strlen(s.l1) <= 21 && strlen(s.l2) <= 21, "text too long for the display");
      long id = -1;
      bool has = core::displayed_charge(p, age, &id);
      CHECK(has == (t[6] != "-") && (!has || id == atol(t[6].c_str())), "displayed charge id");

    } else if (kind == "NDEF") {
      ndef_case(t[1]);

    } else {
      CHECK(false, "unknown vector kind %s", kind.c_str());
    }
  }
  fclose(f);
  printf("PASS %d FAIL %d\n", g_pass, g_fail);
  return g_fail == 0 ? 0 : 1;
}
