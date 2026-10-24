"""SQLite schema and connection helpers.

Safety rules live in the database so a bug in application code gets rejected instead of saved:
  * ledger is append-only (no UPDATE / DELETE)
  * student and vendor accounts can never go negative
  * one open session per reader, one armed charge per reader
  * one check-in per student per session, one tap per (card uid, counter)
"""
from __future__ import annotations

import os
import re
import sqlite3
from contextlib import contextmanager

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts(
  id   INTEGER PRIMARY KEY,
  kind TEXT NOT NULL CHECK(kind IN ('student','vendor','system')),
  name TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS students(
  id         INTEGER PRIMARY KEY,
  roll_no    TEXT NOT NULL UNIQUE,
  name       TEXT NOT NULL,
  pin_hash   TEXT NOT NULL,
  account_id INTEGER NOT NULL UNIQUE REFERENCES accounts(id)
);

CREATE TABLE IF NOT EXISTS vendors(
  id         INTEGER PRIMARY KEY,
  username   TEXT NOT NULL UNIQUE,
  name       TEXT NOT NULL,
  pin_hash   TEXT NOT NULL,
  account_id INTEGER NOT NULL UNIQUE REFERENCES accounts(id)
);

CREATE TABLE IF NOT EXISTS cards(
  id           INTEGER PRIMARY KEY,
  uid          TEXT NOT NULL UNIQUE CHECK(length(uid) = 14),
  student_id   INTEGER NOT NULL REFERENCES students(id),
  status       TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','blocked','retired')),
  last_counter INTEGER NOT NULL DEFAULT -1,
  issued_at    REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS one_live_card_per_student
  ON cards(student_id) WHERE status != 'retired';

CREATE TABLE IF NOT EXISTS devices(
  id        TEXT PRIMARY KEY,
  secret    TEXT NOT NULL,
  mode      TEXT NOT NULL DEFAULT 'idle' CHECK(mode IN ('idle','attendance','mess','vendor')),
  vendor_id INTEGER REFERENCES vendors(id),
  last_seen REAL
);

CREATE TABLE IF NOT EXISTS sessions(
  id        INTEGER PRIMARY KEY,
  kind      TEXT NOT NULL CHECK(kind IN ('class','meal')),
  title     TEXT NOT NULL,
  device_id TEXT NOT NULL REFERENCES devices(id),
  opened_at REAL NOT NULL,
  closed_at REAL
);
CREATE UNIQUE INDEX IF NOT EXISTS one_open_session_per_device
  ON sessions(device_id) WHERE closed_at IS NULL;

CREATE TABLE IF NOT EXISTS checkins(
  id         INTEGER PRIMARY KEY,
  session_id INTEGER NOT NULL REFERENCES sessions(id),
  student_id INTEGER NOT NULL REFERENCES students(id),
  created_at REAL NOT NULL,
  UNIQUE(session_id, student_id)
);

CREATE TABLE IF NOT EXISTS charges(
  id           INTEGER PRIMARY KEY,
  vendor_id    INTEGER NOT NULL REFERENCES vendors(id),
  device_id    TEXT NOT NULL REFERENCES devices(id),
  amount       INTEGER NOT NULL CHECK(amount > 0),
  status       TEXT NOT NULL CHECK(status IN ('armed','completed','expired','cancelled')),
  created_at   REAL NOT NULL,
  expires_at   REAL NOT NULL,
  student_id   INTEGER REFERENCES students(id),
  completed_at REAL,
  txn_id       INTEGER REFERENCES txns(id)
);
CREATE UNIQUE INDEX IF NOT EXISTS one_armed_charge_per_device
  ON charges(device_id) WHERE status = 'armed';

CREATE TABLE IF NOT EXISTS txns(
  id         INTEGER PRIMARY KEY,
  kind       TEXT NOT NULL CHECK(kind IN ('payment','topup','adjust')),
  memo       TEXT NOT NULL,
  created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS ledger(
  id         INTEGER PRIMARY KEY,
  txn_id     INTEGER NOT NULL REFERENCES txns(id),
  account_id INTEGER NOT NULL REFERENCES accounts(id),
  amount     INTEGER NOT NULL CHECK(amount != 0),
  created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ledger_by_account ON ledger(account_id);
CREATE INDEX IF NOT EXISTS ledger_by_txn ON ledger(txn_id);

CREATE TRIGGER IF NOT EXISTS ledger_no_update BEFORE UPDATE ON ledger
BEGIN SELECT RAISE(ABORT, 'ledger is append-only'); END;

CREATE TRIGGER IF NOT EXISTS ledger_no_delete BEFORE DELETE ON ledger
BEGIN SELECT RAISE(ABORT, 'ledger is append-only'); END;

CREATE TRIGGER IF NOT EXISTS ledger_no_overdraft BEFORE INSERT ON ledger
WHEN NEW.amount < 0
 AND (SELECT kind FROM accounts WHERE id = NEW.account_id) IN ('student','vendor')
BEGIN
  SELECT RAISE(ABORT, 'insufficient funds')
  WHERE (SELECT COALESCE(SUM(amount), 0) FROM ledger WHERE account_id = NEW.account_id) + NEW.amount < 0;
END;

CREATE TABLE IF NOT EXISTS taps(
  id         INTEGER PRIMARY KEY,
  uid        TEXT NOT NULL,
  counter    INTEGER NOT NULL,
  device_id  TEXT NOT NULL,
  card_id    INTEGER REFERENCES cards(id),
  mode       TEXT NOT NULL,
  ok         INTEGER NOT NULL,
  code       TEXT NOT NULL,
  response   TEXT NOT NULL,
  created_at REAL NOT NULL,
  UNIQUE(uid, counter)
);
CREATE INDEX IF NOT EXISTS taps_recent ON taps(card_id, device_id, mode, created_at);

CREATE TABLE IF NOT EXISTS events(
  id         INTEGER PRIMARY KEY,
  created_at REAL NOT NULL,
  device_id  TEXT,
  uid        TEXT,
  student_id INTEGER,
  kind       TEXT NOT NULL,
  ok         INTEGER NOT NULL,
  code       TEXT NOT NULL,
  detail     TEXT NOT NULL DEFAULT ''
);

CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;

CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;

CREATE TABLE IF NOT EXISTS auth_tokens(
  token      TEXT PRIMARY KEY,
  role       TEXT NOT NULL CHECK(role IN ('student','vendor','admin')),
  subject_id INTEGER NOT NULL,
  expires_at REAL NOT NULL
);
"""


def connect(path: str) -> sqlite3.Connection:
    """New connection with manual transaction control (use `tx`)."""
    conn = sqlite3.connect(path, timeout=15, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 15000")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = FULL")
    return conn


def on_windows_mount(path: str, proc_version: str | None = None) -> bool:
    """True when running under WSL and `path` lives on a Windows drive (/mnt/c, /mnt/d, ...).

    SQLite's WAL mode needs real file locking and shared memory. WSL's Windows mounts do not provide
    them reliably, which shows up as 'database is locked' or I/O errors in the middle of a demo."""
    if proc_version is None:
        try:
            with open("/proc/version") as f:
                proc_version = f.read()
        except OSError:
            return False
    if "microsoft" not in proc_version.lower():
        return False
    return re.match(r"^/mnt/[a-z]/", os.path.realpath(path)) is not None


def init_db(path: str) -> None:
    if os.environ.get("CAMPUS_ALLOW_MNT") != "1" and on_windows_mount(path):
        raise RuntimeError(
            f"The database path {path!r} is on a Windows drive. Copy the project into your WSL home "
            "folder (for example ~/nfc-campus) and run it from there. "
            "Set CAMPUS_ALLOW_MNT=1 to override at your own risk.")
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    conn = connect(path)
    try:
        conn.executescript(SCHEMA)
        # system account that top-ups are funded from, so the whole ledger always sums to zero
        if not conn.execute("SELECT 1 FROM accounts WHERE kind='system'").fetchone():
            conn.execute("INSERT INTO accounts(kind, name) VALUES('system', 'Top-up source')")
    finally:
        conn.close()


@contextmanager
def tx(conn: sqlite3.Connection):
    """One atomic write transaction. BEGIN IMMEDIATE takes the write lock up front,
    so check-then-write sequences inside cannot race with other writers."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")
