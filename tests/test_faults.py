"""Things going wrong: crashes, lost replies, restarts. Nothing may be half-done."""
import os
import subprocess
import sys

import pytest

from app.config import Config
from app.service import Service

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def msg(w, roll):
    return w.cards[roll].read_ndef().hex()


def armed(w, amount=70):
    w.vendor_mode()
    w.svc.vendor_create_charge(w.v1, amount)
    return w.svc.device_poll("reader-1")["charge"]["id"]


def last_counter(w, roll):
    with w.svc.session() as c:
        return c.execute("SELECT last_counter FROM cards WHERE uid=?", (w.cards[roll].uid_hex,)).fetchone()[0]


@pytest.mark.parametrize("stage", ["_post_txn", "_record_tap"])
def test_crash_mid_payment_changes_nothing_and_the_same_message_can_be_retried(w, monkeypatch, stage):
    cid = armed(w)
    m = msg(w, "S1")
    before = last_counter(w, "S1")
    original = getattr(Service, stage)

    def boom(*a, **k):
        original(*a, **k)          # do the real write first, then die before commit
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(Service, stage, staticmethod(boom) if stage == "_record_tap" else boom)

    r = w.svc.device_tap("reader-1", ndef_hex=m, charge_id=cid)
    assert r["code"] == "ERROR" and not r["ok"]
    # nothing moved, the tap was not consumed, the charge is still armed
    assert w.balance("S1") == 200 and w.vbal(w.v1) == 0
    assert last_counter(w, "S1") == before
    assert w.svc.ledger_check()["ok"]
    assert w.svc.device_poll("reader-1")["charge"]["id"] == cid
    with w.svc.session() as c:
        assert c.execute("SELECT COUNT(*) FROM events WHERE code='ERROR'").fetchone()[0] == 1

    monkeypatch.undo()             # the fault clears; the reader retries the exact same bytes
    r = w.svc.device_tap("reader-1", ndef_hex=m, charge_id=cid)
    assert r["code"] == "PAID"
    assert w.balance("S1") == 130 and w.vbal(w.v1) == 70
    again = w.svc.device_tap("reader-1", ndef_hex=m, charge_id=cid)
    assert again["code"] == "PAID" and again.get("duplicate")
    assert w.balance("S1") == 130


def test_process_killed_before_commit_leaves_the_database_clean(w):
    """A real crash: another process begins a payment, writes ledger rows, and dies."""
    code = f"""
import os, sys
sys.path.insert(0, {ROOT!r})
from app.db import connect
c = connect({w.cfg.db_path!r})
c.execute("BEGIN IMMEDIATE")
acc = c.execute("SELECT account_id FROM students WHERE roll_no='S1'").fetchone()[0]
tid = c.execute("INSERT INTO txns(kind, memo, created_at) VALUES ('payment', 'crash', 1)").lastrowid
c.execute("INSERT INTO ledger(txn_id, account_id, amount, created_at) VALUES (?,?,?,1)", (tid, acc, -50))
os._exit(9)
"""
    r = subprocess.run([sys.executable, "-c", code], cwd=ROOT, timeout=30)
    assert r.returncode == 9
    assert w.balance("S1") == 200
    assert w.svc.ledger_check()["ok"]
    with w.svc.session() as c:
        assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert c.execute("SELECT COUNT(*) FROM txns WHERE memo='crash'").fetchone()[0] == 0
    # and the system keeps working
    cid = armed(w, 25)
    assert w.svc.device_tap("reader-1", ndef_hex=msg(w, "S2"), charge_id=cid)["code"] == "PAID"


def test_server_restart_still_answers_a_retried_message_without_repeating_it(w):
    cid = armed(w, 30)
    m = msg(w, "S1")
    assert w.svc.device_tap("reader-1", ndef_hex=m, charge_id=cid)["code"] == "PAID"
    restarted = Service(Config(db_path=w.cfg.db_path), clock=w.clock)
    r = restarted.device_tap("reader-1", ndef_hex=m, charge_id=cid)
    assert r["code"] == "PAID" and r.get("duplicate")
    assert w.balance("S1") == 170 and w.vbal(w.v1) == 30


def test_lost_reply_then_new_tap_is_a_normal_new_tap(w):
    """Reply never reached the reader. The student taps again: new counter, so it is a new tap."""
    w.svc.admin_open_session("reader-1", "meal", "Lunch")
    w.clock.advance(10)
    first = w.svc.device_tap("reader-1", ndef_hex=msg(w, "S1"))      # reply 'lost'
    assert first["code"] == "MEAL_OK"
    w.clock.advance(10)
    second = w.svc.device_tap("reader-1", ndef_hex=msg(w, "S1"))
    assert second["code"] == "ALREADY"                                # one meal only, and it says so
    with w.svc.session() as c:
        assert c.execute("SELECT COUNT(*) FROM checkins").fetchone()[0] == 1


def test_ledger_rows_cannot_be_rewritten_even_after_a_crash_scenario(w):
    cid = armed(w, 20)
    w.svc.device_tap("reader-1", ndef_hex=msg(w, "S1"), charge_id=cid)
    with w.svc.session() as c:
        with pytest.raises(Exception):
            c.execute("UPDATE ledger SET amount = amount * 2")
        with pytest.raises(Exception):
            c.execute("DELETE FROM ledger")
    assert w.svc.ledger_check()["ok"]
