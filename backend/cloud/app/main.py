"""Sojourner cloud API: settings, connection pools, auth, dashboard cache and rate limit, and the
app factory. Run with: uvicorn app.main:create_app --factory"""
from __future__ import annotations

import logging
import math
import os
import threading
import time
from collections import OrderedDict
from contextlib import asynccontextmanager
from dataclasses import dataclass

import psycopg
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from psycopg_pool import PoolTimeout

from . import store

log = logging.getLogger(__name__)


# Settings (environment only; secrets never live in code or git) ---------------------

@dataclass(frozen=True)
class Settings:
    database_url_app: str          # sojourner_app: profiles, sessions, per-profile aggregates
    database_url_dashboard: str    # login role under demo_reader: demo views only
    token_pepper: str
    allowed_origins: tuple[str, ...] = ()
    max_upload_bytes: int = 50 * 1024 * 1024
    dashboard_cache_seconds: float = 60.0
    dashboard_rate_per_minute: float = 60.0   # per client IP
    dashboard_burst: int = 20


def _required(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"{name} is not set")
    return value


def load_settings() -> Settings:
    env = os.environ.get
    return Settings(
        database_url_app=_required("DATABASE_URL_APP"),
        database_url_dashboard=_required("DATABASE_URL_DASHBOARD"),
        token_pepper=_required("TOKEN_PEPPER"),
        allowed_origins=tuple(o.strip() for o in env("ALLOWED_ORIGINS", "").split(",") if o.strip()),
        max_upload_bytes=int(float(env("MAX_UPLOAD_MB", "50")) * 1024 * 1024),
        dashboard_cache_seconds=float(env("DASHBOARD_CACHE_SECONDS", "60")),
        dashboard_rate_per_minute=float(env("DASHBOARD_RATE_PER_MINUTE", "60")),
        dashboard_burst=int(env("DASHBOARD_BURST", "20")),
    )


# Connection pools: one per database role. The dashboard pool cannot see base tables. -

class Pools:
    def __init__(self, settings: Settings):
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool

        self.app = ConnectionPool(settings.database_url_app, min_size=1, max_size=5, timeout=10,
                                  kwargs={"row_factory": dict_row}, open=False)
        # Short timeout: dashboard reads are non-critical and fall back to the cache.
        self.dashboard = ConnectionPool(settings.database_url_dashboard, min_size=1, max_size=3,
                                        timeout=3, kwargs={"row_factory": dict_row}, open=False)

    def open(self) -> None:
        self.app.open(wait=False)          # start even if the database is not up yet
        self.dashboard.open(wait=False)

    def close(self) -> None:
        self.app.close()
        self.dashboard.close()


# Dashboard protection: TTL cache with stale fallback, and a per-client token bucket ---

class TTLCache:
    def __init__(self, ttl_seconds: float, max_entries: int = 256, clock=time.monotonic):
        self.ttl, self.max_entries, self.clock = ttl_seconds, max_entries, clock
        self._items: OrderedDict = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key, load) -> dict:
        now = self.clock()
        with self._lock:
            hit = self._items.get(key)
        if hit is not None and now - hit[0] < self.ttl:
            return {"data": hit[1], "stale": False}
        try:
            value = load()
        except (psycopg.Error, PoolTimeout):
            log.warning("dashboard load failed for %r", key, exc_info=True)
            if hit is not None:
                return {"data": hit[1], "stale": True}
            raise HTTPException(503, "dashboard data unavailable") from None
        with self._lock:
            self._items[key] = (now, value)
            self._items.move_to_end(key)
            while len(self._items) > self.max_entries:
                self._items.popitem(last=False)
        return {"data": value, "stale": False}


class RateLimiter:
    """In-process: one bucket set per API process, enough for a single Vultr instance."""

    def __init__(self, per_minute: float, burst: int, max_clients: int = 10_000,
                 clock=time.monotonic):
        if per_minute <= 0 or burst < 1:
            raise ValueError("rate must be > 0 per minute and burst >= 1")
        self.rate = per_minute / 60.0          # tokens per second
        self.burst, self.max_clients, self.clock = burst, max_clients, clock
        self._buckets: OrderedDict[str, tuple[float, float]] = OrderedDict()
        self._lock = threading.Lock()

    def check(self, key: str) -> float:
        """0 if allowed (and spends a token), otherwise seconds until the next token."""
        now = self.clock()
        with self._lock:
            tokens, last = self._buckets.get(key, (float(self.burst), now))
            tokens = min(self.burst, tokens + (now - last) * self.rate)
            allowed = tokens >= 1
            self._buckets[key] = (tokens - 1 if allowed else tokens, now)
            self._buckets.move_to_end(key)
            while len(self._buckets) > self.max_clients:   # bound memory: drop least recent
                self._buckets.popitem(last=False)
        return 0.0 if allowed else (1 - tokens) / self.rate


# Request dependencies (tests override these instead of touching a database) ---------

def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def app_conn(request: Request):
    """One sojourner_app connection per request. Commits on success, rolls back on error."""
    with request.app.state.pools.app.connection() as conn:
        yield conn


def dashboard_pool(request: Request):
    return request.app.state.pools.dashboard


def dashboard_cache(request: Request) -> TTLCache:
    return request.app.state.dashboard_cache


def dashboard_rate_limit(request: Request) -> None:
    """Behind Caddy, request.client is the real client (uvicorn --proxy-headers)."""
    wait = request.app.state.dashboard_limiter.check(
        request.client.host if request.client else "unknown")
    if wait:
        raise HTTPException(429, "too many dashboard requests",
                            headers={"Retry-After": str(math.ceil(wait))})


def require_scope(scope: str):
    """Bearer-token auth for team devices. Any valid token with the scope works on any profile."""
    if scope not in store.SCOPES:
        raise ValueError(f"unknown scope {scope!r}")

    def dependency(authorization: str | None = Header(default=None),
                   conn=Depends(app_conn), settings: Settings = Depends(get_settings)) -> None:
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(401, "missing bearer token", headers={"WWW-Authenticate": "Bearer"})
        scopes = store.client_scopes(conn, store.hash_token(authorization[7:].strip(),
                                                            settings.token_pepper))
        if scopes is None:
            raise HTTPException(401, "invalid or revoked token", headers={"WWW-Authenticate": "Bearer"})
        if scope not in scopes:
            raise HTTPException(403, f"token lacks scope {scope}")

    return dependency


# App factory ------------------------------------------------------------------------

def create_app(settings: Settings | None = None, pools=None) -> FastAPI:
    from . import routes   # imported here: routes imports this module

    settings = settings or load_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.pools = pools if pools is not None else Pools(settings)
        app.state.pools.open()
        try:
            yield
        finally:
            app.state.pools.close()

    app = FastAPI(title="Sojourner cloud API", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.dashboard_cache = TTLCache(settings.dashboard_cache_seconds)
    app.state.dashboard_limiter = RateLimiter(settings.dashboard_rate_per_minute,
                                              settings.dashboard_burst)

    if settings.allowed_origins:   # browsers only need the public dashboard reads
        app.add_middleware(CORSMiddleware, allow_origins=list(settings.allowed_origins),
                           allow_methods=["GET"], allow_headers=["Authorization"],
                           # Let browser code read the CSV's stale flag and filename.
                           expose_headers=["X-Data-Stale", "Content-Disposition"])

    @app.exception_handler(PoolTimeout)
    @app.exception_handler(psycopg.OperationalError)
    async def database_unavailable(_request: Request, _exc: Exception):
        return JSONResponse({"detail": "database unavailable"}, status_code=503)

    @app.get("/healthz", tags=["health"])
    def healthz():
        return {"ok": True}

    for router in routes.ROUTERS:
        app.include_router(router)
    return app
