"""Central configuration. Everything tunable lives here, read from env vars."""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _hex_env(name: str, default: str) -> bytes:
    raw = os.environ.get(name, default)
    b = bytes.fromhex(raw)
    if len(b) != 16:
        raise ValueError(f"{name} must be 16 bytes (32 hex chars)")
    return b


@dataclass
class Config:
    db_path: str = field(default_factory=lambda: os.environ.get("CAMPUS_DB", "data/campus.db"))

    # --- NTAG424 SDM keys -------------------------------------------------
    # "default": cards still have the factory all-zero keys (fine for a demo).
    # "diversified": shared meta key + per-card file-read key derived from a master key.
    key_mode: str = field(default_factory=lambda: os.environ.get("CAMPUS_KEY_MODE", "default"))
    meta_key: bytes = field(default_factory=lambda: _hex_env("CAMPUS_META_KEY", "00" * 16))
    master_key: bytes = field(default_factory=lambda: _hex_env("CAMPUS_MASTER_KEY", "00" * 16))

    # --- money rules (whole rupees) ----------------------------------------
    max_txn: int = 500          # largest single payment
    daily_cap: int = 1500       # most a student can spend per day
    max_balance: int = 5000     # wallet ceiling
    max_topup: int = 2000       # largest single demo top-up
    charge_ttl: float = 30.0    # seconds a vendor charge stays armed

    # --- tap handling ------------------------------------------------------
    debounce_s: float = 2.0     # same card, same reader, same mode within this window = same tap
    device_online_s: float = 6.0

    # --- misc --------------------------------------------------------------
    tz_offset_min: int = 330    # IST, only used for "today" in the daily cap
    admin_password: str = field(default_factory=lambda: os.environ.get("CAMPUS_ADMIN_PASSWORD", "admin123"))
    token_ttl: float = 12 * 3600
    login_max_fails: int = 5
    login_lock_s: float = 60.0

    def __post_init__(self) -> None:
        if self.key_mode not in ("default", "diversified"):
            raise ValueError("CAMPUS_KEY_MODE must be 'default' or 'diversified'")
