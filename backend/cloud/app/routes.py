"""HTTP routes and request bodies. SQL lives in store.py; auth, cache and limits in main.py."""
from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Response, UploadFile
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from . import store
from .main import (Settings, TTLCache, app_conn, dashboard_cache, dashboard_pool,
                   dashboard_rate_limit, get_settings, require_scope)

CHANNEL_PATTERN = r"^[A-Za-z_][A-Za-z0-9_.]{0,63}$"


# Request bodies (unknown fields are rejected) ---------------------------------------

class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProfileCreate(_Strict):
    consent_state: Literal["pending", "granted"] = "pending"
    demo_visible: bool = False


class ConsentChange(_Strict):
    consent_state: Literal["granted", "withdrawn"]


class Calibration(_Strict):
    thresholds: dict = Field(default_factory=dict)
    metric_summary: dict = Field(default_factory=dict)


class SessionMetadata(_Strict):
    profile_id: UUID
    client_session_id: UUID       # made on the laptop; makes retries idempotent
    tier: Literal[1, 2, 3]
    data_origin: Literal["measured", "synthetic_seed"]
    calibration: Calibration | None = None
    simulator_metrics: dict = Field(default_factory=dict)


# Profiles, consent and deletion. Cloud upload consent is consent_state = 'granted'. --

profiles = APIRouter(prefix="/v1", tags=["profiles"])
WRITE = [Depends(require_scope("profiles:write"))]
READ = [Depends(require_scope("aggregates:read"))]


def _deleting(profile_id: UUID) -> dict:
    return {"status": "deleting", "status_url": f"/v1/deletions/{profile_id}"}


@profiles.post("/profiles", status_code=201, dependencies=WRITE)
def create_profile(body: ProfileCreate, response: Response, conn=Depends(app_conn)):
    profile = store.create_profile(conn, body.consent_state, body.demo_visible)
    response.headers["Location"] = f"/v1/profiles/{profile['profile_id']}"
    return profile


@profiles.get("/profiles/{profile_id}", dependencies=READ)
def get_profile(profile_id: UUID, conn=Depends(app_conn)):
    profile = store.get_profile(conn, profile_id)
    if profile is None:
        raise HTTPException(404, "profile not found")
    return profile


@profiles.put("/profiles/{profile_id}/consent", dependencies=WRITE)
def change_consent(profile_id: UUID, body: ConsentChange, response: Response,
                   conn=Depends(app_conn)):
    if body.consent_state == "withdrawn":   # withdrawal deletes everything about the profile
        if not store.delete_profile(conn, profile_id):
            raise HTTPException(404, "profile not found")
        response.status_code = 202
        return _deleting(profile_id)
    profile = store.grant_consent(conn, profile_id)
    if profile is None:
        raise HTTPException(404, "profile not found")
    return profile


@profiles.delete("/profiles/{profile_id}", status_code=202, dependencies=WRITE)
def delete_profile(profile_id: UUID, conn=Depends(app_conn)):
    if not store.delete_profile(conn, profile_id):
        raise HTTPException(404, "profile not found")
    return _deleting(profile_id)


@profiles.get("/deletions/{profile_id}", dependencies=WRITE)
def deletion_status(profile_id: UUID, conn=Depends(app_conn)):
    """Base tables cascade at once; summary rows clear at the next refresh policy run (~1 min)."""
    status = store.deletion_status(conn, profile_id)
    done = not status["profile_exists"] and not any(status["leftover_summary_rows"].values())
    return {"status": "complete" if done else "pending", **status}


@profiles.get("/profiles/{profile_id}/trends", dependencies=READ)
def profile_trends(profile_id: UUID, conn=Depends(app_conn),
                   channel: str | None = Query(default=None, pattern=CHANNEL_PATTERN)):
    return store.profile_trends(conn, profile_id, channel)


# Sessions -----------------------------------------------------------------------------

sessions = APIRouter(prefix="/v1", tags=["sessions"])


def _save(upload: UploadFile, path: Path, limit: int) -> int:
    """Copy an upload to disk, failing with 413 past `limit` bytes. Returns bytes written."""
    written = 0
    with path.open("wb") as out:
        while chunk := upload.file.read(1 << 20):
            written += len(chunk)
            if written > limit:
                raise HTTPException(413, f"upload exceeds {limit} bytes")
            out.write(chunk)
    return written


@sessions.post("/sessions", status_code=201,
               dependencies=[Depends(require_scope("sessions:write"))])
def upload_session(response: Response,
                   metadata: str = Form(..., description="SessionMetadata as JSON"),
                   frames: UploadFile = File(..., description="camera feature messages, JSONL"),
                   events: UploadFile = File(..., description="interim event format, JSONL"),
                   conn=Depends(app_conn), settings: Settings = Depends(get_settings)):
    try:
        meta = SessionMetadata.model_validate_json(metadata)
    except ValidationError as e:
        raise HTTPException(422, json.loads(e.json(include_url=False))) from None

    tmp = Path(tempfile.mkdtemp(prefix="sojourner-upload-"))
    try:
        budget = settings.max_upload_bytes
        budget -= _save(frames, tmp / "frames.jsonl", budget)
        _save(events, tmp / "events.jsonl", budget)
        try:
            result = store.ingest_session(conn, meta, tmp / "frames.jsonl", tmp / "events.jsonl")
        except store.IngestError as e:
            raise HTTPException(e.status, e.detail) from None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    response.headers["Location"] = f"/v1/sessions/{result['session_id']}"
    if not result["created"]:
        response.status_code = 200   # idempotent retry: already stored
    return result


@sessions.get("/sessions/{session_id}", dependencies=READ)
def get_session(session_id: UUID, conn=Depends(app_conn)):
    session = store.get_session(conn, session_id)
    if session is None:
        raise HTTPException(404, "session not found")
    return session


@sessions.get("/sessions/{session_id}/summary", dependencies=READ)
def session_summary(session_id: UUID, conn=Depends(app_conn)):
    if store.get_session(conn, session_id) is None:
        raise HTTPException(404, "session not found")
    return store.session_summary(conn, session_id)


# Dashboard: public, non-critical. demo_reader role only; rate limited; cached; never
# uses the app pool. -------------------------------------------------------------------

dashboard = APIRouter(prefix="/v1/dashboard", tags=["dashboard"],
                      dependencies=[Depends(dashboard_rate_limit)])


def _query(pool, fn, *args):
    with pool.connection() as conn:
        return fn(conn, *args)


@dashboard.get("/sessions")
def dashboard_sessions(pool=Depends(dashboard_pool), cache: TTLCache = Depends(dashboard_cache)):
    return cache.get(("sessions",), lambda: _query(pool, store.dashboard_sessions))


@dashboard.get("/trends")
def dashboard_trends(pool=Depends(dashboard_pool), cache: TTLCache = Depends(dashboard_cache),
                     channel: str | None = Query(default=None, pattern=CHANNEL_PATTERN)):
    return cache.get(("trends", channel), lambda: _query(pool, store.dashboard_trends, channel))


ROUTERS = (profiles, sessions, dashboard)
