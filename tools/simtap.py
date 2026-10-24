"""Virtual reader: behaves like the ESP32 so you can drive the system before the hardware arrives.

    python -m tools.simtap --secret <reader secret> tap 04AABBCCDDEE11
    python -m tools.simtap --secret <secret> tap 04AABBCCDDEE11 --again      # resend identical message
    python -m tools.simtap --secret <secret> tap 04AABBCCDDEE11 --bad        # corrupt the signature
    python -m tools.simtap --secret <secret> poll

Card counters are remembered in data/sim_cards.json so repeated runs keep increasing like a real card.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import httpx

from app.config import Config
from app.service import sign_body
from tools.simulator import SimCard, make_ndef

STATE = os.environ.get("SIM_STATE", "data/sim_cards.json")


def _load() -> dict:
    try:
        with open(STATE) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def _save(d: dict) -> None:
    os.makedirs(os.path.dirname(STATE) or ".", exist_ok=True)
    with open(STATE, "w") as f:
        json.dump(d, f)


def signed(client: httpx.Client, method: str, url: str, device: str, secret: str, body: bytes = b"") -> dict:
    sig = sign_body(secret, body if method == "POST" else b"poll")
    r = client.request(method, url, content=body if method == "POST" else None,
                       headers={"X-Device-Id": device, "X-Signature": sig, "Content-Type": "application/json"})
    r.raise_for_status()
    return r.json()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--server", default="http://127.0.0.1:8000")
    ap.add_argument("--device", default="reader-1")
    ap.add_argument("--secret", required=True)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("poll")
    t = sub.add_parser("tap")
    t.add_argument("uid")
    t.add_argument("--again", action="store_true", help="resend the previous message unchanged")
    t.add_argument("--bad", action="store_true", help="corrupt the signature")
    t.add_argument("--no-charge-id", action="store_true", help="do not attach the charge id")
    a = ap.parse_args()

    cfg = Config()
    with httpx.Client(timeout=10) as client:
        poll = signed(client, "GET", f"{a.server}/api/device/poll", a.device, a.secret)
        if a.cmd == "poll":
            print(json.dumps(poll, indent=2))
            return
        state = _load()
        uid = a.uid.upper()
        if a.again and f"last:{uid}" in state:
            ndef = bytes.fromhex(state[f"last:{uid}"])
        else:
            card = SimCard(uid, cfg, state.get(uid, 0))
            ndef = card.read_ndef()
            state[uid] = card.counter
            state[f"last:{uid}"] = ndef.hex()
            _save(state)
        if a.bad:
            b = bytearray(ndef)
            b[-1] = ord("0") if b[-1] != ord("0") else ord("1")
            ndef = bytes(b)
        charge_id = None if a.no_charge_id or not poll.get("charge") else poll["charge"]["id"]
        body = json.dumps({"ndef": ndef.hex(), "charge_id": charge_id}).encode()
        res = signed(client, "POST", f"{a.server}/api/device/tap", a.device, a.secret, body)
        print(f"[{res['tone'].upper()}] {res['big']} | {res['l1']} | {res['l2']}   ({res['code']})")
        sys.exit(0 if res["ok"] else 1)


if __name__ == "__main__":
    main()
