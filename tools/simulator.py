"""Software model of an NTAG 424 DNA card, so the server can be tested without hardware.

It mirrors what the chip does on each NDEF read: bump the read counter, encrypt
(tag | UID | counter | padding) under the meta key, and MAC with the per-tap session key.
The verifier in app/sdm.py is independently checked against NXP's published example vector.
"""
from __future__ import annotations

import os

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from app import sdm
from app.config import Config

BASE_URL = "https://campus.local/t"


class SimCard:
    def __init__(self, uid_hex: str, cfg: Config, counter: int = 0):
        self.uid = bytes.fromhex(uid_hex)
        assert len(self.uid) == 7
        self.cfg = cfg
        self.counter = counter          # value that the NEXT read will mirror

    @property
    def uid_hex(self) -> str:
        return self.uid.hex().upper()

    def read(self) -> tuple[str, str]:
        """One tap -> (picc_data_hex, cmac_hex). The chip's counter advances by one per read."""
        ctr = self.counter
        self.counter += 1
        meta_key, _ = sdm.keys_for(self.cfg)
        _, file_key = sdm.keys_for(self.cfg, self.uid)
        plain = bytes([0xC7]) + self.uid + ctr.to_bytes(3, "little") + os.urandom(5)
        enc = Cipher(algorithms.AES(meta_key), modes.CBC(bytes(16))).encryptor()
        picc = enc.update(plain) + enc.finalize()
        mac = sdm.compute_mac(file_key, self.uid, ctr)
        return picc.hex().upper(), mac.hex().upper()

    def read_ndef(self, base: str = BASE_URL) -> bytes:
        """The NDEF file content a reader would get from READ BINARY (NLEN + short URI record)."""
        picc, mac = self.read()
        return make_ndef(f"{base}?p={picc}&m={mac}")


def make_ndef(url: str) -> bytes:
    assert url.startswith("https://")
    payload = b"\x04" + url[len("https://"):].encode("ascii")
    rec = bytes([0xD1, 0x01, len(payload), 0x55]) + payload
    return len(rec).to_bytes(2, "big") + rec
