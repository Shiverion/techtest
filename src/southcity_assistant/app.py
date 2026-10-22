"""HTTP API."""

from __future__ import annotations

import logging
import threading
import time
import uuid
from collections import defaultdict, deque

from pathlib import Path

from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from .contracts import AskRequest, AskResponse
from .db import Database
from .llm import OpenAICompatiblePlanner, Planner
from .pipeline import Assistant
from .security.roles import RoleError, resolve_role
from .settings import Settings, get_settings

STATIC_DIR = Path(__file__).parent / "static"  # demo chat UI, served at /ui

HTTP_STATUS = {
    "ok": 200,
    "refused": 403,
    "query_timeout": 504,
    "database_unavailable": 503,
    "llm_not_configured": 503,
}


class RateLimiter:
    """Per-client sliding window. In-memory: adequate for one process; use a shared store behind a
    load balancer."""

    MAX_CLIENTS = 10_000

    def __init__(self, per_minute: int):
        self.per_minute = per_minute
        self.hits: dict[str, deque[float]] = defaultdict(deque)
        self.lock = threading.Lock()
        self.last_sweep = time.monotonic()

    def allow(self, client: str) -> bool:
        now = time.monotonic()
        with self.lock:
            if now - self.last_sweep > 60:  # forget idle clients at most once a minute (amortized)
                for key in [k for k, w in self.hits.items() if not w or now - w[-1] > 60]:
                    del self.hits[key]
                self.last_sweep = now
            if client not in self.hits and len(self.hits) >= self.MAX_CLIENTS:
                return False  # hard capacity: new clients wait until idle ones expire
            window = self.hits[client]
            while window and now - window[0] > 60:
                window.popleft()
            if len(window) >= self.per_minute:
                return False
            window.append(now)
            return True


def create_app(
    settings: Settings | None = None, planner: Planner | None = None, db: Database | None = None
) -> FastAPI:
    """`planner` and `db` are injection points for tests and the red-team harness only."""
    settings = settings or get_settings()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    db = db or Database(settings)
    assistant = Assistant(settings, db, planner or OpenAICompatiblePlanner(settings))
    limiter = RateLimiter(settings.rate_limit_per_minute)
    slots = threading.BoundedSemaphore(settings.max_concurrent_requests)

    app = FastAPI(
        title="SouthCity AI Data Query Assistant",
        version="1.0.0",
        description="Read-only Text-to-SQL over the SouthCity ERP with role-based access control.",
    )

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception) -> JSONResponse:
        # Never leak stack traces, SQL Server messages or configuration to the client.
        logging.getLogger("southcity").exception("unhandled error")
        return JSONResponse(status_code=500, content={"status": "error", "code": "internal_error",
                                                      "answer": "Terjadi kesalahan internal."})

    @app.middleware("http")
    async def ui_headers(request: Request, call_next):
        response = await call_next(request)
        if request.url.path.startswith("/ui"):
            # The demo page loads only its own files and talks only to this API.
            response.headers["Content-Security-Policy"] = (
                "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
                "img-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
            )
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @app.get("/", include_in_schema=False)
    def home() -> RedirectResponse:
        return RedirectResponse("/ui/")

    app.mount("/ui", StaticFiles(directory=STATIC_DIR, html=True), name="ui")

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/api/v1/ask", response_model=AskResponse)
    def ask(
        body: AskRequest,
        request: Request,
        x_user_role: str | None = Header(default=None, description="Sales_Staff or Management"),
        x_api_key: str | None = Header(default=None, description="Required when AUTH_MODE=api_key"),
    ) -> JSONResponse:
        request_id = uuid.uuid4().hex[:12]

        def reply(status: int, payload: AskResponse) -> JSONResponse:
            return JSONResponse(status_code=status, content=payload.model_dump(),
                                headers={"X-Request-ID": request_id})

        try:
            role = resolve_role(
                header_role=x_user_role,
                body_role=body.user_role,
                api_key=x_api_key,
                auth_mode=settings.auth_mode,
                api_keys=settings.api_keys,
            )
        except RoleError as exc:
            return reply(exc.status, AskResponse(request_id=request_id, status="refused",
                                                 code=exc.code, answer=exc.message))

        # Only an authenticated key identifies a client; in demo mode an arbitrary X-API-Key header
        # must not buy a fresh rate-limit window.
        client = (f"key:{x_api_key}" if settings.auth_mode == "api_key"
                  else f"ip:{request.client.host if request.client else 'unknown'}")
        if not limiter.allow(client):
            return reply(429, AskResponse(request_id=request_id, status="refused", role=role.value,
                                          code="rate_limited", answer="Terlalu banyak permintaan."))
        if not slots.acquire(blocking=False):
            return reply(503, AskResponse(request_id=request_id, status="error", role=role.value,
                                          code="busy", answer="Server sedang sibuk, coba lagi."))
        try:
            response = assistant.ask(body.question, role, request_id)
        finally:
            slots.release()
        status = HTTP_STATUS.get(response.code or "", HTTP_STATUS.get(response.status, 502))
        return reply(status, response)

    return app


def main() -> None:
    import uvicorn

    uvicorn.run("southcity_assistant.app:create_app", factory=True, host="127.0.0.1", port=8000)
