"""The HTTP layer: signatures, roles, and one whole story end to end."""
import json

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.service import sign_body


@pytest.fixture
def api(w):
    client = TestClient(create_app(service=w.svc))
    with w.svc.session() as c:
        secret = c.execute("SELECT secret FROM devices WHERE id='reader-1'").fetchone()["secret"]
    return client, w, secret


def tap(client, secret, ndef_hex, charge_id=None, device="reader-1", sig=None):
    body = json.dumps({"ndef": ndef_hex, "charge_id": charge_id}).encode()
    return client.post("/api/device/tap", content=body,
                       headers={"X-Device-Id": device, "X-Signature": sig or sign_body(secret, body)})


def poll(client, secret, device="reader-1"):
    return client.get("/api/device/poll", headers={"X-Device-Id": device, "X-Signature": sign_body(secret, b"poll")})


def login(client, role, ident, pin):
    r = client.post("/api/login", json={"role": role, "ident": ident, "pin": pin})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["token"]}


def test_pages_and_static_are_served_and_cannot_escape(api):
    client, *_ = api
    for p in ("/", "/student", "/vendor", "/admin"):
        r = client.get(p)
        assert r.status_code == 200 and "text/html" in r.headers["content-type"], p
    assert client.get("/static/%2e%2e/service.py").status_code in (400, 404)
    assert client.get("/static/..%2fservice.py").status_code in (400, 404)


def test_reader_requests_need_a_valid_signature(api):
    client, w, secret = api
    m = w.cards["S1"].read_ndef().hex()
    assert client.post("/api/device/tap", content=b"{}").status_code == 401
    assert tap(client, secret, m, sig="00" * 32).status_code == 401
    assert tap(client, "ff" * 16, m).status_code == 401                       # signed with another secret
    assert tap(client, secret, m, device="ghost").status_code == 401
    assert client.get("/api/device/poll").status_code == 401
    assert poll(client, "ff" * 16).status_code == 401
    assert poll(client, secret).status_code == 200


def test_garbage_bodies_get_clean_answers_not_crashes(api):
    client, w, secret = api
    for body in (b"not json", b"[1,2,3]", b'{"charge_id": "abc"}', b'{"ndef": 5}'):
        r = client.post("/api/device/tap", content=body,
                        headers={"X-Device-Id": "reader-1", "X-Signature": sign_body(secret, body)})
        assert r.status_code in (200, 400), (body, r.status_code, r.text)
        assert r.status_code != 500
    body = b"{}"
    r = client.post("/api/device/tap", content=body,
                    headers={"X-Device-Id": "reader-1", "X-Signature": sign_body(secret, body)})
    assert r.json()["code"] == "BAD_TAP"


def test_roles_are_isolated(api):
    client, *_ = api
    student = login(client, "student", "S1", "1234")
    vendor = login(client, "vendor", "shop", "1234")
    admin = login(client, "admin", "", "admin123")
    assert client.get("/api/student/me").status_code == 401
    assert client.get("/api/admin/overview", headers=student).status_code == 401
    assert client.get("/api/admin/overview", headers=vendor).status_code == 401
    assert client.get("/api/student/me", headers=admin).status_code == 401
    assert client.post("/api/vendor/charge", json={"amount": 5}, headers=student).status_code == 401
    assert client.get("/api/admin/overview", headers=admin).status_code == 200


def test_the_whole_story_over_http(api):
    client, w, secret = api
    admin = login(client, "admin", "", "admin123")
    student = login(client, "student", "S1", "1234")
    vendor = login(client, "vendor", "shop", "1234")

    # attendance
    r = client.post("/api/admin/session/open", json={"device_id": "reader-1", "kind": "class", "title": "Maths"}, headers=admin)
    assert r.status_code == 200
    r = tap(client, secret, w.cards["S1"].read_ndef().hex()).json()
    assert r["code"] == "PRESENT"
    ov = client.get("/api/admin/overview", headers=admin).json()
    assert ov["devices"][0]["session"]["count"] == 1 and ov["ledger"]["ok"]
    assert any(e["code"] == "PRESENT" for e in client.get("/api/admin/events", headers=admin).json())

    # payment
    r = client.post("/api/admin/device/reader-1/mode", json={"mode": "vendor", "vendor_id": w.v1}, headers=admin)
    assert r.status_code == 200
    assert client.post("/api/vendor/charge", json={"amount": 45}, headers=vendor).status_code == 200
    p = poll(client, secret).json()
    assert p["mode"] == "vendor" and p["charge"]["amount"] == 45
    r = tap(client, secret, w.cards["S1"].read_ndef().hex(), charge_id=p["charge"]["id"]).json()
    assert r["code"] == "PAID" and r["l2"] == "Balance Rs 155"
    me = client.get("/api/student/me", headers=student).json()
    assert me["balance"] == 155 and any(h["amount"] == -45 for h in me["history"])
    v = client.get("/api/vendor/me", headers=vendor).json()
    assert v["balance"] == 45 and v["sales"][0]["amount"] == 45

    # lost card: block from the phone, next tap is refused, unblock needs the PIN
    assert client.post("/api/student/card/block", json={"blocked": True}, headers=student).json()["status"] == "blocked"
    client.post("/api/vendor/charge", json={"amount": 5}, headers=vendor)
    p = poll(client, secret).json()
    r = tap(client, secret, w.cards["S1"].read_ndef().hex(), charge_id=p["charge"]["id"]).json()
    assert r["code"] == "BLOCKED" and not r["ok"]
    assert client.post("/api/student/card/block", json={"blocked": False}, headers=student).status_code == 403
    assert client.post("/api/student/card/block", json={"blocked": False, "pin": "1234"}, headers=student).status_code == 200
    r = tap(client, secret, w.cards["S1"].read_ndef().hex(), charge_id=p["charge"]["id"]).json()
    assert r["code"] == "PAID"
    assert client.get("/api/admin/overview", headers=admin).json()["ledger"]["ok"]


def test_overview_warns_about_demo_security(api):
    client, *_ = api
    admin = login(client, "admin", "", "admin123")
    warnings = client.get("/api/admin/overview", headers=admin).json()["warnings"]
    assert any("admin password" in x for x in warnings)
