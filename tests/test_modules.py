"""Behaviour of each module through the real tap pipeline."""
import pytest

from app.service import ApiError
from tests.conftest import World
from tools.simulator import SimCard


def codes(rs):
    return [r["code"] for r in rs]


# ----------------------------------------------------------------------- issuing
def test_unregistered_card_then_registered_by_admin(w):
    stranger = SimCard("04FFEEDDCCBBAA", w.cfg)
    r = w.svc.device_tap("reader-1", ndef_hex=stranger.read_ndef().hex())
    assert r["code"] == "UNREGISTERED"
    w.svc.admin_register_card("04FFEEDDCCBBAA", "S1", replace=True)     # replaces Asha's old card
    assert w.tap("S1")["code"] == "RETIRED"                                 # old card no longer works
    r = w.svc.device_tap("reader-1", ndef_hex=stranger.read_ndef().hex())
    assert r["code"] == "IDLE" and r["l1"] == "Asha"                        # new card is Asha's now


def test_register_requires_a_verified_tap_first(w):
    with pytest.raises(ApiError) as e:
        w.svc.admin_register_card("04FFEEDDCCBBAA", "S1")
    assert e.value.status == 409


def test_register_twice_or_second_card_without_replace_fails(w):
    stranger = SimCard("04FFEEDDCCBBAA", w.cfg)
    w.svc.device_tap("reader-1", ndef_hex=stranger.read_ndef().hex())
    with pytest.raises(ApiError):
        w.svc.admin_register_card("04FFEEDDCCBBAA", "S1")      # S1 already has a live card
    w.svc.create_student("S4", "Dana", "1234")
    w.svc.admin_register_card("04FFEEDDCCBBAA", "S4")
    with pytest.raises(ApiError):
        w.svc.admin_register_card("04FFEEDDCCBBAA", "S2")      # already registered


def test_registration_cannot_be_replayed_with_the_pre_registration_message(w):
    stranger = SimCard("04FFEEDDCCBBAA", w.cfg)
    old = stranger.read_ndef().hex()
    w.svc.device_tap("reader-1", ndef_hex=old)
    w.svc.create_student("S4", "Dana", "1234")
    w.svc.admin_register_card("04FFEEDDCCBBAA", "S4")
    r = w.svc.device_tap("reader-1", ndef_hex=old)            # skimmed copy of the first tap
    assert r.get("duplicate") and r["code"] == "UNREGISTERED"


# ----------------------------------------------------------------------- attendance
def test_attendance_flow(w):
    assert w.tap("S1")["code"] == "IDLE"
    w.svc.admin_open_session("reader-1", "class", "EC303")
    assert w.tap("S1")["code"] == "PRESENT"
    assert w.tap("S1")["code"] == "ALREADY" and w.tap("S1")["ok"]
    assert w.tap("S2")["code"] == "PRESENT"
    sid = w.svc.admin_overview()["devices"][0]["session"]["id"]
    assert [r["roll_no"] for r in w.svc.admin_session_attendance(sid)] == ["S1", "S2"]
    w.svc.admin_close_session("reader-1")
    assert w.tap("S3")["code"] == "IDLE"


def test_mode_without_session_is_refused(w):
    with pytest.raises(ApiError):
        w.svc.admin_set_mode("reader-1", "attendance")
    with pytest.raises(ApiError):
        w.svc.admin_set_mode("reader-1", "mess")


def test_class_session_does_not_serve_meals(w):
    w.svc.admin_open_session("reader-1", "class", "Lecture")
    with pytest.raises(ApiError):
        w.svc.admin_set_mode("reader-1", "mess")


# ----------------------------------------------------------------------- mess
def test_mess_one_meal_per_session_with_double_read_tolerance(w):
    w.svc.admin_open_session("reader-1", "meal", "Lunch")
    assert w.tap("S1")["code"] == "MEAL_OK"
    w.clock.advance(1.0)
    r = w.tap("S1")                                  # same physical tap read twice
    assert r["ok"] and r["code"] == "MEAL_OK"
    w.clock.advance(60)
    r = w.tap("S1")                                  # genuine second attempt
    assert not r["ok"] and r["code"] == "ALREADY"
    with w.svc.session() as c:
        assert c.execute("SELECT COUNT(*) FROM checkins").fetchone()[0] == 1


def test_new_meal_session_allows_again(w):
    w.svc.admin_open_session("reader-1", "meal", "Lunch")
    w.tap("S1")
    w.clock.advance(60)
    w.svc.admin_open_session("reader-1", "meal", "Dinner")
    assert w.tap("S1")["code"] == "MEAL_OK"


# ----------------------------------------------------------------------- payments
def test_payment_happy_path_moves_money_exactly_once(w):
    w.vendor_mode()
    w.svc.vendor_create_charge(w.v1, 70)
    r = w.tap("S1")
    assert (r["code"], r["l2"]) == ("PAID", "Balance Rs 130")
    assert w.balance("S1") == 130 and w.vbal(w.v1) == 70
    assert w.tap("S1")["code"] == "NO_CHARGE"       # charge is single-use
    assert w.balance("S1") == 130
    assert w.svc.ledger_check()["ok"]


def test_insufficient_funds_keeps_charge_for_a_richer_card(w):
    w.vendor_mode()
    w.svc.vendor_create_charge(w.v1, 250)
    r = w.tap("S1")
    assert r["code"] == "NO_FUNDS" and w.balance("S1") == 200
    w.svc.student_topup(w.sid("S2"), 100)           # S2 now has 300
    assert w.tap("S2")["code"] == "PAID" and w.balance("S2") == 50


def test_charge_amount_limits(w):
    w.vendor_mode()
    for bad in (0, -5, 501):
        with pytest.raises(ApiError):
            w.svc.vendor_create_charge(w.v1, bad)
    w.svc.vendor_create_charge(w.v1, 500)


def test_daily_cap(tmp_path):
    w = World(tmp_path, daily_cap=100)
    w.vendor_mode()
    w.svc.vendor_create_charge(w.v1, 60)
    assert w.tap("S1")["code"] == "PAID"
    w.svc.vendor_create_charge(w.v1, 60)
    r = w.tap("S1")
    assert r["code"] == "DAILY_LIMIT" and w.balance("S1") == 140
    w.clock.advance(86400)                           # next day
    assert w.tap("S1")["code"] == "NO_CHARGE"        # the old charge expired long ago (30 s TTL)
    w.svc.vendor_create_charge(w.v1, 60)
    assert w.tap("S1")["code"] == "PAID"


def test_charge_expires(w):
    w.vendor_mode()
    w.svc.vendor_create_charge(w.v1, 50)
    cid = w.svc.device_poll("reader-1")["charge"]["id"]
    w.clock.advance(w.cfg.charge_ttl + 1)
    assert w.svc.device_poll("reader-1")["charge"] is None
    r = w.svc.device_tap("reader-1", ndef_hex=w.cards["S1"].read_ndef().hex(), charge_id=cid)
    assert r["code"] == "EXPIRED" and w.balance("S1") == 200


def test_vendor_changing_amount_mid_tap_cannot_overcharge(w):
    w.vendor_mode()
    w.svc.vendor_create_charge(w.v1, 50)
    seen_id = w.svc.device_poll("reader-1")["charge"]["id"]        # reader shows Rs 50
    w.svc.vendor_create_charge(w.v1, 400)                            # vendor edits before the tap lands
    r = w.svc.device_tap("reader-1", ndef_hex=w.cards["S1"].read_ndef().hex(), charge_id=seen_id)
    assert r["code"] == "CHARGE_CHANGED" and "400" in r["l1"] and w.balance("S1") == 200
    assert w.tap("S1")["code"] == "NO_FUNDS"        # with the new amount visible, Asha cannot afford it
    assert w.balance("S1") == 200 and w.vbal(w.v1) == 0


def test_vendor_changing_amount_then_paying_new_amount(w):
    w.vendor_mode()
    w.svc.vendor_create_charge(w.v1, 50)
    w.svc.vendor_create_charge(w.v1, 80)
    assert w.tap("S1")["code"] == "PAID"
    assert w.balance("S1") == 120 and w.vbal(w.v1) == 80


def test_tap_without_charge_id_in_vendor_mode_never_pays(w):
    w.vendor_mode()
    w.svc.vendor_create_charge(w.v1, 50)
    r = w.svc.device_tap("reader-1", ndef_hex=w.cards["S1"].read_ndef().hex(), charge_id=None)
    assert r["code"] == "CHARGE_CHANGED" and w.balance("S1") == 200


def test_cancelled_charge(w):
    w.vendor_mode()
    w.svc.vendor_create_charge(w.v1, 50)
    cid = w.svc.device_poll("reader-1")["charge"]["id"]
    w.svc.vendor_cancel_charge(w.v1)
    r = w.svc.device_tap("reader-1", ndef_hex=w.cards["S1"].read_ndef().hex(), charge_id=cid)
    assert r["code"] == "CANCELLED" and w.balance("S1") == 200


def test_switching_modes_cancels_open_charge(w):
    w.vendor_mode()
    w.svc.vendor_create_charge(w.v1, 50)
    w.svc.admin_open_session("reader-1", "class", "Lecture")
    assert w.svc.device_poll("reader-1")["charge"] is None
    with w.svc.session() as c:
        assert c.execute("SELECT status FROM charges").fetchone()[0] == "cancelled"


def test_vendor_without_assigned_reader_cannot_charge(w):
    with pytest.raises(ApiError):
        w.svc.vendor_create_charge(w.v1, 50)


def test_each_vendor_only_uses_their_own_reader(w):
    w.vendor_mode("reader-1", w.v1)
    w.vendor_mode("reader-2", w.v2)
    w.svc.vendor_create_charge(w.v2, 30)
    assert w.tap("S1", "reader-1")["code"] == "NO_CHARGE"        # v2's charge is on reader-2
    assert w.tap("S1", "reader-2")["code"] == "PAID"
    assert w.vbal(w.v2) == 30 and w.vbal(w.v1) == 0


# ----------------------------------------------------------------------- block / unblock
def test_block_stops_everything_and_unblock_needs_pin(w):
    sid = w.sid("S1")
    w.vendor_mode()
    w.svc.vendor_create_charge(w.v1, 50)
    w.svc.student_set_block(sid, True)
    r = w.tap("S1")
    assert r["code"] == "BLOCKED" and w.balance("S1") == 200
    with pytest.raises(ApiError) as e:
        w.svc.student_set_block(sid, False)
    assert e.value.status == 403
    with pytest.raises(ApiError):
        w.svc.student_set_block(sid, False, "0000")
    w.svc.student_set_block(sid, False, "1234")
    assert w.tap("S1")["code"] == "PAID"


def test_blocked_card_denied_in_every_mode(w):
    w.svc.student_set_block(w.sid("S1"), True)
    w.svc.admin_open_session("reader-1", "class", "L")
    assert w.tap("S1")["code"] == "BLOCKED"
    w.svc.admin_open_session("reader-1", "meal", "M")
    assert w.tap("S1")["code"] == "BLOCKED"
    with w.svc.session() as c:
        assert c.execute("SELECT COUNT(*) FROM checkins").fetchone()[0] == 0


def test_block_takes_effect_on_the_very_next_tap(w):
    w.vendor_mode()
    w.svc.vendor_create_charge(w.v1, 10)
    assert w.tap("S1")["code"] == "PAID"
    w.svc.student_set_block(w.sid("S1"), True)
    w.svc.vendor_create_charge(w.v1, 10)
    assert w.tap("S1")["code"] == "BLOCKED"


# ----------------------------------------------------------------------- replay / duplicates
def test_identical_message_delivered_twice_is_answered_once(w):
    w.vendor_mode()
    w.svc.vendor_create_charge(w.v1, 40)
    cid = w.svc.device_poll("reader-1")["charge"]["id"]
    msg = w.cards["S1"].read_ndef().hex()
    first = w.svc.device_tap("reader-1", ndef_hex=msg, charge_id=cid)       # response lost on wifi...
    again = w.svc.device_tap("reader-1", ndef_hex=msg, charge_id=cid)       # ...reader retries
    assert first["code"] == again["code"] == "PAID" and again.get("duplicate")
    assert w.balance("S1") == 160 and w.vbal(w.v1) == 40


def test_old_message_replayed_later_is_rejected(w):
    w.svc.admin_open_session("reader-1", "class", "L")
    m1 = w.cards["S1"].read_ndef().hex()
    m2 = w.cards["S1"].read_ndef().hex()
    assert w.svc.device_tap("reader-1", ndef_hex=m2)["code"] == "PRESENT"
    r = w.svc.device_tap("reader-1", ndef_hex=m1)       # skimmed earlier message
    assert r["code"] == "REPLAY"


def test_skimmed_message_replayed_at_vendor_does_not_pay(w):
    w.svc.admin_open_session("reader-2", "class", "L")
    skimmed = w.cards["S1"].read_ndef().hex()
    w.svc.device_tap("reader-2", ndef_hex=skimmed)       # genuine tap elsewhere
    w.vendor_mode("reader-1")
    w.svc.vendor_create_charge(w.v1, 100)
    cid = w.svc.device_poll("reader-1")["charge"]["id"]
    r = w.svc.device_tap("reader-1", ndef_hex=skimmed, charge_id=cid)
    assert r.get("duplicate") and r["code"] == "PRESENT"      # old answer, nothing executed
    assert w.balance("S1") == 200 and w.vbal(w.v1) == 0


def test_forged_and_corrupt_taps(w):
    w.vendor_mode()
    w.svc.vendor_create_charge(w.v1, 50)
    msg = bytearray(w.cards["S1"].read_ndef())
    msg[-1] ^= 0x01
    r = w.svc.device_tap("reader-1", ndef_hex=bytes(msg).hex())
    assert r["code"] == "BAD_TAP"
    assert w.svc.device_tap("reader-1", ndef_hex="zz")["code"] == "BAD_TAP"
    assert w.svc.device_tap("reader-1", ndef_hex="")["code"] == "BAD_TAP"
    assert w.balance("S1") == 200


def test_cloned_uid_without_keys_cannot_authenticate(w):
    from app.config import Config
    attacker_cfg = Config(db_path="x", key_mode="diversified", master_key=bytes([9]) * 16, meta_key=bytes([8]) * 16)
    clone = SimCard("0401AABBCCDD01", attacker_cfg)        # same UID as Asha's card, attacker's keys
    r = w.svc.device_tap("reader-1", ndef_hex=clone.read_ndef().hex())
    assert r["code"] == "BAD_TAP"


def test_unknown_device(w):
    with pytest.raises(ApiError) as e:
        w.svc.device_tap("nope", ndef_hex=w.cards["S1"].read_ndef().hex())
    assert e.value.status == 404


# ----------------------------------------------------------------------- top-ups, limits
def test_topup_limits(tmp_path):
    w = World(tmp_path, max_balance=1000)              # students start with Rs 200
    sid = w.sid("S1")
    with pytest.raises(ApiError):
        w.svc.student_topup(sid, 0)
    with pytest.raises(ApiError):
        w.svc.student_topup(sid, w.cfg.max_topup + 1)
    with pytest.raises(ApiError):
        w.svc.student_topup(sid, "50")
    with pytest.raises(ApiError):
        w.svc.student_topup(sid, True)
    w.svc.student_topup(sid, 800)                      # exactly at the ceiling: allowed
    with pytest.raises(ApiError) as e:
        w.svc.student_topup(sid, 1)                    # one rupee over: refused
    assert e.value.status == 409 and w.balance("S1") == 1000
    assert w.svc.ledger_check()["ok"]


# ----------------------------------------------------------------------- auth
def test_login_and_lockout(w):
    assert w.svc.login("student", "s1", "1234")["role"] == "student"
    for _ in range(5):
        with pytest.raises(ApiError):
            w.svc.login("student", "S2", "bad")
    with pytest.raises(ApiError) as e:
        w.svc.login("student", "S2", "1234")
    assert e.value.status == 429
    w.clock.advance(61)
    assert w.svc.login("student", "S2", "1234")["token"]


def test_tokens_are_role_bound_and_expire(w):
    tok = w.svc.login("student", "S1", "1234")["token"]
    assert w.svc.auth(tok, "student") == w.sid("S1")
    with pytest.raises(ApiError):
        w.svc.auth(tok, "admin")
    with pytest.raises(ApiError):
        w.svc.auth("garbage", "student")
    w.clock.advance(w.cfg.token_ttl + 1)
    with pytest.raises(ApiError):
        w.svc.auth(tok, "student")


def test_admin_overview_shape(w):
    o = w.svc.admin_overview()
    assert o["ledger"]["ok"] and o["counts"]["students"] == 3 and len(o["devices"]) == 2
