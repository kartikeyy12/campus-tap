"""Checks the reader's hardware-free C++ core (firmware/reader/core.h) against the real server.

Expected values are produced here, in Python, by the actual server code, hmac/hashlib and the card
simulator. The C++ is compiled with warnings as errors plus address/undefined-behaviour sanitizers.
"""
import hashlib
import hmac
import json
import math
import os
import random
import shutil
import subprocess

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.service import sign_body
from tools.provision_plan import plan
from tools.simulator import make_ndef

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FW = os.path.join(ROOT, "firmware")

pytestmark = pytest.mark.skipif(shutil.which("g++") is None, reason="g++ not installed")


@pytest.fixture(scope="module")
def binary(tmp_path_factory):
    out = str(tmp_path_factory.mktemp("fw") / "test_core")
    base = ["g++", "-std=c++11", "-Wall", "-Wextra", "-Werror", "-O1", "-g",
            os.path.join(FW, "tests", "test_core.cpp"), "-o", out]
    r = subprocess.run(base + ["-fsanitize=address,undefined", "-fno-sanitize-recover=undefined"],
                       capture_output=True, text=True)
    if r.returncode != 0:   # sanitizers not installed: still compile strictly
        r = subprocess.run(base, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return out


@pytest.fixture
def api(w):
    client = TestClient(create_app(service=w.svc))
    with w.svc.session() as c:
        secret = c.execute("SELECT secret FROM devices WHERE id='reader-1'").fetchone()["secret"]
    return client, w, secret


def run(binary, tmp_path, lines):
    p = tmp_path / "vectors.tsv"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    r = subprocess.run([binary, str(p)], capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "FAIL 0" in r.stdout, r.stdout
    return r.stdout


def post_tap(client, secret, ndef_hex, charge_id=None):
    body = json.dumps({"ndef": ndef_hex, "charge_id": charge_id}).encode()
    return client.post("/api/device/tap", content=body,
                       headers={"X-Device-Id": "reader-1", "X-Signature": sign_body(secret, body)})


def get_poll(client, secret):
    return client.get("/api/device/poll", headers={"X-Device-Id": "reader-1", "X-Signature": sign_body(secret, b"poll")})


def reply_line(text):
    d = json.loads(text)
    return "\t".join(["REPLY", text, "1" if d["ok"] else "0", d["code"], d["big"], d["l1"], d["l2"], d["tone"]])


def poll_line(text):
    d = json.loads(text)
    ch = d.get("charge")
    return "\t".join(["POLL", text, d["mode"], d.get("title", ""), d.get("vendor", ""), "1" if ch else "0",
                      str(ch["id"]) if ch else "0", str(ch["amount"]) if ch else "0"])


def test_hmac_matches_python_for_awkward_lengths(binary, tmp_path):
    rng = random.Random(7)
    lines = []
    for klen in (0, 1, 16, 32, 63, 64, 65, 100):
        for mlen in (0, 1, 3, 4, 55, 56, 57, 63, 64, 65, 119, 120, 121, 128, 300, 1000):
            key, msg = rng.randbytes(klen), rng.randbytes(mlen)
            lines.append(f"HMAC\t{key.hex()}\t{msg.hex()}\t{hmac.new(key, msg, hashlib.sha256).hexdigest()}")
    key = bytes.fromhex("00112233445566778899aabbccddeeff")
    lines.append(f"HMAC\t{key.hex()}\t{b'poll'.hex()}\t{hmac.new(key, b'poll', hashlib.sha256).hexdigest()}")
    run(binary, tmp_path, lines)


def test_signature_the_server_accepts(api):
    """The C++ HMAC must produce exactly what Service.device_auth checks."""
    client, w, secret = api
    assert sign_body(secret, b"poll") == hmac.new(bytes.fromhex(secret), b"poll", hashlib.sha256).hexdigest()
    assert get_poll(client, secret).status_code == 200


def test_request_body_matches_what_the_server_parses(binary, tmp_path, api):
    client, w, secret = api
    lines = []
    for cid in (None, 1, 987654):
        ndef = w.cards["S1"].read_ndef()
        expected = json.dumps({"ndef": ndef.hex().upper(), "charge_id": cid}, separators=(",", ":"))
        lines.append(f"BODY\t{ndef.hex()}\t{'-' if cid is None else cid}\t{expected}")
    run(binary, tmp_path, lines)


def test_real_server_replies_are_read_correctly(binary, tmp_path, api):
    client, w, secret = api
    texts = []

    def tap(roll, cid=None):
        r = post_tap(client, secret, w.cards[roll].read_ndef().hex(), cid)
        assert r.status_code == 200, r.text
        texts.append(r.text)
        return json.loads(r.text)

    tap("S1")                                                    # IDLE
    w.svc.admin_open_session("reader-1", "meal", "Lunch")
    tap("S1"); tap("S1")                                         # MEAL_OK, then ALREADY after the debounce
    w.clock.advance(60); tap("S1")
    post = post_tap(client, secret, "00FF"); texts.append(post.text)   # BAD_TAP
    w.svc.admin_set_mode("reader-1", "vendor", w.v1)
    w.svc.vendor_create_charge(w.v1, 300)
    cid = get_poll(client, secret).json()["charge"]["id"]
    assert tap("S2", cid)["code"] == "NO_FUNDS"
    w.svc.vendor_create_charge(w.v1, 70)
    cid = get_poll(client, secret).json()["charge"]["id"]
    assert tap("S2", cid)["code"] == "PAID"
    tap("S2", cid)                                               # duplicate-style / no charge answer
    w.svc.student_set_block(w.sid("S3"), True)
    tap("S3")                                                    # BLOCKED
    assert len(texts) >= 8
    run(binary, tmp_path, [reply_line(t) for t in texts])


def test_reply_parser_edge_cases(binary, tmp_path):
    lines = [
        # escapes, raw and escaped non-ASCII, quotes and backslashes
        "REPLY\t" + r'{"ok":true,"code":"X","big":"Zoë","l1":"Zoë \"Q\" \\ end","l2":"","tone":"ok"}'
        + "\t1\tX\tZo?\tZo? \"Q\" \\ end\t\tok",
        "REPLY\t" + '{"ok":true,"code":"X","big":"Zoë","l1":"é\\"é","l2":"a","tone":"ok"}'
        + "\t1\tX\tZo?\t?\"?\ta\tok",
        # whitespace and key order do not matter
        "REPLY\t" + '{ "tone" : "warn" , "l2":"b","l1":"a","big":"B","code":"C", "ok" : false }'
        + "\t0\tC\tB\ta\tb\twarn",
        # a nested object with the same key names must not be mistaken for the top-level ones
        "REPLY\t" + '{"detail":{"ok":false,"code":"NESTED"},"ok":true,"code":"TOP","big":"B","l1":"","l2":"","tone":"ok"}'
        + "\t1\tTOP\tB\t\t\tok",
        # oversize text is cut, never overflows
        "REPLY\t" + '{"ok":true,"code":"X","big":"B","l1":"' + "A" * 100 + '","l2":"","tone":"ok"}'
        + "\t1\tX\tB\t" + "A" * 31 + "\t\tok",
        "REPLY_BAD\t",
        "REPLY_BAD\t[]",
        "REPLY_BAD\t" + '{"error":"bad signature"}',
        "REPLY_BAD\t" + '{"ok":"yes","code":"X","big":"B","l1":"","l2":"","tone":"ok"}',
        "REPLY_BAD\t" + '{"ok":true,"code":"X","big":"B","l1":"","l2":""}',
        "REPLY_BAD\t" + '{"ok":true,"code":"X","big":"B","l1":"unterminated',
        "REPLY_BAD\t" + '{"ok":true,"code":"X","big":"B","l1":"a\\',
    ]
    run(binary, tmp_path, lines)


def test_real_poll_answers_and_screens(binary, tmp_path, api):
    client, w, secret = api
    lines = []

    def add():
        r = get_poll(client, secret)
        assert r.status_code == 200
        lines.append(poll_line(r.text))
        return r.json()

    add()                                                              # idle
    w.svc.admin_open_session("reader-1", "class", "Math 101")
    add()
    w.svc.admin_open_session("reader-1", "meal", "Dîner")
    add()
    w.svc.admin_set_mode("reader-1", "vendor", w.v1)
    add()                                                              # vendor, no charge
    w.svc.vendor_create_charge(w.v1, 70)
    d = add()                                                          # vendor with charge
    assert d["charge"]["amount"] == 70
    lines += ["POLL_BAD\t{}", "POLL_BAD\t",
              "POLL_BAD\t" + '{"mode":"vendor","charge":{"id":"x","amount":5}}',
              "POLL_BAD\t" + '{"mode":"vendor","charge":{"amount":5}}']
    run(binary, tmp_path, lines)

    vend = '{"mode":"vendor","server_time":1.0,"title":"","charge":{"id":9,"amount":70,"ttl":30.0},"vendor":"Shop"}'
    scr = []
    for age in (0, 12.9995, 13, 29.5, 29.999, 30, 45):
        left = 30.0 - age
        if left > 0:
            scr.append(f"SCREEN\t{vend}\t{age}\tRs 70\tShop\tTap to pay  {math.ceil(left)}s\t9")
        else:
            scr.append(f"SCREEN\t{vend}\t{age}\tVENDOR\tShop\tWaiting for amount\t-")
    nocharge = '{"mode":"vendor","title":"","charge":null,"vendor":"Shop"}'
    scr.append(f"SCREEN\t{nocharge}\t0\tVENDOR\tShop\tWaiting for amount\t-")
    scr.append('SCREEN\t{"mode":"idle","title":"","charge":null,"vendor":""}\t0\tIDLE\tReader not in use\tAsk the admin\t-')
    scr.append('SCREEN\t{"mode":"attendance","title":"Physics Lab 3","charge":null,"vendor":""}\t0\tATTEND\tPhysics Lab 3\tTap your card\t-')
    scr.append('SCREEN\t{"mode":"mess","title":"Lunch","charge":null,"vendor":""}\t0\tMESS\tLunch\tTap your card\t-')
    long_title = "T" * 40
    scr.append(f'SCREEN\t{{"mode":"mess","title":"{long_title}","charge":null,"vendor":""}}\t0\tMESS\t{long_title[:21]}\tTap your card\t-')
    scr.append('SCREEN\t{"mode":"surprise","title":"","charge":null,"vendor":""}\t0\tIDLE\tReader not in use\tAsk the admin\t-')
    big = '{"mode":"vendor","title":"","charge":{"id":1,"amount":500,"ttl":30.0},"vendor":"A Very Long Vendor Name Indeed"}'
    scr.append(f"SCREEN\t{big}\t0\tRs 500\tA Very Long Vendor Na\tTap to pay  30s\t1")
    run(binary, tmp_path, scr)


def test_card_reading_sequence_on_real_card_data(binary, tmp_path, api):
    client, w, secret = api
    rng = random.Random(3)
    files = [w.cards["S1"].read_ndef(), bytes.fromhex(plan()["ndef_file_hex"]),
             make_ndef("https://campus.local/t?p=" + "A" * 60 + "&m=" + "B" * 16)]
    for nlen in (5, 31, 32, 33, 63, 64, 65, 100, 126):
        files.append(nlen.to_bytes(2, "big") + rng.randbytes(nlen))
    run(binary, tmp_path, ["NDEF\t" + f.hex() for f in files])


def test_reader_core_output_is_accepted_end_to_end(api):
    """Whatever the reader's chunked read returns, once reassembled, is exactly what a tap needs."""
    client, w, secret = api
    w.svc.admin_open_session("reader-1", "meal", "Lunch")
    ndef = w.cards["S1"].read_ndef()
    r = post_tap(client, secret, ndef.hex().upper())
    assert r.json()["code"] == "MEAL_OK"


def test_sketch_compiles_against_stub_arduino_headers(tmp_path):
    """Catches typos and type errors in reader.ino. The stubs only mirror the library signatures, so this
    does not prove the sketch works on a board."""
    shutil.copy(os.path.join(FW, "reader", "config.example.h"), tmp_path / "config.h")
    r = subprocess.run(["g++", "-std=gnu++11", "-fsyntax-only", "-Wall", "-Wextra", "-Werror", "-x", "c++",
                        "-I", os.path.join(FW, "tests", "stubs"), "-I", str(tmp_path),
                        "-I", os.path.join(FW, "reader"), os.path.join(FW, "reader", "reader.ino")],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
