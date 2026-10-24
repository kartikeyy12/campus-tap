"""All business logic. The API layer and the tests both talk to this class.

Rules of the road:
  * A tap is verified (no DB writes) first, then processed inside ONE write transaction.
  * Business "no" answers (blocked, no funds, ...) are normal results that get logged.
  * Anything unexpected rolls the whole transaction back and the reader gets a clean error.
  * The server owns time. Readers never decide anything.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import sqlite3
import threading
import time
import unicodedata
from contextlib import contextmanager

from . import sdm
from .config import Config
from .db import connect, init_db, tx

log = logging.getLogger("campus")


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def ascii_fold(s: str) -> str:
    """Reader screens only have an ASCII font. 'Zoë' -> 'Zoe'; text with no ASCII form at all -> '?'."""
    folded = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode("ascii")
    return folded if folded.strip() or not (s or "").strip() else "?"


def _resp(ok: bool, code: str, big: str, l1: str = "", l2: str = "", tone: str | None = None) -> dict:
    """What a reader shows after a tap. big: <=10 chars, l1/l2: <=21 chars, ASCII only."""
    return {"ok": ok, "code": code, "big": ascii_fold(big)[:10], "l1": ascii_fold(l1)[:21],
            "l2": ascii_fold(l2)[:21], "tone": tone or ("ok" if ok else "deny")}


def hash_pin(pin: str) -> str:
    salt = os.urandom(16)
    h = hashlib.pbkdf2_hmac("sha256", pin.encode(), salt, 100_000)
    return f"{salt.hex()}${h.hex()}"


def check_pin(pin: str, stored: str) -> bool:
    salt_hex, h_hex = stored.split("$")
    h = hashlib.pbkdf2_hmac("sha256", pin.encode(), bytes.fromhex(salt_hex), 100_000)
    return hmac.compare_digest(h.hex(), h_hex)


def sign_body(secret_hex: str, body: bytes) -> str:
    return hmac.new(bytes.fromhex(secret_hex), body, hashlib.sha256).hexdigest()


class Service:
    def __init__(self, cfg: Config | None = None, clock=time.time):
        self.cfg = cfg or Config()
        self.clock = clock
        init_db(self.cfg.db_path)
        self._fails: dict[tuple[str, str], list[float]] = {}
        self._fails_lock = threading.Lock()

    # ------------------------------------------------------------------ plumbing
    @contextmanager
    def session(self):
        c = connect(self.cfg.db_path)
        try:
            yield c
        finally:
            c.close()

    @staticmethod
    def _log(c, now, device_id, uid, student_id, kind, ok, code, detail=""):
        c.execute(
            "INSERT INTO events(created_at, device_id, uid, student_id, kind, ok, code, detail) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (now, device_id, uid, student_id, kind, int(ok), code, detail),
        )

    @staticmethod
    def _balance(c, account_id: int) -> int:
        return c.execute("SELECT COALESCE(SUM(amount),0) FROM ledger WHERE account_id=?", (account_id,)).fetchone()[0]

    def _day_start(self, now: float) -> float:
        off = self.cfg.tz_offset_min * 60
        return ((now + off) // 86400) * 86400 - off

    def _spent_today(self, c, account_id: int, now: float) -> int:
        return c.execute(
            "SELECT COALESCE(-SUM(l.amount),0) FROM ledger l JOIN txns t ON t.id = l.txn_id "
            "WHERE l.account_id=? AND t.kind='payment' AND l.amount<0 AND t.created_at>=?",
            (account_id, self._day_start(now)),
        ).fetchone()[0]

    def _post_txn(self, c, kind: str, memo: str, entries: list[tuple[int, int]], now: float) -> int:
        """Write one balanced transaction. Must be called inside tx()."""
        if sum(a for _, a in entries) != 0:
            raise RuntimeError("unbalanced transaction")
        txn_id = c.execute("INSERT INTO txns(kind, memo, created_at) VALUES (?,?,?)", (kind, memo, now)).lastrowid
        for account_id, amount in entries:
            c.execute("INSERT INTO ledger(txn_id, account_id, amount, created_at) VALUES (?,?,?,?)",
                      (txn_id, account_id, amount, now))
        if c.execute("SELECT SUM(amount) FROM ledger WHERE txn_id=?", (txn_id,)).fetchone()[0] != 0:
            raise RuntimeError("ledger imbalance after write")
        return txn_id

    # ------------------------------------------------------------------ seeding / setup
    def create_student(self, roll_no: str, name: str, pin: str = "1234") -> int:
        with self.session() as c, tx(c):
            acc = c.execute("INSERT INTO accounts(kind, name) VALUES('student', ?)", (name,)).lastrowid
            return c.execute("INSERT INTO students(roll_no, name, pin_hash, account_id) VALUES (?,?,?,?)",
                             (roll_no.upper(), name, hash_pin(pin), acc)).lastrowid

    def create_vendor(self, username: str, name: str, pin: str = "1234") -> int:
        with self.session() as c, tx(c):
            acc = c.execute("INSERT INTO accounts(kind, name) VALUES('vendor', ?)", (name,)).lastrowid
            return c.execute("INSERT INTO vendors(username, name, pin_hash, account_id) VALUES (?,?,?,?)",
                             (username.lower(), name, hash_pin(pin), acc)).lastrowid

    def create_device(self, device_id: str, secret_hex: str | None = None) -> str:
        secret_hex = secret_hex or secrets.token_hex(16)
        bytes.fromhex(secret_hex)
        with self.session() as c, tx(c):
            c.execute("INSERT INTO devices(id, secret) VALUES (?,?)", (device_id, secret_hex))
        return secret_hex

    def seed_card(self, uid: str, roll_no: str, last_counter: int = -1) -> int:
        """Register a card directly (seeding and tests). Real issuing goes through admin_register_card."""
        uid = uid.upper()
        with self.session() as c, tx(c):
            st = c.execute("SELECT id FROM students WHERE roll_no=?", (roll_no.upper(),)).fetchone()
            if not st:
                raise ApiError(404, "unknown student")
            return c.execute("INSERT INTO cards(uid, student_id, last_counter, issued_at) VALUES (?,?,?,?)",
                             (uid, st["id"], last_counter, self.clock())).lastrowid

    # ------------------------------------------------------------------ device endpoints
    def device_auth(self, device_id: str | None, body: bytes, sig_hex: str | None) -> None:
        if not device_id or not sig_hex:
            raise ApiError(401, "missing device credentials")
        with self.session() as c:
            row = c.execute("SELECT secret FROM devices WHERE id=?", (device_id,)).fetchone()
        if not row:
            raise ApiError(401, "unknown device")
        if not hmac.compare_digest(sign_body(row["secret"], body), sig_hex.lower()):
            raise ApiError(401, "bad signature")

    def device_poll(self, device_id: str) -> dict:
        now = self.clock()
        with self.session() as c:
            c.execute("UPDATE devices SET last_seen=? WHERE id=?", (now, device_id))
            dev = c.execute("SELECT * FROM devices WHERE id=?", (device_id,)).fetchone()
            out = {"mode": dev["mode"], "server_time": now, "title": "", "charge": None, "vendor": ""}
            if dev["mode"] in ("attendance", "mess"):
                s = c.execute("SELECT title FROM sessions WHERE device_id=? AND closed_at IS NULL", (device_id,)).fetchone()
                out["title"] = ascii_fold(s["title"]) if s else ""
                if not s:
                    out["mode"] = "idle"   # mode without an open session behaves as idle
            elif dev["mode"] == "vendor":
                v = c.execute("SELECT name FROM vendors WHERE id=?", (dev["vendor_id"],)).fetchone()
                out["vendor"] = ascii_fold(v["name"]) if v else ""
                ch = c.execute("SELECT id, amount, expires_at FROM charges WHERE device_id=? AND status='armed' "
                               "AND expires_at>?", (device_id, now)).fetchone()
                if ch:
                    out["charge"] = {"id": ch["id"], "amount": ch["amount"], "ttl": round(ch["expires_at"] - now, 1)}
            return out

    def device_tap(self, device_id: str, ndef_hex: str | None = None, url: str | None = None,
                   charge_id: int | None = None) -> dict:
        now = self.clock()
        with self.session() as c:
            dev = c.execute("SELECT * FROM devices WHERE id=?", (device_id,)).fetchone()
            if dev is None:
                raise ApiError(404, "unknown device")
            # 1. authenticate the tap itself (pure computation, no writes)
            try:
                ndef = bytes.fromhex(ndef_hex) if ndef_hex else None
                picc, mac = sdm.parse_tap(ndef=ndef, url=url)
                uid, ctr = sdm.verify(self.cfg, picc, mac)
            except (sdm.SDMError, ValueError, TypeError, AttributeError) as e:   # wrong JSON types too
                self._log(c, now, device_id, None, None, "tap", 0, "BAD_TAP", str(e)[:120])
                return _resp(False, "BAD_TAP", "BAD TAP", "Card not valid", "Try again", tone="warn")
            # 2. process it atomically
            try:
                with tx(c):
                    return self._process_verified(c, now, dev, uid, ctr, charge_id)
            except Exception:  # noqa: BLE001 - reader must always get a clean answer
                log.exception("tap processing failed; transaction rolled back")
                try:
                    self._log(c, now, device_id, uid, None, "tap", 0, "ERROR", "internal error, nothing changed")
                except Exception:  # noqa: BLE001
                    pass
                return _resp(False, "ERROR", "SERVER ERR", "Nothing was changed", "Tap again", tone="warn")

    def _process_verified(self, c, now, dev, uid: str, ctr: int, charge_id) -> dict:
        did = dev["id"]
        # `dev` was read before the write lock was taken; an admin may have switched the mode since.
        # Re-read it here, inside the transaction, so the tap is judged against the mode that is current.
        dev = c.execute("SELECT * FROM devices WHERE id=?", (did,)).fetchone() or dev
        dup = c.execute("SELECT response FROM taps WHERE uid=? AND counter=?", (uid, ctr)).fetchone()
        if dup:  # same message delivered twice (network retry): return the original answer, do nothing
            r = json.loads(dup["response"])
            r["duplicate"] = True
            self._log(c, now, did, uid, None, "tap", int(r["ok"]), "DUPLICATE", f"replayed answer for {r['code']}")
            return r

        card = c.execute("SELECT * FROM cards WHERE uid=?", (uid,)).fetchone()
        if card is None:
            r = _resp(False, "UNREGISTERED", "NEW CARD", "Not registered", "Ask admin", tone="warn")
            self._record_tap(c, now, uid, ctr, did, None, dev["mode"], r)
            self._log(c, now, did, uid, None, "tap", 0, "UNREGISTERED", "valid card, not issued to a student")
            return r

        if ctr <= card["last_counter"]:
            r = _resp(False, "REPLAY", "DENIED", "Old tap message", "Rejected")
            self._log(c, now, did, uid, card["student_id"], "tap", 0, "REPLAY",
                      f"counter {ctr} <= last seen {card['last_counter']}")
            return r

        c.execute("UPDATE cards SET last_counter=? WHERE id=?", (ctr, card["id"]))
        student = c.execute("SELECT * FROM students WHERE id=?", (card["student_id"],)).fetchone()

        if card["status"] != "active":
            code = "BLOCKED" if card["status"] == "blocked" else "RETIRED"
            r = _resp(False, code, "BLOCKED" if code == "BLOCKED" else "REPLACED", student["name"],
                      "Card is blocked" if code == "BLOCKED" else "Card was replaced")
        else:
            handler = {"idle": self._h_idle, "attendance": self._h_checkin,
                       "mess": self._h_checkin, "vendor": self._h_pay}[dev["mode"]]
            r = handler(c, now, dev, card, student, charge_id)

        self._record_tap(c, now, uid, ctr, did, card["id"], dev["mode"], r)
        self._log(c, now, did, uid, student["id"], "tap", int(r["ok"]), r["code"], f"{dev['mode']}: {r['l1']} {r['l2']}".strip())
        return r

    @staticmethod
    def _record_tap(c, now, uid, ctr, device_id, card_id, mode, resp):
        c.execute("INSERT INTO taps(uid, counter, device_id, card_id, mode, ok, code, response, created_at) "
                  "VALUES (?,?,?,?,?,?,?,?,?)",
                  (uid, ctr, device_id, card_id, mode, int(resp["ok"]), resp["code"], json.dumps(resp), now))

    # ---- mode handlers
    @staticmethod
    def _h_idle(c, now, dev, card, student, charge_id):
        return _resp(False, "IDLE", "IDLE", student["name"], "Reader not in use", tone="warn")

    def _h_checkin(self, c, now, dev, card, student, charge_id):
        kind = "class" if dev["mode"] == "attendance" else "meal"
        sess = c.execute("SELECT * FROM sessions WHERE device_id=? AND closed_at IS NULL", (dev["id"],)).fetchone()
        if sess is None or sess["kind"] != kind:
            return _resp(False, "NO_SESSION", "NO SESSION", student["name"], "Nothing is open", tone="warn")
        ok_big, ok_code = ("PRESENT", "PRESENT") if kind == "class" else ("MEAL OK", "MEAL_OK")
        prev = c.execute("SELECT created_at FROM checkins WHERE session_id=? AND student_id=?",
                         (sess["id"], student["id"])).fetchone()
        if prev:
            if kind == "class":
                return _resp(True, "ALREADY", "PRESENT", student["name"], "Already marked")
            if now - prev["created_at"] <= self.cfg.debounce_s:   # same physical tap read twice
                return _resp(True, ok_code, ok_big, student["name"], sess["title"])
            # show campus time (IST), not whatever timezone the laptop/WSL happens to be set to
            when = time.strftime("%H:%M", time.gmtime(prev["created_at"] + self.cfg.tz_offset_min * 60))
            return _resp(False, "ALREADY", "ALREADY", student["name"], f"Taken at {when}")
        c.execute("INSERT INTO checkins(session_id, student_id, created_at) VALUES (?,?,?)",
                  (sess["id"], student["id"], now))
        return _resp(True, ok_code, ok_big, student["name"], sess["title"])

    def _h_pay(self, c, now, dev, card, student, charge_id):
        c.execute("UPDATE charges SET status='expired' WHERE status='armed' AND expires_at<=?", (now,))
        ch = c.execute("SELECT * FROM charges WHERE device_id=? AND status='armed'", (dev["id"],)).fetchone()
        if ch is None:
            if charge_id is not None:
                old = c.execute("SELECT status FROM charges WHERE id=? AND device_id=?", (charge_id, dev["id"])).fetchone()
                if old and old["status"] == "expired":
                    return _resp(False, "EXPIRED", "EXPIRED", "Charge timed out", "Ask vendor again", tone="warn")
                if old and old["status"] == "cancelled":
                    return _resp(False, "CANCELLED", "CANCELLED", "Vendor cancelled", "Ask vendor again", tone="warn")
            return _resp(False, "NO_CHARGE", "NO CHARGE", student["name"], "Vendor sets amount", tone="warn")
        if charge_id is None or int(charge_id) != ch["id"]:
            return _resp(False, "CHARGE_CHANGED", "CHANGED", f"Now Rs {ch['amount']}", "Check, tap again", tone="warn")
        amt = ch["amount"]
        if amt > self.cfg.max_txn:
            return _resp(False, "LIMIT", "LIMIT", f"Max Rs {self.cfg.max_txn} per pay", "")
        spent = self._spent_today(c, student["account_id"], now)
        if spent + amt > self.cfg.daily_cap:
            return _resp(False, "DAILY_LIMIT", "DAY LIMIT", f"Spent Rs {spent} today", f"Cap Rs {self.cfg.daily_cap}")
        bal = self._balance(c, student["account_id"])
        if bal < amt:
            return _resp(False, "NO_FUNDS", "NO FUNDS", f"Balance Rs {bal}", f"Need Rs {amt}")
        vendor = c.execute("SELECT * FROM vendors WHERE id=?", (ch["vendor_id"],)).fetchone()
        txn_id = self._post_txn(c, "payment", f"{vendor['name']} (charge #{ch['id']})",
                                [(student["account_id"], -amt), (vendor["account_id"], amt)], now)
        n = c.execute("UPDATE charges SET status='completed', student_id=?, completed_at=?, txn_id=? "
                      "WHERE id=? AND status='armed'", (student["id"], now, txn_id, ch["id"])).rowcount
        if n != 1:
            raise RuntimeError("charge changed state during payment")
        return _resp(True, "PAID", f"PAID {amt}", student["name"], f"Balance Rs {bal - amt}")

    # ------------------------------------------------------------------ auth
    def login(self, role: str, ident: str, pin: str) -> dict:
        now = self.clock()
        key = (role, (ident or "").strip().lower())
        with self._fails_lock:
            recent = [t for t in self._fails.get(key, []) if now - t < self.cfg.login_lock_s]
            self._fails[key] = recent
            if len(recent) >= self.cfg.login_max_fails:
                raise ApiError(429, "too many attempts, wait a minute")
        subject, name = None, ""
        with self.session() as c:
            if role == "admin":
                if hmac.compare_digest((pin or "").encode(), self.cfg.admin_password.encode()):
                    subject, name = 0, "Admin"
            elif role == "student":
                r = c.execute("SELECT * FROM students WHERE roll_no=?", ((ident or "").strip().upper(),)).fetchone()
                if r and check_pin(pin or "", r["pin_hash"]):
                    subject, name = r["id"], r["name"]
            elif role == "vendor":
                r = c.execute("SELECT * FROM vendors WHERE username=?", ((ident or "").strip().lower(),)).fetchone()
                if r and check_pin(pin or "", r["pin_hash"]):
                    subject, name = r["id"], r["name"]
            else:
                raise ApiError(400, "unknown role")
            if subject is None:
                with self._fails_lock:
                    self._fails.setdefault(key, []).append(now)
                raise ApiError(401, "wrong id or pin")
            token = secrets.token_urlsafe(24)
            c.execute("INSERT INTO auth_tokens(token, role, subject_id, expires_at) VALUES (?,?,?,?)",
                      (token, role, subject, now + self.cfg.token_ttl))
        return {"token": token, "role": role, "name": name}

    def auth(self, token: str | None, role: str) -> int:
        if not token:
            raise ApiError(401, "not logged in")
        with self.session() as c:
            r = c.execute("SELECT * FROM auth_tokens WHERE token=?", (token,)).fetchone()
        if not r or r["role"] != role or r["expires_at"] < self.clock():
            raise ApiError(401, "session expired, log in again")
        return r["subject_id"]

    # ------------------------------------------------------------------ student
    def student_me(self, student_id: int) -> dict:
        with self.session() as c:
            st = c.execute("SELECT * FROM students WHERE id=?", (student_id,)).fetchone()
            card = c.execute("SELECT * FROM cards WHERE student_id=? AND status!='retired' ORDER BY id DESC LIMIT 1",
                             (student_id,)).fetchone()
            hist = []
            for r in c.execute("SELECT l.created_at ts, t.kind, t.memo, l.amount FROM ledger l "
                               "JOIN txns t ON t.id=l.txn_id WHERE l.account_id=? ORDER BY l.id DESC LIMIT 20",
                               (st["account_id"],)):
                hist.append({"ts": r["ts"], "type": r["kind"], "text": r["memo"], "amount": r["amount"]})
            for r in c.execute("SELECT c.created_at ts, s.kind, s.title FROM checkins c JOIN sessions s "
                               "ON s.id=c.session_id WHERE c.student_id=? ORDER BY c.id DESC LIMIT 20", (student_id,)):
                hist.append({"ts": r["ts"], "type": "attendance" if r["kind"] == "class" else "meal",
                             "text": r["title"], "amount": None})
            hist.sort(key=lambda h: h["ts"], reverse=True)
            return {
                "name": st["name"], "roll_no": st["roll_no"],
                "balance": self._balance(c, st["account_id"]),
                "spent_today": self._spent_today(c, st["account_id"], self.clock()),
                "limits": {"max_txn": self.cfg.max_txn, "daily_cap": self.cfg.daily_cap,
                           "max_balance": self.cfg.max_balance, "max_topup": self.cfg.max_topup},
                "card": ({"status": card["status"], "uid_tail": card["uid"][-4:]} if card else None),
                "history": hist[:25],
            }

    def student_set_block(self, student_id: int, blocked: bool, pin: str | None = None) -> dict:
        now = self.clock()
        with self.session() as c:
            if not blocked:   # unblocking needs the PIN again
                st = c.execute("SELECT pin_hash FROM students WHERE id=?", (student_id,)).fetchone()
                if not pin or not check_pin(pin, st["pin_hash"]):
                    raise ApiError(403, "PIN required to unblock")
            with tx(c):
                card = c.execute("SELECT * FROM cards WHERE student_id=? AND status!='retired'", (student_id,)).fetchone()
                if not card:
                    raise ApiError(404, "no card issued to you yet")
                new = "blocked" if blocked else "active"
                c.execute("UPDATE cards SET status=? WHERE id=?", (new, card["id"]))
                self._log(c, now, None, card["uid"], student_id, "student", 1,
                          "CARD_BLOCKED" if blocked else "CARD_UNBLOCKED", "by student")
        return {"status": new}

    def student_topup(self, student_id: int, amount: int) -> dict:
        now = self.clock()
        if not isinstance(amount, int) or isinstance(amount, bool) or not (1 <= amount <= self.cfg.max_topup):
            raise ApiError(400, f"amount must be 1 to {self.cfg.max_topup}")
        with self.session() as c, tx(c):
            st = c.execute("SELECT * FROM students WHERE id=?", (student_id,)).fetchone()
            bal = self._balance(c, st["account_id"])
            if bal + amount > self.cfg.max_balance:
                raise ApiError(409, f"balance cannot exceed Rs {self.cfg.max_balance}")
            sysacc = c.execute("SELECT id FROM accounts WHERE kind='system'").fetchone()["id"]
            self._post_txn(c, "topup", "Demo top-up (no real money)", [(sysacc, -amount), (st["account_id"], amount)], now)
            self._log(c, now, None, None, student_id, "student", 1, "TOPUP", f"Rs {amount}")
            return {"balance": bal + amount}

    # ------------------------------------------------------------------ vendor
    def _vendor_device(self, c, vendor_id: int):
        return c.execute("SELECT * FROM devices WHERE vendor_id=? AND mode='vendor' ORDER BY id LIMIT 1",
                         (vendor_id,)).fetchone()

    def vendor_me(self, vendor_id: int) -> dict:
        now = self.clock()
        with self.session() as c:
            v = c.execute("SELECT * FROM vendors WHERE id=?", (vendor_id,)).fetchone()
            dev = self._vendor_device(c, vendor_id)
            charge = None
            if dev:
                ch = c.execute("SELECT * FROM charges WHERE device_id=? ORDER BY id DESC LIMIT 1", (dev["id"],)).fetchone()
                if ch:
                    charge = self._charge_view(ch, now)
            sales = [{"ts": r["completed_at"], "amount": r["amount"], "student": r["name"]} for r in c.execute(
                "SELECT ch.completed_at, ch.amount, s.name FROM charges ch JOIN students s ON s.id=ch.student_id "
                "WHERE ch.vendor_id=? AND ch.status='completed' ORDER BY ch.id DESC LIMIT 10", (vendor_id,))]
            return {"name": v["name"], "balance": self._balance(c, v["account_id"]),
                    "device": ({"id": dev["id"], "online": bool(dev["last_seen"] and now - dev["last_seen"] <= self.cfg.device_online_s)}
                               if dev else None),
                    "charge": charge, "sales": sales, "max_txn": self.cfg.max_txn}

    @staticmethod
    def _charge_view(ch, now) -> dict:
        status = ch["status"]
        if status == "armed" and ch["expires_at"] <= now:
            status = "expired"
        return {"id": ch["id"], "amount": ch["amount"], "status": status,
                "ttl": max(0.0, round(ch["expires_at"] - now, 1)) if status == "armed" else 0}

    def vendor_create_charge(self, vendor_id: int, amount: int) -> dict:
        now = self.clock()
        if not isinstance(amount, int) or isinstance(amount, bool) or not (1 <= amount <= self.cfg.max_txn):
            raise ApiError(400, f"amount must be 1 to {self.cfg.max_txn}")
        with self.session() as c, tx(c):
            dev = self._vendor_device(c, vendor_id)
            if not dev:
                raise ApiError(409, "no reader is assigned to you right now")
            c.execute("UPDATE charges SET status='cancelled' WHERE device_id=? AND status='armed'", (dev["id"],))
            cid = c.execute("INSERT INTO charges(vendor_id, device_id, amount, status, created_at, expires_at) "
                            "VALUES (?,?,?,'armed',?,?)", (vendor_id, dev["id"], amount, now, now + self.cfg.charge_ttl)).lastrowid
            self._log(c, now, dev["id"], None, None, "vendor", 1, "CHARGE_ARMED", f"charge #{cid} Rs {amount}")
            return {"id": cid, "amount": amount, "status": "armed", "ttl": self.cfg.charge_ttl}

    def vendor_cancel_charge(self, vendor_id: int) -> dict:
        now = self.clock()
        with self.session() as c, tx(c):
            dev = self._vendor_device(c, vendor_id)
            if dev:
                c.execute("UPDATE charges SET status='cancelled' WHERE device_id=? AND status='armed'", (dev["id"],))
                self._log(c, now, dev["id"], None, None, "vendor", 1, "CHARGE_CANCELLED", "")
        return {"ok": True}

    # ------------------------------------------------------------------ admin
    def ledger_check(self, c=None) -> dict:
        def run(c):
            total = c.execute("SELECT COALESCE(SUM(amount),0) FROM ledger").fetchone()[0]
            bad_txns = c.execute("SELECT COUNT(*) FROM (SELECT txn_id FROM ledger GROUP BY txn_id "
                                 "HAVING SUM(amount)!=0)").fetchone()[0]
            neg = c.execute("SELECT COUNT(*) FROM (SELECT l.account_id FROM ledger l JOIN accounts a ON a.id=l.account_id "
                            "WHERE a.kind IN ('student','vendor') GROUP BY l.account_id HAVING SUM(l.amount)<0)").fetchone()[0]
            return {"ok": total == 0 and bad_txns == 0 and neg == 0, "total": total,
                    "unbalanced_txns": bad_txns, "negative_accounts": neg}
        if c is not None:
            return run(c)
        with self.session() as c2:
            return run(c2)

    def _warnings(self) -> list[str]:
        """Things that are fine for the expo demo but must change before any real use."""
        w = []
        if self.cfg.key_mode == "default":
            w.append("Demo security: cards use factory keys, so anyone who knows the format could forge a tap.")
        if self.cfg.admin_password == "admin123":
            w.append("Demo security: the admin password is still the default.")
        return w

    def admin_overview(self) -> dict:
        now = self.clock()
        with self.session() as c:
            devices = []
            for d in c.execute("SELECT * FROM devices ORDER BY id"):
                s = c.execute("SELECT * FROM sessions WHERE device_id=? AND closed_at IS NULL", (d["id"],)).fetchone()
                sess = None
                if s:
                    n = c.execute("SELECT COUNT(*) FROM checkins WHERE session_id=?", (s["id"],)).fetchone()[0]
                    sess = {"id": s["id"], "kind": s["kind"], "title": s["title"], "count": n, "opened_at": s["opened_at"]}
                v = c.execute("SELECT name FROM vendors WHERE id=?", (d["vendor_id"],)).fetchone() if d["vendor_id"] else None
                ch = c.execute("SELECT * FROM charges WHERE device_id=? AND status='armed' AND expires_at>?",
                               (d["id"], now)).fetchone()
                devices.append({"id": d["id"], "mode": d["mode"], "vendor_id": d["vendor_id"],
                                "vendor_name": v["name"] if v else None,
                                "online": bool(d["last_seen"] and now - d["last_seen"] <= self.cfg.device_online_s),
                                "session": sess, "charge": self._charge_view(ch, now) if ch else None})
            unreg = [{"uid": r["uid"], "counter": r["counter"], "ts": r["created_at"]} for r in c.execute(
                "SELECT t.uid, t.counter, t.created_at FROM taps t WHERE t.code='UNREGISTERED' AND NOT EXISTS "
                "(SELECT 1 FROM cards k WHERE k.uid=t.uid) AND t.id=(SELECT MAX(id) FROM taps WHERE uid=t.uid) "
                "ORDER BY t.id DESC LIMIT 5")]
            return {
                "server_time": now,
                "devices": devices,
                "vendors": [{"id": r["id"], "name": r["name"]} for r in c.execute("SELECT id, name FROM vendors ORDER BY id")],
                "students": [{"roll_no": r["roll_no"], "name": r["name"], "card": r["status"]} for r in c.execute(
                    "SELECT s.roll_no, s.name, k.status FROM students s LEFT JOIN cards k "
                    "ON k.student_id=s.id AND k.status!='retired' ORDER BY s.roll_no")],
                "counts": {
                    "students": c.execute("SELECT COUNT(*) FROM students").fetchone()[0],
                    "cards": c.execute("SELECT COUNT(*) FROM cards WHERE status!='retired'").fetchone()[0],
                    "blocked": c.execute("SELECT COUNT(*) FROM cards WHERE status='blocked'").fetchone()[0],
                    "taps": c.execute("SELECT COUNT(*) FROM taps").fetchone()[0],
                    "denied": c.execute("SELECT COUNT(*) FROM events WHERE kind='tap' AND ok=0").fetchone()[0],
                },
                "ledger": self.ledger_check(c),
                "warnings": self._warnings(),
                "unregistered": unreg,
                "limits": {"max_txn": self.cfg.max_txn, "daily_cap": self.cfg.daily_cap, "charge_ttl": self.cfg.charge_ttl},
            }

    def admin_events(self, after_id: int | None = None, limit: int = 60) -> list[dict]:
        with self.session() as c:
            base = ("SELECT e.id, e.created_at ts, e.device_id, e.uid, e.kind, e.ok, e.code, e.detail, s.name student "
                    "FROM events e LEFT JOIN students s ON s.id=e.student_id ")
            if after_id is None:
                rows = c.execute(base + "ORDER BY e.id DESC LIMIT ?", (limit,)).fetchall()[::-1]
            else:
                rows = c.execute(base + "WHERE e.id>? ORDER BY e.id LIMIT ?", (after_id, limit)).fetchall()
            return [dict(r) for r in rows]

    def admin_open_session(self, device_id: str, kind: str, title: str) -> dict:
        now = self.clock()
        title = (title or "").strip()[:40]
        if kind not in ("class", "meal") or not title:
            raise ApiError(400, "kind must be class or meal, and a title is required")
        with self.session() as c, tx(c):
            if not c.execute("SELECT 1 FROM devices WHERE id=?", (device_id,)).fetchone():
                raise ApiError(404, "unknown device")
            c.execute("UPDATE sessions SET closed_at=? WHERE device_id=? AND closed_at IS NULL", (now, device_id))
            c.execute("UPDATE charges SET status='cancelled' WHERE device_id=? AND status='armed'", (device_id,))
            sid = c.execute("INSERT INTO sessions(kind, title, device_id, opened_at) VALUES (?,?,?,?)",
                            (kind, title, device_id, now)).lastrowid
            c.execute("UPDATE devices SET mode=?, vendor_id=NULL WHERE id=?",
                      ("attendance" if kind == "class" else "mess", device_id))
            self._log(c, now, device_id, None, None, "admin", 1, "SESSION_OPENED", f"{kind}: {title}")
            return {"session_id": sid}

    def admin_close_session(self, device_id: str) -> dict:
        return self.admin_set_mode(device_id, "idle")

    def admin_set_mode(self, device_id: str, mode: str, vendor_id: int | None = None) -> dict:
        now = self.clock()
        with self.session() as c, tx(c):
            dev = c.execute("SELECT * FROM devices WHERE id=?", (device_id,)).fetchone()
            if not dev:
                raise ApiError(404, "unknown device")
            if mode in ("attendance", "mess"):
                kind = "class" if mode == "attendance" else "meal"
                s = c.execute("SELECT kind FROM sessions WHERE device_id=? AND closed_at IS NULL", (device_id,)).fetchone()
                if not s or s["kind"] != kind:
                    raise ApiError(409, f"open a {kind} session first")
                c.execute("UPDATE devices SET mode=?, vendor_id=NULL WHERE id=?", (mode, device_id))
            elif mode == "vendor":
                if vendor_id is None or not c.execute("SELECT 1 FROM vendors WHERE id=?", (vendor_id,)).fetchone():
                    raise ApiError(400, "pick a vendor")
                c.execute("UPDATE sessions SET closed_at=? WHERE device_id=? AND closed_at IS NULL", (now, device_id))
                c.execute("UPDATE devices SET mode='vendor', vendor_id=? WHERE id=?", (vendor_id, device_id))
            elif mode == "idle":
                c.execute("UPDATE sessions SET closed_at=? WHERE device_id=? AND closed_at IS NULL", (now, device_id))
                c.execute("UPDATE devices SET mode='idle', vendor_id=NULL WHERE id=?", (device_id,))
            else:
                raise ApiError(400, "unknown mode")
            if mode != "vendor":
                c.execute("UPDATE charges SET status='cancelled' WHERE device_id=? AND status='armed'", (device_id,))
            self._log(c, now, device_id, None, None, "admin", 1, "MODE_SET", mode)
        return {"mode": mode}

    def admin_session_attendance(self, session_id: int) -> list[dict]:
        with self.session() as c:
            return [dict(r) for r in c.execute(
                "SELECT s.roll_no, s.name, k.created_at ts FROM checkins k JOIN students s ON s.id=k.student_id "
                "WHERE k.session_id=? ORDER BY k.id", (session_id,))]

    def admin_register_card(self, uid: str, roll_no: str, replace: bool = False) -> dict:
        now = self.clock()
        uid = (uid or "").strip().upper()
        if not re.fullmatch(r"[0-9A-F]{14}", uid):
            raise ApiError(400, "uid must be 14 hex characters")
        with self.session() as c, tx(c):
            st = c.execute("SELECT * FROM students WHERE roll_no=?", ((roll_no or "").strip().upper(),)).fetchone()
            if not st:
                raise ApiError(404, "unknown student")
            if c.execute("SELECT 1 FROM cards WHERE uid=?", (uid,)).fetchone():
                raise ApiError(409, "this card is already registered")
            t = c.execute("SELECT MAX(counter) m FROM taps WHERE uid=? AND code='UNREGISTERED'", (uid,)).fetchone()
            if t["m"] is None:
                raise ApiError(409, "tap this card on a reader first so its signature can be verified")
            live = c.execute("SELECT id FROM cards WHERE student_id=? AND status!='retired'", (st["id"],)).fetchone()
            if live:
                if not replace:
                    raise ApiError(409, "student already has a card, use replace")
                c.execute("UPDATE cards SET status='retired' WHERE id=?", (live["id"],))
            c.execute("INSERT INTO cards(uid, student_id, last_counter, issued_at) VALUES (?,?,?,?)",
                      (uid, st["id"], t["m"], now))
            self._log(c, now, None, uid, st["id"], "admin", 1, "CARD_ISSUED", "replacement" if live else "new")
        return {"ok": True}
