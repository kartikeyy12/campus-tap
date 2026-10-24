"""Fixes found while reviewing the first build."""
import pytest

from app import sdm
from app.db import init_db, on_windows_mount, tx

WSL = "Linux version 5.15.0-microsoft-standard-WSL2 (root@build) #1 SMP"
PLAIN = "Linux version 6.1.0-generic (buildd@ubuntu) #1 SMP"


def test_windows_mount_is_detected_only_under_wsl():
    assert on_windows_mount("/mnt/c/Users/karti/nfc-campus/data/campus.db", WSL)
    assert on_windows_mount("/mnt/d/x/campus.db", WSL)
    assert not on_windows_mount("/home/karti/nfc-campus/data/campus.db", WSL)
    assert not on_windows_mount("/mnt/wsl/x.db", WSL)                   # not a drive letter
    assert not on_windows_mount("/mnt/c/Users/karti/campus.db", PLAIN)  # real Linux box with a /mnt/c


def test_init_db_refuses_a_windows_mount_with_a_clear_message(monkeypatch):
    monkeypatch.delenv("CAMPUS_ALLOW_MNT", raising=False)
    monkeypatch.setattr("app.db.on_windows_mount", lambda p, v=None: True)
    with pytest.raises(RuntimeError, match="WSL home"):
        init_db("/mnt/c/Users/karti/campus.db")


def test_init_db_override_flag_is_respected(monkeypatch, tmp_path):
    monkeypatch.setenv("CAMPUS_ALLOW_MNT", "1")
    monkeypatch.setattr("app.db.on_windows_mount", lambda p, v=None: True)
    init_db(str(tmp_path / "ok.db"))


def test_tap_is_judged_by_the_mode_current_inside_the_transaction(w):
    """The reader row is read before the write lock. If an admin switches the mode in between,
    the stale 'vendor' row must not be used to take money."""
    w.vendor_mode()
    w.svc.vendor_create_charge(w.v1, 50)
    with w.svc.session() as c:
        stale = c.execute("SELECT * FROM devices WHERE id='reader-1'").fetchone()
        assert stale["mode"] == "vendor"
        w.svc.admin_set_mode("reader-1", "idle")           # admin switches while the tap is in flight
        uid, ctr = sdm.verify(w.cfg, *w.cards["S1"].read())
        with tx(c):
            r = w.svc._process_verified(c, w.clock(), stale, uid, ctr, charge_id=None)
    assert r["code"] == "IDLE"
    assert w.balance("S1") == 200 and w.vbal(w.v1) == 0
    assert w.svc.ledger_check()["ok"]


def test_reader_text_is_ascii_only(w):
    from app.service import ascii_fold
    assert ascii_fold("Zoë Müller") == "Zoe Muller"
    assert ascii_fold("मेरा") == "?"                      # nothing printable on an ASCII OLED
    assert ascii_fold("") == "" and ascii_fold("   ") == "   "
    with w.svc.session() as c:
        c.execute("UPDATE students SET name='Zoë Müller-Åkesson' WHERE roll_no='S1'")
    w.svc.admin_open_session("reader-1", "meal", "Dîner")
    r = w.tap("S1")
    assert r["l1"] == "Zoe Muller-Akesson"[:21]
    for field in ("big", "l1", "l2"):
        assert r[field].isascii()
    assert w.svc.device_poll("reader-1")["title"] == "Diner"


def test_favicon_request_is_answered_quietly(w):
    from fastapi.testclient import TestClient
    from app.main import create_app
    r = TestClient(create_app(service=w.svc)).get("/favicon.ico")
    assert r.status_code == 204
