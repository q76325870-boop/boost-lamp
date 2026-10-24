import hmac
import ipaddress
import json
import logging
from pathlib import Path
import time
from typing import Annotated

from fastapi import Depends, FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, model_validator

from . import __version__
from .errors import Problem
from .stream import media_response

ROOT = Path(__file__).resolve().parent.parent
Author = Annotated[str, Query(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$")]


class Guard:
    def __init__(self, app, networks=(), origins=(), admin=False):
        self.app = app
        self.networks = tuple(ipaddress.ip_network(n) for n in networks)
        self.origins = origins
        self.admin = admin

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = dict(scope["headers"])
        address = scope.get("client", ("", 0))[0]
        try:
            allowed = self.admin or any(ipaddress.ip_address(address) in n for n in self.networks)
        except ValueError:
            allowed = False
        origin = headers.get(b"origin", b"").decode("latin1")
        expected_origin = scope["scheme"] + "://" + headers.get(b"host", b"").decode("latin1")
        if not allowed or (origin and (origin != expected_origin if self.admin else origin not in self.origins)):
            return await JSONResponse({"code": "FORBIDDEN"}, 403)(scope, receive, send)
        try:
            length = int(headers.get(b"content-length", b"0"))
        except ValueError:
            length = 65537
        if length > 65536 or length < 0:
            return await JSONResponse({"code": "REQUEST_TOO_LARGE"}, 413)(scope, receive, send)
        count = 0

        async def bounded_receive():
            nonlocal count
            message = await receive()
            count += len(message.get("body", b""))
            if count > 65536:
                raise Problem("REQUEST_TOO_LARGE", 413)
            return message

        async def protected_send(message):
            if message["type"] == "http.response.start":
                message["headers"] += [(b"x-content-type-options", b"nosniff"), (b"referrer-policy", b"no-referrer")]
            await send(message)

        await self.app(scope, bounded_receive, protected_send)


class PlaybackBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    author_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$")
    post_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9-]{0,79}$")
    video_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9-]{0,79}$")
    quality: str = Field(max_length=20)


class ProgressBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    position_seconds: FiniteFloat = Field(ge=0, le=604800)
    duration_seconds: FiniteFloat | None = Field(default=None, ge=0, le=604800)
    completed: bool = False

    @model_validator(mode="after")
    def valid_position(self):
        if self.duration_seconds is not None and self.position_seconds > self.duration_seconds:
            raise ValueError("Position exceeds duration")
        return self


class ApproveBody(BaseModel):
    code: str = Field(pattern=r"^\d{6}$")


def make_apps(service):
    public = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    admin = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    for app in (public, admin):
        @app.exception_handler(Problem)
        async def problem(request, error):
            return JSONResponse({"code": error.code, "retryable": error.status in (429, 502, 503)}, error.status, headers={"Cache-Control": "no-store"})

        @app.exception_handler(RequestValidationError)
        async def validation(request, error):
            return JSONResponse({"code": "INVALID_REQUEST", "retryable": False}, 422)

        @app.exception_handler(Exception)
        async def unexpected(request, error):
            logging.error("Request failed: %s", type(error).__name__)
            return JSONResponse({"code": "INTERNAL_ERROR", "retryable": False}, 500)

    public.add_middleware(Guard, networks=service.settings.networks, origins=service.settings.origins)
    public.add_middleware(CORSMiddleware, allow_origins=list(service.settings.origins),
                          allow_methods=["GET", "HEAD", "POST", "PUT", "DELETE"],
                          allow_headers=["Authorization", "Content-Type", "X-Pair-Secret", "Range", "If-Range"],
                          expose_headers=["Content-Length", "Content-Range", "Accept-Ranges"])
    admin.add_middleware(Guard, admin=True)

    async def device(request: Request):
        auth = request.headers.get("authorization", "")
        if not auth.startswith("Bearer ") or len(auth) > 256:
            raise Problem("DEVICE_REQUIRED", 401)
        return service.state.device(auth[7:]), service.boosty.generation

    async def owner(request: Request):
        if not hmac.compare_digest(request.headers.get("authorization", "").encode(), ("Bearer " + service.state.admin_key).encode()):
            raise Problem("ADMIN_REQUIRED", 401)

    @public.get("/health")
    async def health():
        return {"ok": True, "version": __version__}

    @public.get("/boosty.js")
    async def plugin():
        return FileResponse(ROOT / "plugin" / "boosty.js", media_type="application/javascript", headers={"Cache-Control": "no-cache"})

    @public.post("/boosty/v1/pairing")
    async def pair(request: Request):
        return service.pair(request.client.host)

    @public.get("/boosty/v1/pairing/{identifier}")
    async def claim(identifier: str, request: Request):
        return service.claim(identifier, request.headers.get("x-pair-secret", ""))

    @public.get("/boosty/v1/status")
    async def status(current=Depends(device)):
        service.state.read_session()
        return {"connected": True, "version": __version__}

    @public.get("/boosty/v1/authors")
    async def authors(cursor: str | None = Query(default=None, max_length=2048), current=Depends(device)):
        return await service.catalog(current, "authors", cursor=cursor)

    @public.get("/boosty/v1/posts")
    async def posts(author: Author, cursor: str | None = Query(default=None, max_length=2048), current=Depends(device)):
        return await service.catalog(current, "posts", author, cursor)

    @public.get("/boosty/v1/feed")
    async def feed(cursor: str | None = Query(default=None, max_length=2048), current=Depends(device)):
        return await service.catalog(current, "feed", cursor=cursor)

    @public.get("/boosty/v1/continue")
    async def continuation(current=Depends(device)):
        return {"items": service.state.continue_watching(), "next_cursor": None}

    @public.post("/boosty/v1/playbacks")
    async def play(body: PlaybackBody, current=Depends(device)):
        return await service.play(current, body.author_id, body.post_id, body.video_id, body.quality)

    @public.delete("/boosty/v1/playbacks/{identifier}")
    async def stop(identifier: str, current=Depends(device)):
        session = service.playbacks.get(identifier)
        if session and session["device"] == current[0]:
            service.playbacks.pop(identifier)
        return {"ok": True}

    @public.put("/boosty/v1/progress/{key}")
    async def progress(key: str, body: ProgressBody, current=Depends(device)):
        if (current[0], key) not in service.opened:
            raise Problem("UNKNOWN_VIDEO", 404)
        service.state.save_progress(key, body.position_seconds, body.duration_seconds, body.completed)
        return {"ok": True}

    @public.api_route("/boosty/media/{identifier}/video.mp4", methods=["GET", "HEAD"])
    async def stream(identifier: str, request: Request):
        return await media_response(service, identifier, request.method, request.headers.get("range"), request.headers.get("if-range"))

    @public.get("/boosty/images/{identifier}")
    async def image(identifier: str):
        item = service.images.get(identifier)
        if not item or item[1] < time.time():
            raise Problem("IMAGE_EXPIRED", 404)
        response = await service.safe.open("GET", item[0], image=True)
        try:
            mime = response.headers.get("content-type", "").split(";")[0]
            if response.status_code != 200 or mime not in ("image/jpeg", "image/png", "image/webp", "image/gif"):
                raise Problem("IMAGE_UNAVAILABLE", 404)
            data = bytearray()
            async for chunk in response.aiter_bytes():
                data.extend(chunk)
                if len(data) > 2 * 1024 * 1024:
                    raise Problem("IMAGE_TOO_LARGE", 413)
            return Response(bytes(data), media_type=mime, headers={"Cache-Control": "private, max-age=300"})
        finally:
            await response.aclose()

    @admin.get("/")
    async def page():
        return FileResponse(ROOT / "server" / "admin.html", headers={"Cache-Control": "no-store"})

    @admin.get("/admin/status", dependencies=[Depends(owner)])
    async def admin_status():
        try:
            service.state.read_session()
            connected = True
        except Problem:
            connected = False
        return {"connected": connected, "version": __version__, "public_url": service.settings.public_url,
                "pairings": [{"code": p["code"], "address": p["address"]} for p in service.pending.values() if p["expires"] > time.time() and not p["token"]]}

    @admin.post("/admin/session", dependencies=[Depends(owner)])
    async def import_session(request: Request):
        try:
            payload = await request.json()
        except (ValueError, UnicodeError):
            raise Problem("INVALID_SESSION") from None
        changed = await service.boosty.import_session(payload)
        if changed:
            service.reset()
        else:
            service.cache.clear()
        return {"connected": True, "account_changed": changed}

    @admin.post("/admin/pairing/approve", dependencies=[Depends(owner)])
    async def approve(body: ApproveBody):
        service.approve(body.code)
        return {"ok": True}

    return public, admin
