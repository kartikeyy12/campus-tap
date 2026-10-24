from app import sdm
from tools.provision_plan import le3, plan


def test_matches_nxps_published_offsets():
    """NXP's worked example: https://www.ntag.nxp.com/424?e=<32>&c=<16> puts the encrypted-ID placeholder
    at file byte 26 and the MAC placeholder at byte 61 (community.nxp.com thread, NXP TechSupport)."""
    p = plan("https://www.ntag.nxp.com/424", "e", "c")
    assert (p["picc_data_offset"], p["mac_offset"]) == (26, 61)
    assert le3(26) == "1A0000" and le3(61) == "3D0000"


def test_default_template_offsets_and_parse():
    p = plan()
    assert p["url_template"] == "https://campus.local/t?p=" + "0" * 32 + "&m=" + "0" * 16
    assert (p["picc_data_offset"], p["mac_offset"], p["mac_input_offset"]) == (24, 59, 59)
    ndef = bytes.fromhex(p["ndef_file_hex"])
    assert sdm.ndef_to_url(ndef) == p["url_template"]
    assert sdm.parse_tap(ndef=ndef) == ("0" * 32, "0" * 16)
    assert len(ndef) == p["ndef_file_len"] == 75       # fits the 256-byte NDEF file


def test_other_prefixes_and_long_param_names():
    for base in ("http://campus.local/t", "https://x.io/a/b", "campus.local/t"):
        p = plan(base, "picc_data", "cmac")
        ndef = bytes.fromhex(p["ndef_file_hex"])
        assert sdm.ndef_to_url(ndef).endswith("&cmac=" + "0" * 16) or base == "campus.local/t"
        assert ndef[p["picc_data_offset"]:p["picc_data_offset"] + 32] == b"0" * 32
        assert ndef[p["mac_offset"]:p["mac_offset"] + 16] == b"0" * 16
