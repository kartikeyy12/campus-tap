"""Rehearsal check: plays the whole demo story against a RUNNING server with software cards.

    python -m tools.seed --reset --sim --device-secret 00112233445566778899aabbccddeeff
    uvicorn app.main:app_factory --factory --port 8000        # in another terminal
    python -m tools.smoke --secret 00112233445566778899aabbccddeeff

It changes data (opens sessions, spends credit), so reseed afterwards. Exit code 0 means every step passed.
Once the real reader is connected the same server can be rehearsed by hand; this script is for the software side.
"""
from __future__ import annotations

import argparse
import json
import sys
import time

import httpx

from app.config import Config
from tools.simtap import _load, _save, signed
from tools.simulator import SimCard

CARD_A, ROLL_A = "04AABBCCDDEE11", "24ECE1020"
CARD_B, ROLL_B = "04AABBCCDDEE22", "24ECE1001"


class Smoke:
    def __init__(self, a):
        self.a = a
        self.cfg = Config()
        self.http = httpx.Client(timeout=10, trust_env=False)   # never go through a proxy for the local server
        self.state = _load()
        self.fails = 0
        self.last_ndef: dict[str, bytes] = {}

    # ---- plumbing
    def url(self, path):
        return self.a.server.rstrip("/") + path

    def login(self, role, ident, pin):
        r = self.http.post(self.url("/api/login"), json={"role": role, "ident": ident, "pin": pin})
        r.raise_for_status()
        return {"Authorization": "Bearer " + r.json()["token"]}

    def get(self, path, hdr):
        r = self.http.get(self.url(path), headers=hdr)
        r.raise_for_status()
        return r.json()

    def post(self, path, hdr, body=None):
        r = self.http.post(self.url(path), headers=hdr, json=body or {})
        if r.status_code >= 400:
            raise SystemExit(f"POST {path} -> {r.status_code} {r.text}")
        return r.json()

    def tap(self, uid, again=False, corrupt=False, charge=True):
        poll = signed(self.http, "GET", self.url("/api/device/poll"), self.a.device, self.a.secret)
        if again and uid in self.last_ndef:
            ndef = self.last_ndef[uid]
        else:
            card = SimCard(uid, self.cfg, self.state.get(uid, 0))
            ndef = card.read_ndef()
            self.state[uid] = card.counter
            _save(self.state)
            self.last_ndef[uid] = ndef
        if corrupt:
            b = bytearray(ndef)
            b[-1] = ord("0") if b[-1] != ord("0") else ord("1")
            ndef = bytes(b)
        cid = poll["charge"]["id"] if (charge and poll.get("charge")) else None
        body = json.dumps({"ndef": ndef.hex(), "charge_id": cid}).encode()
        return signed(self.http, "POST", self.url("/api/device/tap"), self.a.device, self.a.secret, body)

    def check(self, name, cond, detail=""):
        print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   [{detail}]" if detail and not cond else ""))
        if not cond:
            self.fails += 1

    # ---- the story
    def run(self):
        admin = self.login("admin", "", self.a.admin_password)
        stud_a = self.login("student", ROLL_A, "1234")
        vend = self.login("vendor", "canteen", "1234")
        dev = self.a.device
        ov = self.get("/api/admin/overview", admin)
        vendor_id = next(v["id"] for v in ov["vendors"] if v["name"] == "Campus Canteen")
        bal0 = self.get("/api/student/me", stud_a)["balance"]
        print(f"Starting balance for {ROLL_A}: Rs {bal0}")

        print("Mess")
        self.post("/api/admin/session/open", admin, {"device_id": dev, "kind": "meal", "title": "Smoke lunch"})
        r = self.tap(CARD_A); self.check("first meal tap is accepted", r["code"] == "MEAL_OK", r)
        time.sleep(self.cfg.debounce_s + 0.5)
        r = self.tap(CARD_A); self.check("second meal in the same session is refused", r["code"] == "ALREADY", r)

        print("Attendance")
        sid = self.post("/api/admin/session/open", admin, {"device_id": dev, "kind": "class", "title": "Smoke class"})["session_id"]
        r = self.tap(CARD_A); self.check("student A marked present", r["code"] == "PRESENT", r)
        r = self.tap(CARD_B); self.check("student B marked present", r["code"] == "PRESENT", r)
        r = self.tap(CARD_B); self.check("repeat tap does not add a second row", r["code"] == "ALREADY", r)
        rows = self.get(f"/api/admin/session/{sid}/attendance", admin)
        self.check("attendance list has exactly two students", len(rows) == 2, rows)

        print("Payment")
        self.post(f"/api/admin/device/{dev}/mode", admin, {"mode": "vendor", "vendor_id": vendor_id})
        self.post("/api/vendor/charge", vend, {"amount": 40})
        r = self.tap(CARD_A); self.check("payment of Rs 40 goes through", r["code"] == "PAID", r)
        bal1 = self.get("/api/student/me", stud_a)["balance"]
        self.check("student balance dropped by exactly 40", bal1 == bal0 - 40, (bal0, bal1))
        r = self.tap(CARD_A, again=True); self.check("same tap message sent again is not charged twice",
                                                    r.get("duplicate") is True and self.get("/api/student/me", stud_a)["balance"] == bal1, r)
        r = self.tap(CARD_A); self.check("a fresh tap with no charge pending pays nothing", r["code"] == "NO_CHARGE", r)

        self.post("/api/vendor/charge", vend, {"amount": 500})
        r = self.tap(CARD_B); self.check("not enough credit is refused", r["code"] == "NO_FUNDS", r)
        self.post("/api/vendor/charge/cancel", vend)

        self.post("/api/vendor/charge", vend, {"amount": 10})
        old = signed(self.http, "GET", self.url("/api/device/poll"), self.a.device, self.a.secret)["charge"]["id"]
        self.post("/api/vendor/charge", vend, {"amount": 20})       # vendor changes the amount after the student saw Rs 10
        card = SimCard(CARD_A, self.cfg, self.state.get(CARD_A, 0))
        ndef = card.read_ndef(); self.state[CARD_A] = card.counter; _save(self.state)
        res = signed(self.http, "POST", self.url("/api/device/tap"), dev, self.a.secret,
                     json.dumps({"ndef": ndef.hex(), "charge_id": old}).encode())
        self.check("amount changed while tapping is refused, not charged", res["code"] == "CHARGE_CHANGED", res)
        self.post("/api/vendor/charge/cancel", vend)

        print("Lost card")
        self.post("/api/student/card/block", stud_a, {"blocked": True})
        self.post("/api/vendor/charge", vend, {"amount": 10})
        r = self.tap(CARD_A); self.check("blocked card is refused", r["code"] == "BLOCKED", r)
        self.post("/api/student/card/block", stud_a, {"blocked": False, "pin": "1234"})
        self.post("/api/vendor/charge", vend, {"amount": 10})
        r = self.tap(CARD_A); self.check("unblocked card works again", r["code"] == "PAID", r)

        print("Bad cards")
        r = self.tap("04DEADBEEF0001"); self.check("card nobody registered is recognised, not served", r["code"] == "UNREGISTERED", r)
        r = self.tap(CARD_A, corrupt=True); self.check("tampered tap is rejected", r["code"] == "BAD_TAP", r)

        print("Books")
        ov = self.get("/api/admin/overview", admin)
        self.check("ledger adds up to zero", ov["ledger"]["ok"], ov["ledger"])
        self.post(f"/api/admin/device/{dev}/mode", admin, {"mode": "idle"})
        return self.fails


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--server", default="http://127.0.0.1:8000")
    ap.add_argument("--device", default="reader-1")
    ap.add_argument("--secret", required=True)
    ap.add_argument("--admin-password", default=Config().admin_password)
    a = ap.parse_args()
    fails = Smoke(a).run()
    print("\nALL STEPS PASSED" if fails == 0 else f"\n{fails} STEP(S) FAILED")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
