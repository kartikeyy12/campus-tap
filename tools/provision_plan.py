"""Work out exactly what to write onto an NTAG 424 DNA card so the server can verify its taps.

    python -m tools.provision_plan
    python -m tools.provision_plan --base https://campus.local/t

It prints the NDEF message to write and the three SDM offsets to enter in whatever tool you use to
configure the card (they are byte positions inside the NDEF *file*, counting its 2-byte length field).
The server expects: UID mirror ON, read-counter mirror ON, ASCII encoding, PICC data encrypted, and a
MAC whose input is empty (SDMMACInputOffset == SDMMACOffset).
"""
from __future__ import annotations

import argparse

PICC_HEX_LEN = 32     # 16 bytes of encrypted PICC data, as ASCII hex
MAC_HEX_LEN = 16      # 8 bytes of truncated CMAC, as ASCII hex

# longest first so "https://www." wins over "https://"
URI_PREFIXES = [("https://www.", 0x02), ("http://www.", 0x01), ("https://", 0x04), ("http://", 0x03)]


def le3(n: int) -> str:
    return n.to_bytes(3, "little").hex().upper()


def plan(base_url: str = "https://campus.local/t", picc_param: str = "p", mac_param: str = "m") -> dict:
    code, rest = 0x00, base_url
    for prefix, c in URI_PREFIXES:
        if base_url.startswith(prefix):
            code, rest = c, base_url[len(prefix):]
            break
    before_picc = f"{rest}?{picc_param}="
    between = f"&{mac_param}="
    uri_tail = before_picc + "0" * PICC_HEX_LEN + between + "0" * MAC_HEX_LEN
    payload = bytes([code]) + uri_tail.encode("ascii")
    if len(payload) > 255:
        raise ValueError("URL too long for a short NDEF record; use a shorter base URL")
    record = bytes([0xD1, 0x01, len(payload), 0x55]) + payload
    ndef_file = len(record).to_bytes(2, "big") + record

    # file offsets: 2 (NLEN) + 4 (record header) + 1 (URI prefix code) precede the URL text
    head = 2 + 4 + 1
    picc_offset = head + len(before_picc)
    mac_offset = picc_offset + PICC_HEX_LEN + len(between)
    assert ndef_file[picc_offset:picc_offset + PICC_HEX_LEN] == b"0" * PICC_HEX_LEN
    assert ndef_file[mac_offset:mac_offset + MAC_HEX_LEN] == b"0" * MAC_HEX_LEN
    return {
        "url_template": base_url + f"?{picc_param}=" + "0" * PICC_HEX_LEN + between + "0" * MAC_HEX_LEN,
        "ndef_file_hex": ndef_file.hex().upper(),
        "ndef_file_len": len(ndef_file),
        "picc_data_offset": picc_offset,
        "mac_offset": mac_offset,
        "mac_input_offset": mac_offset,      # equal to the MAC offset => empty MAC input
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="https://campus.local/t")
    ap.add_argument("--picc-param", default="p")
    ap.add_argument("--mac-param", default="m")
    a = ap.parse_args()
    p = plan(a.base, a.picc_param, a.mac_param)
    print("NDEF URL to write (placeholders are zeros):")
    print("  " + p["url_template"])
    print(f"NDEF file content ({p['ndef_file_len']} bytes):")
    print("  " + p["ndef_file_hex"])
    print("SDM offsets (decimal, and as 3 bytes little endian):")
    print(f"  PICCDataOffset    {p['picc_data_offset']:>4}   {le3(p['picc_data_offset'])}")
    print(f"  SDMMACOffset      {p['mac_offset']:>4}   {le3(p['mac_offset'])}")
    print(f"  SDMMACInputOffset {p['mac_input_offset']:>4}   {le3(p['mac_input_offset'])}   (same as MAC offset)")
    print("Other settings: UID mirror ON, read counter mirror ON, ASCII encoding, PICC data encrypted with the")
    print("meta-read key, MAC with the file-read key, read-counter limit OFF.")


if __name__ == "__main__":
    main()
