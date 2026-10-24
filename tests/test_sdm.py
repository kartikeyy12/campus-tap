import os
import random

import pytest

from app import sdm
from app.config import Config
from tools.simulator import SimCard, make_ndef

# NXP AN12196 worked example (all-zero keys). The MAC value is NXP's published one.
NXP_PICC = "EF963FF7828658A599F3041510671E88"
NXP_MAC = "94EED9EE65337086"


def test_nxp_vector_decrypt_and_mac():
    uid, ctr = sdm.decrypt_picc(bytes(16), bytes.fromhex(NXP_PICC))
    assert uid.hex().upper() == "04DE5F1EACC040" and ctr == 61
    assert sdm.compute_mac(bytes(16), uid, ctr).hex().upper() == NXP_MAC


def test_nxp_vector_full_verify():
    assert sdm.verify(Config(db_path="x"), NXP_PICC, NXP_MAC) == ("04DE5F1EACC040", 61)


def test_nxp_vector_wrong_mac_rejected():
    with pytest.raises(sdm.SDMError):
        sdm.verify(Config(db_path="x"), NXP_PICC, "94EED9EE65337087")


def test_roundtrip_default_keys_counter_increments():
    cfg = Config(db_path="x")
    card = SimCard("04AABBCCDDEE11", cfg, counter=5)
    seen = []
    for _ in range(3):
        picc, mac = card.read()
        seen.append(sdm.verify(cfg, picc, mac))
    assert seen == [("04AABBCCDDEE11", 5), ("04AABBCCDDEE11", 6), ("04AABBCCDDEE11", 7)]


def _diversified(master="11" * 16, meta="22" * 16):
    return Config(db_path="x", key_mode="diversified", master_key=bytes.fromhex(master), meta_key=bytes.fromhex(meta))


def test_diversified_keys_roundtrip_and_isolation():
    cfg = _diversified()
    a, b = SimCard("04AAAAAAAAAAAA", cfg), SimCard("04BBBBBBBBBBBB", cfg)
    assert sdm.verify(cfg, *a.read())[0] == "04AAAAAAAAAAAA"
    assert sdm.verify(cfg, *b.read())[0] == "04BBBBBBBBBBBB"
    # card A's picc with card B's MAC must not verify
    picc_a, _ = a.read()
    _, mac_b = b.read()
    with pytest.raises(sdm.SDMError):
        sdm.verify(cfg, picc_a, mac_b)


def test_wrong_master_key_rejected():
    good, bad = _diversified(), _diversified(master="33" * 16)
    with pytest.raises(sdm.SDMError):
        sdm.verify(bad, *SimCard("04AAAAAAAAAAAA", good).read())


def test_wrong_meta_key_rejected():
    good, bad = _diversified(), _diversified(meta="44" * 16)
    with pytest.raises(sdm.SDMError):
        sdm.verify(bad, *SimCard("04AAAAAAAAAAAA", good).read())


def test_every_single_nibble_flip_is_rejected():
    cfg = Config(db_path="x")
    picc, mac = SimCard("04AABBCCDDEE11", cfg).read()
    for s, label in ((picc, "picc"), (mac, "mac")):
        for i in range(len(s)):
            flipped = s[:i] + ("0" if s[i] != "0" else "1") + s[i + 1:]
            args = (flipped, mac) if label == "picc" else (picc, flipped)
            with pytest.raises(sdm.SDMError):
                sdm.verify(cfg, *args)


def test_bad_lengths_and_non_hex():
    cfg = Config(db_path="x")
    for picc, mac in [("", ""), ("ZZ" * 16, NXP_MAC), (NXP_PICC, "12"), (NXP_PICC[:-2], NXP_MAC), (None, None)]:
        with pytest.raises(sdm.SDMError):
            sdm.verify(cfg, picc, mac)


def test_ndef_roundtrip_and_param_names():
    url = "https://campus.local/t?p=" + NXP_PICC + "&m=" + NXP_MAC
    assert sdm.ndef_to_url(make_ndef(url)) == url
    assert sdm.parse_tap(ndef=make_ndef(url)) == (NXP_PICC, NXP_MAC)
    long_names = f"https://x.io/a?picc_data={NXP_PICC}&cmac={NXP_MAC}"
    assert sdm.parse_tap(url=long_names) == (NXP_PICC, NXP_MAC)
    lower = url.replace(NXP_PICC, NXP_PICC.lower()).replace(NXP_MAC, NXP_MAC.lower())
    assert sdm.verify(Config(db_path="x"), *sdm.parse_tap(url=lower)) == ("04DE5F1EACC040", 61)


def test_garbage_ndef_only_ever_raises_sdmerror():
    rng = random.Random(1234)
    cfg = Config(db_path="x")
    good = make_ndef(f"https://campus.local/t?p={NXP_PICC}&m={NXP_MAC}")
    samples = [os.urandom(rng.randint(0, 120)) for _ in range(400)]
    for _ in range(400):  # mutate a valid message
        b = bytearray(good)
        for _ in range(rng.randint(1, 4)):
            b[rng.randrange(len(b))] = rng.randrange(256)
        samples.append(bytes(b[:rng.randint(0, len(b))]))
    for s in samples:
        try:
            picc, mac = sdm.parse_tap(ndef=s)
            sdm.verify(cfg, picc, mac)
        except sdm.SDMError:
            pass
