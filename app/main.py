"""HTTP API. Thin: validation and auth here, all rules in service.py."""
from __future__ import annotations

import json
import os

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from .config import Config
from .service import ApiError, Service

STATIC = os.path.join(os.path.dirname(__file__), "static")


class LoginIn(BaseModel):
    role: str
    ident: str = ""
    pin: str = ""


class BlockIn(BaseModel):
    blocked: bool
    pin: str | None = None


class AmountIn(BaseModel):
    amount: int


class ModeIn(BaseModel):
    mode: str
    vendor_id: int | None = None


class SessionIn(BaseModel):
    device_id: str
    kind: str
    title: str = Field(max_length=60)


class DeviceIn(BaseModel):
    device_id: str


class RegisterIn(BaseModel):
    uid: str
    roll_no: str
    replace: bool = False


def create_app(cfg: Config | None = None, service: Service | None = None) -> FastAPI:
    svc = service or Service(cfg or Config())
    app = FastAPI(title="Campus NFC", docs_url=None, redoc_url=None)
    app.state.svc = svc

    @app.exception_handler(ApiError)
    async def _api_error(_: Request, e: ApiError):
        return JSONResponse({"error": e.message}, status_code=e.status)

    def bearer(authorization: str | None = Header(default=None)) -> str | None:
        if authorization and authorization.lower().startswith("bearer "):
            return authorization[7:].strip()
        return None

    def need(role: str):
        def dep(token: str | None = Depends(bearer)) -> int:
            return svc.auth(token, role)
        return dep

    # ---------------------------------------------------------------- pages
    for route, name in (("/", "index"), ("/student", "student"), ("/vendor", "vendor"), ("/admin", "admin")):
        app.add_api_route(route, (lambda n=name: FileResponse(os.path.join(STATIC, f"{n}.html"))),
                          methods=["GET"], include_in_schema=False)

    app.mount("/static", StaticFiles(directory=STATIC), name="static")   # shared css/js for the pages

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon():   # browsers ask for this on every page; answer quietly instead of logging a 404
        return Response(status_code=204)

    @app.get("/api/health")
    def health():
        with svc.session() as c:
            c.execute("SELECT 1").fetchone()
        return {"ok": True, "server_time": svc.clock()}

    # ---------------------------------------------------------------- auth
    @app.post("/api/login")
    def login(b: LoginIn):
        return svc.login(b.role, b.ident, b.pin)

    # ---------------------------------------------------------------- reader (ESP32)
    @app.post("/api/device/tap")
    async def device_tap(request: Request, x_device_id: str | None = Header(default=None),
                         x_signature: str | None = Header(default=None)):
        body = await request.body()
        await run_in_threadpool(svc.device_auth, x_device_id, body, x_signature)
        try:
            data = json.loads(body or b"{}")
            ndef, url, charge_id = data.get("ndef"), data.get("url"), data.get("charge_id")
            if charge_id is not None:
                charge_id = int(charge_id)
        except (ValueError, TypeError, AttributeError):
            raise ApiError(400, "bad json")
        return await run_in_threadpool(svc.device_tap, x_device_id, ndef, url, charge_id)

    @app.get("/api/device/poll")
    async def device_poll(x_device_id: str | None = Header(default=None), x_signature: str | None = Header(default=None)):
        await run_in_threadpool(svc.device_auth, x_device_id, b"poll", x_signature)
        return await run_in_threadpool(svc.device_poll, x_device_id)

    # ---------------------------------------------------------------- student
    @app.get("/api/student/me")
    def student_me(sid: int = Depends(need("student"))):
        return svc.student_me(sid)

    @app.post("/api/student/card/block")
    def student_block(b: BlockIn, sid: int = Depends(need("student"))):
        return svc.student_set_block(sid, b.blocked, b.pin)

    @app.post("/api/student/topup")
    def student_topup(b: AmountIn, sid: int = Depends(need("student"))):
        return svc.student_topup(sid, b.amount)

    # ---------------------------------------------------------------- vendor
    @app.get("/api/vendor/me")
    def vendor_me(vid: int = Depends(need("vendor"))):
        return svc.vendor_me(vid)

    @app.post("/api/vendor/charge")
    def vendor_charge(b: AmountIn, vid: int = Depends(need("vendor"))):
        return svc.vendor_create_charge(vid, b.amount)

    @app.post("/api/vendor/charge/cancel")
    def vendor_cancel(vid: int = Depends(need("vendor"))):
        return svc.vendor_cancel_charge(vid)

    # ---------------------------------------------------------------- admin
    @app.get("/api/admin/overview")
    def admin_overview(_: int = Depends(need("admin"))):
        return svc.admin_overview()

    @app.get("/api/admin/events")
    def admin_events(after_id: int | None = None, _: int = Depends(need("admin"))):
        return svc.admin_events(after_id)

    @app.post("/api/admin/device/{device_id}/mode")
    def admin_mode(device_id: str, b: ModeIn, _: int = Depends(need("admin"))):
        return svc.admin_set_mode(device_id, b.mode, b.vendor_id)

    @app.post("/api/admin/session/open")
    def admin_open(b: SessionIn, _: int = Depends(need("admin"))):
        return svc.admin_open_session(b.device_id, b.kind, b.title)

    @app.post("/api/admin/session/close")
    def admin_close(b: DeviceIn, _: int = Depends(need("admin"))):
        return svc.admin_close_session(b.device_id)

    @app.get("/api/admin/session/{session_id}/attendance")
    def admin_attendance(session_id: int, _: int = Depends(need("admin"))):
        return svc.admin_session_attendance(session_id)

    @app.post("/api/admin/card/register")
    def admin_register(b: RegisterIn, _: int = Depends(need("admin"))):
        return svc.admin_register_card(b.uid, b.roll_no, b.replace)

    return app


def app_factory() -> FastAPI:
    """Entry point for uvicorn:  uvicorn app.main:app_factory --factory"""
    return create_app()
