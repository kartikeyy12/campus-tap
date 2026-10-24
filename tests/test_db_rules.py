"""The database itself must refuse states the application should never create."""
import sqlite3

import pytest

from app.db import connect


def q(w):
    return connect(w.cfg.db_path)


def test_ledger_is_append_only(w):
    c = q(w)
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        c.execute("UPDATE ledger SET amount = amount + 1")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        c.execute("DELETE FROM ledger")


def test_events_are_append_only(w):
    w.tap("S1")
    c = q(w)
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        c.execute("DELETE FROM events")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        c.execute("UPDATE events SET ok = 1")


def test_student_account_cannot_go_negative_even_if_app_code_is_buggy(w):
    c = q(w)
    acc = c.execute("SELECT account_id FROM students WHERE roll_no='S1'").fetchone()[0]
    tid = c.execute("INSERT INTO txns(kind, memo, created_at) VALUES ('adjust','bug',0)").lastrowid
    with pytest.raises(sqlite3.DatabaseError, match="insufficient funds"):
        c.execute("INSERT INTO ledger(txn_id, account_id, amount, created_at) VALUES (?,?,?,0)", (tid, acc, -201))
    c.execute("INSERT INTO ledger(txn_id, account_id, amount, created_at) VALUES (?,?,?,0)", (tid, acc, -200))  # exactly zero is fine


def test_one_armed_charge_per_reader(w):
    c = q(w)
    c.execute("INSERT INTO charges(vendor_id, device_id, amount, status, created_at, expires_at) "
              "VALUES (1,'reader-1',10,'armed',0,99)")
    with pytest.raises(sqlite3.IntegrityError):
        c.execute("INSERT INTO charges(vendor_id, device_id, amount, status, created_at, expires_at) "
                  "VALUES (1,'reader-1',20,'armed',0,99)")


def test_non_positive_charge_rejected(w):
    c = q(w)
    with pytest.raises(sqlite3.IntegrityError):
        c.execute("INSERT INTO charges(vendor_id, device_id, amount, status, created_at, expires_at) "
                  "VALUES (1,'reader-1',0,'armed',0,99)")


def test_one_checkin_per_student_per_session(w):
    w.svc.admin_open_session("reader-1", "class", "L")
    c = q(w)
    c.execute("INSERT INTO checkins(session_id, student_id, created_at) VALUES (1,1,0)")
    with pytest.raises(sqlite3.IntegrityError):
        c.execute("INSERT INTO checkins(session_id, student_id, created_at) VALUES (1,1,1)")


def test_one_open_session_per_reader(w):
    c = q(w)
    c.execute("INSERT INTO sessions(kind,title,device_id,opened_at) VALUES ('class','a','reader-1',0)")
    with pytest.raises(sqlite3.IntegrityError):
        c.execute("INSERT INTO sessions(kind,title,device_id,opened_at) VALUES ('meal','b','reader-1',1)")


def test_one_live_card_per_student_and_unique_uid(w):
    c = q(w)
    sid = c.execute("SELECT id FROM students WHERE roll_no='S1'").fetchone()[0]
    with pytest.raises(sqlite3.IntegrityError):
        c.execute("INSERT INTO cards(uid, student_id, issued_at) VALUES ('04ABABABABABAB', ?, 0)", (sid,))
    with pytest.raises(sqlite3.IntegrityError):
        c.execute("INSERT INTO cards(uid, student_id, issued_at) VALUES ('0401AABBCCDD01', 2, 0)")  # uid taken


def test_tap_uid_counter_is_unique(w):
    w.tap("S1")
    c = q(w)
    with pytest.raises(sqlite3.IntegrityError):
        c.execute("INSERT INTO taps(uid,counter,device_id,mode,ok,code,response,created_at) "
                  "VALUES ('0401AABBCCDD01',0,'reader-1','idle',0,'X','{}',0)")


def test_foreign_keys_enforced(w):
    c = q(w)
    with pytest.raises(sqlite3.IntegrityError):
        c.execute("INSERT INTO cards(uid, student_id, issued_at) VALUES ('04CDCDCDCDCDCD', 999, 0)")
