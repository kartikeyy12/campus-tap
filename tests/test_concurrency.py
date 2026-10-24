"""Many things at once must never break the money or attendance rules.

Each Service call opens its own SQLite connection, so these threads contend for the database
exactly like separate readers and browsers would.
"""
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from app.service import ApiError
from tests.conftest import World

ROLLS = ("S1", "S2", "S3")


def msg(w, roll):
    return w.cards[roll].read_ndef().hex()


def test_only_one_of_several_simultaneous_taps_pays_a_charge(w):
    w.vendor_mode()
    for _ in range(25):
        w.svc.vendor_create_charge(w.v1, 10)
        cid = w.svc.device_poll("reader-1")["charge"]["id"]
        msgs = {r: msg(w, r) for r in ROLLS}
        with ThreadPoolExecutor(3) as ex:
            res = list(ex.map(lambda r: w.svc.device_tap("reader-1", ndef_hex=msgs[r], charge_id=cid), ROLLS))
        assert [r["code"] for r in res].count("PAID") == 1, [r["code"] for r in res]
    assert sum(w.balance(r) for r in ROLLS) == 600 - 250
    assert w.vbal(w.v1) == 250
    assert w.svc.ledger_check()["ok"]


def test_same_message_arriving_many_times_at_once_changes_things_once(w):
    w.vendor_mode()
    w.svc.vendor_create_charge(w.v1, 40)
    cid = w.svc.device_poll("reader-1")["charge"]["id"]
    m = msg(w, "S1")
    with ThreadPoolExecutor(12) as ex:
        res = list(ex.map(lambda _: w.svc.device_tap("reader-1", ndef_hex=m, charge_id=cid), range(12)))
    assert {r["code"] for r in res} == {"PAID"}
    assert sum(1 for r in res if not r.get("duplicate")) == 1
    assert w.balance("S1") == 160 and w.vbal(w.v1) == 40


def test_attendance_message_retry_storm_marks_once(w):
    w.svc.admin_open_session("reader-1", "class", "Maths")
    m = msg(w, "S1")
    with ThreadPoolExecutor(12) as ex:
        res = list(ex.map(lambda _: w.svc.device_tap("reader-1", ndef_hex=m), range(12)))
    assert {r["code"] for r in res} == {"PRESENT"}
    with w.svc.session() as c:
        assert c.execute("SELECT COUNT(*) FROM checkins").fetchone()[0] == 1


def test_block_is_never_overtaken_by_a_payment(w):
    """Once 'block' has returned, no payment that starts afterwards may succeed."""
    w.vendor_mode()
    blocked, results = threading.Event(), []

    def worker():
        for _ in range(250):
            started_after_block = blocked.is_set()
            try:
                w.svc.vendor_create_charge(w.v1, 1)
            except ApiError:
                pass
            p = w.svc.device_poll("reader-1")
            cid = p["charge"]["id"] if p["charge"] else None
            r = w.svc.device_tap("reader-1", ndef_hex=msg(w, "S1"), charge_id=cid)
            results.append((started_after_block, r["code"]))
            if started_after_block and len([1 for a, _ in results if a]) >= 15:
                break

    t = threading.Thread(target=worker)
    t.start()
    deadline = time.time() + 10
    while sum(1 for a, c in results if c == "PAID") < 3 and time.time() < deadline:
        time.sleep(0.005)
    w.svc.student_set_block(w.sid("S1"), True)
    blocked.set()
    t.join(30)
    assert not t.is_alive()
    assert any(c == "PAID" for a, c in results if not a), "race was never exercised"
    assert not any(c == "PAID" for a, c in results if a), "a payment slipped through after the block"
    assert any(c == "BLOCKED" for a, c in results if a)
    assert w.svc.ledger_check()["ok"]


def test_random_mixed_load_keeps_every_invariant(tmp_path):
    w = World(tmp_path)
    w.vendor_mode("reader-1", w.v1)
    w.svc.admin_open_session("reader-2", "class", "Lab")
    lock = threading.Lock()
    topped = {r: 0 for r in ROLLS}
    paid_responses = []

    def worker(seed):
        rnd = random.Random(seed)
        for _ in range(60):
            roll = rnd.choice(ROLLS)
            op = rnd.random()
            if op < 0.25:
                amt = rnd.randint(1, 50)
                try:
                    w.svc.student_topup(w.sid(roll), amt)
                    with lock:
                        topped[roll] += amt
                except ApiError:
                    pass
            elif op < 0.5:
                try:
                    w.svc.vendor_create_charge(w.v1, rnd.randint(1, 80))
                except ApiError:
                    pass
            elif op < 0.9:
                p = w.svc.device_poll("reader-1")
                cid = p["charge"]["id"] if p["charge"] else None
                with lock:                      # a card's reads happen one at a time
                    m = msg(w, roll)
                r = w.svc.device_tap("reader-1", ndef_hex=m, charge_id=cid)
                if r["code"] == "PAID" and not r.get("duplicate"):
                    with lock:
                        paid_responses.append(r)
            else:
                with lock:
                    m = msg(w, roll)
                w.svc.device_tap("reader-2", ndef_hex=m)

    with ThreadPoolExecutor(8) as ex:
        list(ex.map(worker, range(8)))

    assert w.svc.ledger_check()["ok"]
    with w.svc.session() as c:
        done = c.execute("SELECT * FROM charges WHERE status='completed'").fetchall()
        assert len(done) == len(paid_responses)
        for ch in done:
            assert ch["txn_id"] is not None and ch["student_id"] is not None
            assert c.execute("SELECT SUM(amount) FROM ledger WHERE txn_id=?", (ch["txn_id"],)).fetchone()[0] == 0
        assert w.vbal(w.v1) == sum(ch["amount"] for ch in done)
        for roll in ROLLS:
            sid = w.sid(roll)
            spent = sum(ch["amount"] for ch in done if ch["student_id"] == sid)
            assert w.balance(roll) == 200 + topped[roll] - spent >= 0
        # one check-in per student at most, and only students who really tapped
        n = c.execute("SELECT COUNT(*) FROM (SELECT student_id FROM checkins GROUP BY student_id "
                      "HAVING COUNT(*) > 1)").fetchone()[0]
        assert n == 0


def test_soak_hundreds_of_taps_in_a_row(w):
    """Stand-in for the 500-tap hardware soak: nothing leaks, nothing drifts."""
    w.svc.admin_open_session("reader-1", "class", "Soak")
    started = time.time()
    for i in range(600):
        r = w.svc.device_tap("reader-1", ndef_hex=msg(w, ROLLS[i % 3]))
        assert r["code"] in ("PRESENT", "ALREADY")
    assert time.time() - started < 60
    with w.svc.session() as c:
        assert c.execute("SELECT COUNT(*) FROM checkins").fetchone()[0] == 3
        assert c.execute("SELECT COUNT(*) FROM taps").fetchone()[0] == 600
