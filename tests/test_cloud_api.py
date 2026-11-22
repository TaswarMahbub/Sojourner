"""Cloud API routes with an in-memory store. No database: SQL itself is not exercised here.

Run from the repository root:  pip install -r requirements.txt && pytest tests
"""
from __future__ import annotations

import csv
import io
import json
import sys
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import psycopg
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend" / "cloud"))
from app import main, store  # noqa: E402
from app.main import RateLimiter, Settings, create_app  # noqa: E402
from app.store import hash_token  # noqa: E402

PEPPER = "test-pepper"
ALL = ["profiles:write", "sessions:write", "aggregates:read"]


# Constructed contract frames (not recorded data) ------------------------------

def tracking_frame(ts_ms: int) -> dict:
    return {"schema_version": "4", "timestamp_ms": ts_ms, "tracking_state": "tracking",
            "features": {n: 0.1 for n in store.tiger_store.BLENDSHAPE_NAMES},
            "head_pose": {"matrix": [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, -50], [0, 0, 0, 1]],
                          "yaw_deg": 0.0, "pitch_deg": 0.0, "roll_deg": 0.0},
            "confidence": None}


def jsonl(*objs) -> bytes:
    return ("\n".join(json.dumps(o) for o in objs) + "\n").encode()


EVENTS = jsonl({"t": 1795276800.5, "cue": "go", "confidence": 0.9, "stop_reason": None})


# Fakes ------------------------------------------------------------------------

class FakeConn:
    @contextmanager
    def transaction(self):
        yield


class FakePools:
    def __init__(self):
        self.dashboard = self

    def open(self):
        pass

    def close(self):
        pass

    @contextmanager
    def connection(self):
        yield FakeConn()


class FakeDB:
    def __init__(self):
        self.clients, self.profiles, self.sessions = {}, {}, {}
        self.copied, self.dashboard_calls, self.dashboard_fail = [], 0, False
        self.leftover = {"signal_summary_1min": 0, "control_summary_1min": 0}
        self.session_rows = [{"session_alias": "abc"}]
        self.trend_rows = []

    def add_token(self, token, scopes):
        self.clients[hash_token(token, PEPPER)] = scopes

    def profile(self, consent="granted"):
        pid = uuid.uuid4()
        self.profiles[pid] = {"profile_id": pid, "consent_state": consent, "demo_visible": False,
                              "created_at": "2026-11-21T00:00:00Z"}
        return pid


@pytest.fixture
def db(monkeypatch):
    fake = FakeDB()
    s = monkeypatch.setattr
    s(store, "client_scopes", lambda c, h: fake.clients.get(h))
    s(store, "create_profile", lambda c, cs, dv: fake.profiles.setdefault(
        pid := uuid.uuid4(), {"profile_id": pid, "consent_state": cs, "demo_visible": dv,
                              "created_at": "2026-11-21T00:00:00Z"}))
    s(store, "get_profile", lambda c, pid: fake.profiles.get(pid))

    def grant(c, pid):
        if pid in fake.profiles:
            fake.profiles[pid]["consent_state"] = "granted"
        return fake.profiles.get(pid)
    s(store, "grant_consent", grant)
    s(store, "delete_profile", lambda c, pid: fake.profiles.pop(pid, None) is not None)
    s(store, "deletion_status", lambda c, pid: {"profile_exists": pid in fake.profiles,
                                                "leftover_summary_rows": dict(fake.leftover)})
    s(store, "lock_profile_consent",
      lambda c, pid: fake.profiles[pid]["consent_state"] if pid in fake.profiles else None)
    s(store, "session_by_client_id", lambda c, csid: next(
        (sid for sid, v in fake.sessions.items() if v["client_session_id"] == csid), None))

    def insert_session(c, **kw):
        sid = uuid.uuid4()
        fake.sessions[sid] = kw
        return sid
    s(store, "insert_session", insert_session)
    s(store, "close_session", lambda c, sid, ended, sim: fake.sessions[sid].update(ended_at=ended))
    s(store.tiger_store, "add_calibration", lambda c, *a: uuid.uuid4())

    def copy_rows(c, spec, rows):
        rows = list(rows)
        fake.copied.append((spec.split()[0], len(rows)))
        return len(rows)
    s(store.tiger_store, "copy_rows", copy_rows)

    def dashboard_sessions(c):
        fake.dashboard_calls += 1
        if fake.dashboard_fail:
            raise psycopg.OperationalError("down")
        return fake.session_rows
    s(store, "dashboard_sessions", dashboard_sessions)

    def dashboard_trends(c, channel):
        fake.dashboard_calls += 1
        return [r for r in fake.trend_rows if channel is None or r["channel_name"] == channel]
    s(store, "dashboard_trends", dashboard_trends)
    return fake


@pytest.fixture
def client(db):
    app = create_app(Settings("unused", "unused", PEPPER, max_upload_bytes=10_000_000),
                     pools=FakePools())
    app.dependency_overrides[main.app_conn] = lambda: FakeConn()
    db.add_token("good", ALL)
    db.add_token("readonly", ["aggregates:read"])
    with TestClient(app) as c:
        yield c


def auth(token="good"):
    return {"Authorization": f"Bearer {token}"}


def upload(client, pid, frames, csid=None, token="good", events=EVENTS, **meta):
    metadata = {"profile_id": str(pid), "client_session_id": str(csid or uuid.uuid4()),
                "tier": 1, "data_origin": "synthetic_seed", **meta}
    return client.post("/v1/sessions", headers=auth(token),
                       data={"metadata": json.dumps(metadata)},
                       files={"frames": ("f.jsonl", frames), "events": ("e.jsonl", events)})


# Auth -------------------------------------------------------------------------

def test_health_needs_no_token(client):
    assert client.get("/healthz").json() == {"ok": True}


@pytest.mark.parametrize("headers, status", [({}, 401), (auth("nope"), 401), (auth("readonly"), 403)])
def test_writes_need_a_valid_token_with_scope(client, headers, status):
    assert client.post("/v1/profiles", json={}, headers=headers).status_code == status


def test_any_team_token_reads_any_profile(client, db):
    pid = db.profile()
    assert client.get(f"/v1/profiles/{pid}", headers=auth("readonly")).status_code == 200


# Profiles and consent -----------------------------------------------------------

def test_create_profile_defaults_to_pending_and_rejects_unknown_fields(client):
    r = client.post("/v1/profiles", json={}, headers=auth())
    assert r.status_code == 201 and r.json()["consent_state"] == "pending"
    assert client.post("/v1/profiles", json={"display_name": "x"}, headers=auth()).status_code == 422
    assert client.post("/v1/profiles", json={"consent_state": "withdrawn"},
                       headers=auth()).status_code == 422


def test_withdrawing_consent_deletes_the_profile(client, db):
    pid = db.profile()
    r = client.put(f"/v1/profiles/{pid}/consent", json={"consent_state": "withdrawn"}, headers=auth())
    assert r.status_code == 202 and r.json()["status_url"] == f"/v1/deletions/{pid}"
    assert pid not in db.profiles


def test_deletion_status_is_pending_until_summary_rows_clear(client, db):
    pid = db.profile()
    assert client.delete(f"/v1/profiles/{pid}", headers=auth()).status_code == 202
    db.leftover["signal_summary_1min"] = 4
    assert client.get(f"/v1/deletions/{pid}", headers=auth()).json()["status"] == "pending"
    db.leftover["signal_summary_1min"] = 0
    assert client.get(f"/v1/deletions/{pid}", headers=auth()).json()["status"] == "complete"


# Ingestion ------------------------------------------------------------------------

def test_upload_writes_samples_and_events(client, db):
    pid = db.profile()
    r = upload(client, pid, jsonl(tracking_frame(1795276800000), tracking_frame(1795276800033)))
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["samples"] == 2 * (52 + 3) and body["events"] == 1
    assert body["rejected_frames"]["total"] == 0
    assert r.headers["Location"] == f"/v1/sessions/{body['session_id']}"


@pytest.mark.parametrize("event", [
    {"t": 1795276800.5, "cue": None},          # NOT NULL in control_events: was a 500
    {"t": 1795276800.5},                       # no cue
    {"cue": "go"},                             # no t
    {"t": 1e300, "cue": "go"},                 # beyond the representable date range
    {"t": "yesterday", "cue": "go"},
    {"t": True, "cue": "go"},
    {"t": 1795276800.5, "cue": "go", "confidence": "high"},
    {"t": 1795276800.5, "cue": "go", "stop_reason": 3},
    ["not", "an", "object"],
])
def test_malformed_event_is_a_422_naming_the_line(client, db, event):
    good = {"t": 1795276800.5, "cue": "go"}
    r = upload(client, db.profile(), jsonl(tracking_frame(1795276800000)),
               events=jsonl(good, event))
    assert r.status_code == 422, r.text
    assert "line 2" in r.json()["detail"]


def test_upload_is_refused_without_granted_consent(client, db):
    assert upload(client, db.profile("pending"), jsonl(tracking_frame(1))).status_code == 403
    assert upload(client, uuid.uuid4(), jsonl(tracking_frame(1))).status_code == 404
    assert db.sessions == {}


def test_retry_with_same_client_session_id_is_idempotent(client, db):
    pid, csid = db.profile(), uuid.uuid4()
    first = upload(client, pid, jsonl(tracking_frame(1)), csid=csid)
    second = upload(client, pid, jsonl(tracking_frame(1)), csid=csid)
    assert (first.status_code, second.status_code) == (201, 200)
    assert first.json()["session_id"] == second.json()["session_id"]
    assert len(db.sessions) == 1


def test_bad_frames_are_reported_and_all_bad_is_422(client, db):
    pid = db.profile()
    bad = {**tracking_frame(2), "schema_version": "3"}
    r = upload(client, pid, jsonl(tracking_frame(1), bad))
    assert r.status_code == 201 and r.json()["rejected_frames"]["first"][0]["line"] == 2
    r = upload(client, pid, jsonl(bad) + b"{not json\n")
    assert r.status_code == 422 and r.json()["detail"]["rejected_frames"]["total"] == 2


def test_metadata_is_validated(client, db):
    r = upload(client, db.profile(), jsonl(tracking_frame(1)), tier=7)
    assert r.status_code == 422


def test_oversized_upload_is_413(client, db):
    client.app.state.settings = Settings("u", "u", PEPPER, max_upload_bytes=100)
    assert upload(client, db.profile(), jsonl(tracking_frame(1))).status_code == 413


# Dashboard --------------------------------------------------------------------------

def test_dashboard_is_public_cached_and_serves_stale_on_failure(client, db):
    first = client.get("/v1/dashboard/sessions").json()
    assert first == {"data": [{"session_alias": "abc"}], "stale": False}
    client.get("/v1/dashboard/sessions")
    assert db.dashboard_calls == 1                       # second read came from the cache

    client.app.state.dashboard_cache.ttl = 0             # force a reload, with the database down
    db.dashboard_fail = True
    assert client.get("/v1/dashboard/sessions").json()["stale"] is True


def test_dashboard_without_cache_and_database_down_is_503(client, db):
    db.dashboard_fail = True
    assert client.get("/v1/dashboard/sessions").status_code == 503


def test_channel_parameter_is_validated(client):
    assert client.get("/v1/dashboard/trends", params={"channel": "x; DROP"}).status_code == 422


# Dashboard CSV export --------------------------------------------------------------
# The store is faked here, so these tests can't catch a leak introduced in the SQL views. The
# real check is the live call: report.csv on the server must show only P- aliases.

def _view_rows(db):
    """Rows shaped like demo_session_summary and demo_channel_trend (columns from schema.sql)."""
    started = datetime(2026, 11, 21, 16, 0, tzinfo=timezone.utc)
    db.session_rows = [
        {"profile_alias": "P-1a2b3c", "session_alias": "0f1e2d3c", "data_origin": "measured",
         "tier": 1, "started_at": started, "duration_s": 90.0,
         "simulator_metrics": {"cleared": True, "hesitation_s": [0.4, 1.2]}},
        {"profile_alias": "P-1a2b3c", "session_alias": "9a8b7c6d", "data_origin": "measured",
         "tier": 1, "started_at": started, "duration_s": 85.5, "simulator_metrics": {}},
    ]
    db.trend_rows = [
        {"profile_alias": "P-1a2b3c", "session_alias": "0f1e2d3c", "data_origin": "measured",
         "started_at": started, "channel_name": name, "observed_range": 0.58,
         "tracking_ok_frac": 1.0}
        for name in ("jawOpen", "browInnerUp", "head_pose.yaw_deg")]


def _csv(response):
    return list(csv.DictReader(io.StringIO(response.text)))


def test_report_csv_matches_the_json_endpoint_and_leaks_no_ids(client, db):
    _view_rows(db)
    pid = db.profile()          # a real profile exists; its UUID must not reach the CSV
    r = client.get("/v1/dashboard/report.csv", params={"view": "sessions"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert r.headers["x-data-stale"] == "false"
    assert 'filename="sojourner-sessions.csv"' in r.headers["content-disposition"]
    header = r.text.splitlines()[0]
    assert header and header.split(",")[:2] == ["profile_alias", "session_alias"]
    rows = _csv(r)
    assert len(rows) == len(client.get("/v1/dashboard/sessions").json()["data"]) == 2
    assert "profile_id" not in r.text and str(pid) not in r.text
    assert json.loads(rows[0]["simulator_metrics"]) == {"cleared": True, "hesitation_s": [0.4, 1.2]}
    assert rows[0]["started_at"] == "2026-11-21T16:00:00+00:00"
    assert db.dashboard_calls == 1                   # CSV and JSON shared one cached load


def test_report_csv_channels_view_and_filter(client, db):
    _view_rows(db)
    everything = _csv(client.get("/v1/dashboard/report.csv", params={"view": "channels"}))
    one = client.get("/v1/dashboard/report.csv", params={"view": "channels", "channel": "jawOpen"})
    assert len(everything) == len(client.get("/v1/dashboard/trends").json()["data"]) == 3
    assert [r["channel_name"] for r in _csv(one)] == ["jawOpen"]
    assert 'filename="sojourner-channels.csv"' in one.headers["content-disposition"]


def test_report_csv_empty_result_is_an_empty_csv(client, db):
    db.session_rows = []
    r = client.get("/v1/dashboard/report.csv")      # view defaults to sessions
    assert r.status_code == 200 and r.text == ""
    assert r.headers["content-type"].startswith("text/csv")


@pytest.mark.parametrize("params", [{"view": "profiles"}, {"view": "channels", "channel": "x; DROP"},
                                    {"view": "sessions", "channel": "jawOpen"}])
def test_report_csv_rejects_bad_parameters(client, db, params):
    assert client.get("/v1/dashboard/report.csv", params=params).status_code == 422


def test_report_csv_marks_stale_data(client, db):
    _view_rows(db)
    client.get("/v1/dashboard/report.csv")
    client.app.state.dashboard_cache.ttl = 0
    db.dashboard_fail = True
    r = client.get("/v1/dashboard/report.csv")
    assert r.status_code == 200 and r.headers["x-data-stale"] == "true" and len(_csv(r)) == 2


def test_report_csv_is_rate_limited(client, db):
    client.app.state.dashboard_limiter = RateLimiter(per_minute=60, burst=1)
    codes = [client.get("/v1/dashboard/report.csv").status_code for _ in range(2)]
    assert codes == [200, 429]


# Rate limiting ------------------------------------------------------------------------

def test_dashboard_is_rate_limited_per_client(client, db):
    client.app.state.dashboard_limiter = RateLimiter(per_minute=60, burst=3)
    codes = [client.get("/v1/dashboard/sessions").status_code for _ in range(4)]
    assert codes == [200, 200, 200, 429]
    r = client.get("/v1/dashboard/sessions")
    assert r.status_code == 429 and int(r.headers["Retry-After"]) >= 1
    assert db.dashboard_calls == 1                       # limited requests never reach the DB
    assert client.get("/healthz").status_code == 200     # other routes are not limited


def test_rate_limiter_refills_and_isolates_clients():
    now = [0.0]
    limiter = RateLimiter(per_minute=60, burst=2, clock=lambda: now[0])
    assert [limiter.check("a") for _ in range(3)] == [0.0, 0.0, 1.0]
    assert limiter.check("b") == 0.0                     # another client has its own bucket
    now[0] = 1.0                                         # one token per second at 60/minute
    assert limiter.check("a") == 0.0


def test_rate_limiter_memory_is_bounded():
    limiter = RateLimiter(per_minute=60, burst=1, max_clients=10)
    for i in range(100):
        limiter.check(f"10.0.0.{i}")
    assert len(limiter._buckets) == 10
