import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import Config  # noqa: E402
from app.service import Service  # noqa: E402
from tools.simulator import SimCard  # noqa: E402


class FakeClock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


class World:
    """Three students with cards, two vendors, two readers. Each student starts with Rs 200."""

    def __init__(self, tmp_path, **cfg_kw):
        self.cfg = Config(db_path=str(tmp_path / "t.db"), **cfg_kw)
        self.clock = FakeClock()
        self.svc = Service(self.cfg, clock=self.clock)
        self.cards: dict[str, SimCard] = {}
        for i, (roll, name) in enumerate([("S1", "Asha"), ("S2", "Bilal"), ("S3", "Chen")], start=1):
            sid = self.svc.create_student(roll, name, "1234")
            uid = f"04{i:02X}AABBCCDD{i:02X}"
            self.svc.seed_card(uid, roll)
            self.cards[roll] = SimCard(uid, self.cfg)
            self.svc.student_topup(sid, 200)
        self.v1 = self.svc.create_vendor("shop", "Shop", "1234")
        self.v2 = self.svc.create_vendor("cafe", "Cafe", "1234")
        self.svc.create_device("reader-1")
        self.svc.create_device("reader-2")

    def tap(self, roll, device="reader-1", with_charge=True):
        """Behave like the reader firmware: poll for the displayed charge, then tap."""
        cid = None
        if with_charge:
            p = self.svc.device_poll(device)
            cid = p["charge"]["id"] if p["charge"] else None
        return self.svc.device_tap(device, ndef_hex=self.cards[roll].read_ndef().hex(), charge_id=cid)

    def vendor_mode(self, device="reader-1", vendor=None):
        self.svc.admin_set_mode(device, "vendor", vendor or self.v1)

    def balance(self, roll):
        return self.svc.student_me(self.sid(roll))["balance"]

    def sid(self, roll):
        with self.svc.session() as c:
            return c.execute("SELECT id FROM students WHERE roll_no=?", (roll,)).fetchone()["id"]

    def vbal(self, vendor_id):
        return self.svc.vendor_me(vendor_id)["balance"]


@pytest.fixture
def w(tmp_path):
    return World(tmp_path)
