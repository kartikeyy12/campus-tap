"""NTAG 424 DNA Secure Unique NFC (SUN / SDM) verification.

A tap yields a URL carrying two values:
  picc_data : 16 bytes, AES-128-CBC (zero IV) under the SDM *meta read* key.
              Plaintext = tag byte (0xC7) | UID (7) | SDMReadCtr (3, little endian) | padding
  cmac      : 8 bytes, the odd-indexed bytes of AES-CMAC(session MAC key, mac_input)
              session MAC key = AES-CMAC(file read key, SV2)
              SV2 = 3C C3 00 01 00 80 | UID | SDMReadCtr

Reference: NXP AN12196 (NTAG 424 DNA features and hints). The card is expected to be
provisioned so the MAC input is empty (SDMMACInputOffset == SDMMACOffset).
"""
from __future__ import annotations

import hmac
import re
from urllib.parse import parse_qs, urlsplit

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.cmac import CMAC

from .config import Config


class SDMError(Exception):
    """The tap message is malformed or fails authentication."""


def aes_cmac(key: bytes, data: bytes) -> bytes:
    c = CMAC(algorithms.AES(key))
    c.update(data)
    return c.finalize()


def diversify(master_key: bytes, uid: bytes) -> bytes:
    """Per-card file-read key from the master key. Our own scheme (not NXP AN10922)."""
    return aes_cmac(master_key, b"\x01CAMPUSNFC" + uid)


def keys_for(cfg: Config, uid: bytes | None = None) -> tuple[bytes, bytes | None]:
    """(meta_key, file_read_key). file_read_key is None until the UID is known."""
    if cfg.key_mode == "default":
        return bytes(16), bytes(16)
    return cfg.meta_key, (diversify(cfg.master_key, uid) if uid is not None else None)


def decrypt_picc(meta_key: bytes, picc_enc: bytes) -> tuple[bytes, int]:
    if len(picc_enc) != 16:
        raise SDMError("picc_data must be 16 bytes")
    dec = Cipher(algorithms.AES(meta_key), modes.CBC(bytes(16))).decryptor()
    pt = dec.update(picc_enc) + dec.finalize()
    tag = pt[0]
    # bit7: UID mirrored, bit6: read counter mirrored, low nibble: UID length (must be 7)
    if not (tag & 0x80) or not (tag & 0x40) or (tag & 0x0F) != 7:
        raise SDMError("unexpected PICC data format")
    return pt[1:8], int.from_bytes(pt[8:11], "little")


def compute_mac(file_key: bytes, uid: bytes, ctr: int, mac_input: bytes = b"") -> bytes:
    sv2 = bytes([0x3C, 0xC3, 0x00, 0x01, 0x00, 0x80]) + uid + ctr.to_bytes(3, "little")
    session_key = aes_cmac(file_key, sv2)
    return aes_cmac(session_key, mac_input)[1::2]


def verify(cfg: Config, picc_hex: str, mac_hex: str) -> tuple[str, int]:
    """Return (uid_hex, counter) if the tap authenticates, else raise SDMError."""
    if not re.fullmatch(r"[0-9A-Fa-f]{32}", picc_hex or ""):
        raise SDMError("picc_data must be 32 hex chars")
    if not re.fullmatch(r"[0-9A-Fa-f]{16}", mac_hex or ""):
        raise SDMError("cmac must be 16 hex chars")
    meta_key, _ = keys_for(cfg)
    uid, ctr = decrypt_picc(meta_key, bytes.fromhex(picc_hex))
    _, file_key = keys_for(cfg, uid)
    expected = compute_mac(file_key, uid, ctr)
    if not hmac.compare_digest(expected, bytes.fromhex(mac_hex)):
        raise SDMError("bad signature")
    return uid.hex().upper(), ctr


# ---------------------------------------------------------------- NDEF / URL parsing

_URI_PREFIX = {0x00: "", 0x01: "http://www.", 0x02: "https://www.", 0x03: "http://", 0x04: "https://"}


def ndef_to_url(data: bytes) -> str:
    """Parse the NDEF file content (2-byte NLEN + one short URI record) into a URL."""
    if len(data) < 9:
        raise SDMError("NDEF too short")
    nlen = int.from_bytes(data[:2], "big")
    rec = data[2:2 + nlen]
    if len(rec) != nlen or nlen < 5:
        raise SDMError("NDEF length mismatch")
    header, type_len, pay_len = rec[0], rec[1], rec[2]
    if not (header & 0x10) or type_len != 1 or rec[3] != 0x55:  # SR flag, type 'U'
        raise SDMError("not a short URI record")
    payload = rec[4:4 + pay_len]
    if len(payload) != pay_len or pay_len < 1:
        raise SDMError("bad URI payload")
    prefix = _URI_PREFIX.get(payload[0])
    if prefix is None:
        raise SDMError("unknown URI prefix")
    try:
        return prefix + payload[1:].decode("ascii")
    except UnicodeDecodeError as e:
        raise SDMError("URL not ascii") from e


def url_to_params(url: str) -> tuple[str, str]:
    q = parse_qs(urlsplit(url).query)

    def one(*names: str) -> str:
        for n in names:
            v = q.get(n)
            if v and len(v) == 1:
                return v[0]
        raise SDMError(f"missing parameter {names[0]}")

    return one("picc_data", "p"), one("cmac", "m")


def parse_tap(ndef: bytes | None = None, url: str | None = None) -> tuple[str, str]:
    if url is None:
        if ndef is None:
            raise SDMError("no tap data")
        url = ndef_to_url(ndef)
    return url_to_params(url)
