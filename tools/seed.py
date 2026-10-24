"""Create demo data.

    python -m tools.seed --reset                       # fresh database with demo people
    python -m tools.seed --reset --card 24ECE1020=04AABBCCDDEE11   # also register real card UIDs
    python -m tools.seed --reset --sim                 # register the two software cards used by tools.smoke / tools.simtap
"""
from __future__ import annotations

import argparse
import os

from app.config import Config
from app.service import Service

DEMO_STUDENTS = [
    ("24ECE1020", "Kartikey"),
    ("24ECE1001", "Aarav Sharma"),
    ("24ECE1002", "Meera Nair"),
    ("24ECE1003", "Rohan Naik"),
]
DEMO_DEVICE = "reader-1"
SIM_CARDS = ["24ECE1020=04AABBCCDDEE11", "24ECE1001=04AABBCCDDEE22"]


def seed(cfg: Config, reset: bool = False, cards: list[str] | None = None, credit: int = 300,
         device_secret: str | None = None) -> dict:
    if reset:
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(cfg.db_path + suffix)
            except FileNotFoundError:
                pass
    svc = Service(cfg)
    with svc.session() as c:
        if c.execute("SELECT 1 FROM students LIMIT 1").fetchone():
            raise SystemExit("database already has data; use --reset for a fresh one")
    for roll, name in DEMO_STUDENTS:
        sid = svc.create_student(roll, name, "1234")
        if credit:
            svc.student_topup(sid, credit)
    svc.create_vendor("canteen", "Campus Canteen", "1234")
    svc.create_vendor("stationery", "Stationery Shop", "1234")
    secret = svc.create_device(DEMO_DEVICE, device_secret)
    for spec in cards or []:
        roll, uid = spec.split("=")
        svc.seed_card(uid, roll)
    return {"device": DEMO_DEVICE, "secret": secret}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reset", action="store_true")
    ap.add_argument("--card", action="append", help="ROLL=UID (14 hex), repeatable")
    ap.add_argument("--sim", action="store_true", help="also register the two software cards")
    ap.add_argument("--credit", type=int, default=300)
    ap.add_argument("--device-secret", help="32 hex chars; random if omitted")
    a = ap.parse_args()
    cfg = Config()
    info = seed(cfg, a.reset, (a.card or []) + (SIM_CARDS if a.sim else []), a.credit, a.device_secret)
    print(f"Database: {cfg.db_path}")
    print(f"Reader id: {info['device']}")
    print(f"Reader secret (put this in the firmware config.h): {info['secret']}")
    print("Demo logins (PIN 1234): students 24ECE1020, 24ECE1001, 24ECE1002, 24ECE1003; "
          "vendors canteen, stationery")
    print(f"Admin password: {cfg.admin_password}")


if __name__ == "__main__":
    main()
